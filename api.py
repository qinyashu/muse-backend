import logging
import os
import shutil
import tempfile

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from config import ADMIN_TOKEN
from database import SessionLocal
from models import Task, User
from oss_utils import upload_file
from tasks import process_task


logger = logging.getLogger(__name__)

router = APIRouter()


class RegisterRequest(BaseModel):
    """用户注册请求体。"""

    device_id: str


class RechargeRequest(BaseModel):
    """管理员充值请求体。"""

    device_id: str
    count: int
    admin_token: str


@router.post("/user/register")
def register_user(payload: RegisterRequest) -> dict[str, int]:
    """注册设备用户；如果用户已存在，直接返回剩余次数。"""
    with SessionLocal() as db:
        user = db.query(User).filter(User.device_id == payload.device_id).first()
        if user is None:
            user = User(device_id=payload.device_id, remaining_count=1)
            db.add(user)
            db.commit()
            db.refresh(user)
            logger.info("新用户注册成功: device_id=%s", payload.device_id)
        else:
            logger.info("用户已存在: device_id=%s", payload.device_id)

        return {"remaining_count": user.remaining_count}


@router.post("/admin/recharge")
def recharge_user(payload: RechargeRequest) -> dict[str, int | str]:
    """管理员给指定设备用户增加剩余生成次数。"""
    if payload.admin_token != ADMIN_TOKEN:
        logger.warning("管理员充值失败，token 不匹配: device_id=%s", payload.device_id)
        raise HTTPException(status_code=403, detail="管理员 token 无效")

    if payload.count <= 0:
        raise HTTPException(status_code=400, detail="充值次数必须大于 0")

    with SessionLocal() as db:
        user = db.query(User).filter(User.device_id == payload.device_id).first()
        if user is None:
            user = User(device_id=payload.device_id, remaining_count=0)
            db.add(user)
            db.flush()
            logger.info("充值时自动创建用户: device_id=%s", payload.device_id)

        user.remaining_count += payload.count
        db.commit()
        db.refresh(user)
        logger.info(
            "管理员充值成功: device_id=%s, count=%s, remaining_count=%s",
            payload.device_id,
            payload.count,
            user.remaining_count,
        )

        return {"device_id": user.device_id, "remaining_count": user.remaining_count}


@router.get("/user/info")
def get_user_info(device_id: str) -> dict[str, int]:
    """查询用户剩余生成次数。"""
    with SessionLocal() as db:
        user = db.query(User).filter(User.device_id == device_id).first()
        if user is None:
            logger.warning("查询用户失败，用户不存在: device_id=%s", device_id)
            raise HTTPException(status_code=404, detail="用户不存在")

        return {"remaining_count": user.remaining_count}


@router.post("/task/create")
def create_task(
    background_tasks: BackgroundTasks,
    device_id: str = Form(...),
    type: str = Form(...),
    douyin_url: str = Form(...),
    photo: UploadFile = File(...),
) -> dict[str, int]:
    """创建生成任务，上传照片到 OSS，并把后台任务加入 FastAPI 队列。"""
    if type not in {"sing", "dance"}:
        raise HTTPException(status_code=400, detail="type 只能是 sing 或 dance")

    temp_photo_path = ""

    with SessionLocal() as db:
        user = db.query(User).filter(User.device_id == device_id).first()
        if user is None:
            logger.warning("创建任务失败，用户不存在: device_id=%s", device_id)
            raise HTTPException(status_code=404, detail="用户不存在")

        if user.remaining_count <= 0:
            logger.warning("创建任务失败，剩余次数不足: device_id=%s", device_id)
            raise HTTPException(status_code=400, detail="剩余次数不足")

        try:
            # 先创建任务并 flush，拿到 task.id 后才能按 photos/{task_id}.jpg 命名 OSS 对象。
            task = Task(
                user_id=user.id,
                type=type,
                douyin_url=douyin_url,
                photo_url="",
                status="queued",
            )
            db.add(task)
            db.flush()

            # 将上传图片保存为临时文件，随后上传到 OSS。
            with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as temp_file:
                shutil.copyfileobj(photo.file, temp_file)
                temp_photo_path = temp_file.name

            photo_url = upload_file(temp_photo_path, f"photos/{task.id}.jpg")
            if not photo_url:
                raise RuntimeError("照片上传 OSS 失败")

            # 写入照片 URL，扣减用户次数，并提交事务。
            task.photo_url = photo_url
            user.remaining_count -= 1
            db.commit()
            db.refresh(task)

            # 提交成功后再加入后台队列，避免后台任务读取不到数据库记录。
            background_tasks.add_task(process_task, task.id)
            logger.info("任务创建成功: task_id=%s, device_id=%s", task.id, device_id)
            return {"task_id": task.id}

        except HTTPException:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            logger.exception("创建任务失败: device_id=%s, 错误: %s", device_id, exc)
            raise HTTPException(status_code=500, detail="创建任务失败") from exc
        finally:
            # 清理照片临时文件，避免本地磁盘堆积。
            try:
                if temp_photo_path and os.path.exists(temp_photo_path):
                    os.remove(temp_photo_path)
            except Exception as exc:
                logger.warning("清理照片临时文件失败: %s, 错误: %s", temp_photo_path, exc)


@router.get("/task/status")
def get_task_status(task_id: int) -> dict[str, str | None]:
    """查询任务状态和最终输出 URL。"""
    with SessionLocal() as db:
        task = db.query(Task).filter(Task.id == task_id).first()
        if task is None:
            logger.warning("查询任务失败，任务不存在: task_id=%s", task_id)
            raise HTTPException(status_code=404, detail="任务不存在")

        return {"status": task.status, "output_url": task.output_url}
