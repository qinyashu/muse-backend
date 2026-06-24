import logging
import os
import shutil
import tempfile

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from auth_utils import hash_password, verify_password
from config import ADMIN_TOKEN
from database import SessionLocal
from models import HotVideo, Task, User
from oss_utils import upload_file
from tasks import process_task


logger = logging.getLogger(__name__)

router = APIRouter()


class RegisterRequest(BaseModel):
    device_id: str


class AuthRequest(BaseModel):
    phone: str
    password: str


class RechargeRequest(BaseModel):
    device_id: str
    count: int
    admin_token: str


class HotVideoRequest(BaseModel):
    title: str
    type: str = "sing"
    cover_url: str
    video_url: str
    views: str = ""
    sort_order: int = 0
    is_enabled: bool = True
    admin_token: str


def _clean_phone(phone: str) -> str:
    return "".join(char for char in phone.strip() if char.isdigit())


def _validate_admin_token(admin_token: str) -> None:
    if admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="管理员 token 无效")


def _serialize_user(user: User) -> dict[str, int | str | bool]:
    return {
        "device_id": user.device_id,
        "phone": user.phone or "",
        "remaining_count": user.remaining_count,
        "is_unlimited": bool(user.is_unlimited),
    }


def _serialize_hot_video(video: HotVideo) -> dict[str, int | str | bool]:
    return {
        "id": video.id,
        "title": video.title,
        "type": video.type,
        "cover_url": video.cover_url,
        "video_url": video.video_url,
        "views": video.views,
        "sort_order": video.sort_order,
        "is_enabled": bool(video.is_enabled),
    }


@router.post("/auth/register")
def auth_register(payload: AuthRequest) -> dict[str, int | str | bool]:
    phone = _clean_phone(payload.phone)
    password = payload.password.strip()

    if len(phone) != 11:
        raise HTTPException(status_code=400, detail="手机号格式不正确")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")

    with SessionLocal() as db:
        existing = db.query(User).filter(User.phone == phone).first()
        if existing is not None:
            raise HTTPException(status_code=400, detail="手机号已注册，请直接登录")

        user = User(
            device_id=f"phone_{phone}",
            phone=phone,
            password_hash=hash_password(password),
            remaining_count=1,
            is_unlimited=False,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info("Phone account registered: phone=%s", phone)
        return _serialize_user(user)


@router.post("/auth/login")
def auth_login(payload: AuthRequest) -> dict[str, int | str | bool]:
    phone = _clean_phone(payload.phone)
    password = payload.password.strip()

    with SessionLocal() as db:
        user = db.query(User).filter(User.phone == phone).first()
        if user is None or not verify_password(password, user.password_hash):
            raise HTTPException(status_code=401, detail="手机号或密码错误")

        logger.info("Phone account logged in: phone=%s", phone)
        return _serialize_user(user)


@router.post("/user/register")
def register_user(payload: RegisterRequest) -> dict[str, int | bool]:
    with SessionLocal() as db:
        user = db.query(User).filter(User.device_id == payload.device_id).first()
        if user is None:
            user = User(device_id=payload.device_id, remaining_count=1, is_unlimited=False)
            db.add(user)
            db.commit()
            db.refresh(user)
            logger.info("Device user registered: device_id=%s", payload.device_id)
        else:
            logger.info("Device user already exists: device_id=%s", payload.device_id)

        return {
            "remaining_count": user.remaining_count,
            "is_unlimited": bool(user.is_unlimited),
        }


@router.post("/admin/recharge")
def recharge_user(payload: RechargeRequest) -> dict[str, int | str | bool]:
    _validate_admin_token(payload.admin_token)

    if payload.count <= 0:
        raise HTTPException(status_code=400, detail="充值次数必须大于 0")

    with SessionLocal() as db:
        user = db.query(User).filter(User.device_id == payload.device_id).first()
        if user is None:
            user = User(device_id=payload.device_id, remaining_count=0, is_unlimited=False)
            db.add(user)
            db.flush()
            logger.info("Created user during recharge: device_id=%s", payload.device_id)

        user.remaining_count += payload.count
        db.commit()
        db.refresh(user)
        return {
            "device_id": user.device_id,
            "remaining_count": user.remaining_count,
            "is_unlimited": bool(user.is_unlimited),
        }


@router.get("/user/info")
def get_user_info(device_id: str = "", phone: str = "") -> dict[str, int | bool]:
    with SessionLocal() as db:
        clean_phone = _clean_phone(phone) if phone else ""
        if clean_phone:
            user = db.query(User).filter(User.phone == clean_phone).first()
        else:
            user = db.query(User).filter(User.device_id == device_id).first()

        if user is None:
            logger.warning("User not found: device_id=%s, phone=%s", device_id, clean_phone)
            raise HTTPException(status_code=404, detail="用户不存在")

        return {
            "remaining_count": user.remaining_count,
            "is_unlimited": bool(user.is_unlimited),
        }


@router.get("/hot-videos")
def list_hot_videos() -> dict[str, list[dict[str, int | str | bool]]]:
    with SessionLocal() as db:
        videos = (
            db.query(HotVideo)
            .filter(HotVideo.is_enabled == True)  # noqa: E712
            .order_by(HotVideo.sort_order.asc(), HotVideo.id.desc())
            .all()
        )
        return {"videos": [_serialize_hot_video(video) for video in videos]}


@router.post("/admin/hot-videos")
def create_hot_video(payload: HotVideoRequest) -> dict[str, int | str | bool]:
    _validate_admin_token(payload.admin_token)
    if payload.type not in {"sing", "dance"}:
        raise HTTPException(status_code=400, detail="type 只能是 sing 或 dance")

    title = payload.title.strip()
    cover_url = payload.cover_url.strip()
    video_url = payload.video_url.strip()
    if not title or not cover_url or not video_url:
        raise HTTPException(status_code=400, detail="标题、封面和视频地址不能为空")

    with SessionLocal() as db:
        video = HotVideo(
            title=title,
            type=payload.type,
            cover_url=cover_url,
            video_url=video_url,
            views=payload.views.strip(),
            sort_order=payload.sort_order,
            is_enabled=payload.is_enabled,
        )
        db.add(video)
        db.commit()
        db.refresh(video)
        return _serialize_hot_video(video)


@router.put("/admin/hot-videos/{video_id}")
def update_hot_video(video_id: int, payload: HotVideoRequest) -> dict[str, int | str | bool]:
    _validate_admin_token(payload.admin_token)
    if payload.type not in {"sing", "dance"}:
        raise HTTPException(status_code=400, detail="type 只能是 sing 或 dance")

    with SessionLocal() as db:
        video = db.query(HotVideo).filter(HotVideo.id == video_id).first()
        if video is None:
            raise HTTPException(status_code=404, detail="热门作品不存在")

        video.title = payload.title.strip()
        video.type = payload.type
        video.cover_url = payload.cover_url.strip()
        video.video_url = payload.video_url.strip()
        video.views = payload.views.strip()
        video.sort_order = payload.sort_order
        video.is_enabled = payload.is_enabled
        db.commit()
        db.refresh(video)
        return _serialize_hot_video(video)


@router.delete("/admin/hot-videos/{video_id}")
def delete_hot_video(video_id: int, admin_token: str) -> dict[str, bool]:
    _validate_admin_token(admin_token)
    with SessionLocal() as db:
        video = db.query(HotVideo).filter(HotVideo.id == video_id).first()
        if video is None:
            raise HTTPException(status_code=404, detail="热门作品不存在")

        db.delete(video)
        db.commit()
        return {"ok": True}


@router.post("/task/create")
def create_task(
    background_tasks: BackgroundTasks,
    device_id: str = Form(...),
    task_type: str = Form(..., alias="type"),
    douyin_url: str = Form(""),
    photo: UploadFile = File(...),
    source_video: UploadFile | None = File(None),
) -> dict[str, int]:
    if task_type not in {"sing", "dance"}:
        raise HTTPException(status_code=400, detail="type 只能是 sing 或 dance")

    clean_douyin_url = douyin_url.strip()
    if not clean_douyin_url and source_video is None:
        raise HTTPException(status_code=400, detail="请填写抖音链接或上传源视频")

    temp_photo_path = ""
    temp_video_path = ""

    with SessionLocal() as db:
        user = db.query(User).filter(User.device_id == device_id).first()
        if user is None:
            logger.warning("Create task failed, user not found: device_id=%s", device_id)
            raise HTTPException(status_code=404, detail="用户不存在")

        if not user.is_unlimited and user.remaining_count <= 0:
            logger.warning("Create task failed, quota is not enough: device_id=%s", device_id)
            raise HTTPException(status_code=400, detail="剩余次数不足")

        try:
            task = Task(
                user_id=user.id,
                type=task_type,
                douyin_url=clean_douyin_url,
                source_video_url=None,
                photo_url="",
                status="queued",
                error_message=None,
            )
            db.add(task)
            db.flush()

            suffix = os.path.splitext(photo.filename or "")[1] or ".jpg"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
                shutil.copyfileobj(photo.file, temp_file)
                temp_photo_path = temp_file.name

            photo_url = upload_file(temp_photo_path, f"photos/{task.id}{suffix}")
            if not photo_url:
                raise RuntimeError("照片上传 OSS 失败")

            source_video_url = None
            if source_video is not None:
                video_suffix = os.path.splitext(source_video.filename or "")[1] or ".mp4"
                with tempfile.NamedTemporaryFile(delete=False, suffix=video_suffix) as temp_file:
                    shutil.copyfileobj(source_video.file, temp_file)
                    temp_video_path = temp_file.name

                source_video_url = upload_file(temp_video_path, f"source_videos/{task.id}{video_suffix}")
                if not source_video_url:
                    raise RuntimeError("源视频上传 OSS 失败")

            task.photo_url = photo_url
            task.source_video_url = source_video_url
            if not user.is_unlimited:
                user.remaining_count -= 1
            db.commit()
            db.refresh(task)

            background_tasks.add_task(process_task, task.id)
            logger.info("Task created: task_id=%s, device_id=%s", task.id, device_id)
            return {"task_id": task.id}

        except HTTPException:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            logger.exception("Create task failed: device_id=%s, error=%s", device_id, exc)
            raise HTTPException(status_code=500, detail=f"创建任务失败: {exc}") from exc
        finally:
            try:
                if temp_photo_path and os.path.exists(temp_photo_path):
                    os.remove(temp_photo_path)
                if temp_video_path and os.path.exists(temp_video_path):
                    os.remove(temp_video_path)
            except Exception as exc:
                logger.warning("Failed to remove upload temp files: %s", exc)


@router.get("/task/status")
def get_task_status(task_id: int) -> dict[str, str | None]:
    with SessionLocal() as db:
        task = db.query(Task).filter(Task.id == task_id).first()
        if task is None:
            logger.warning("Task not found: task_id=%s", task_id)
            raise HTTPException(status_code=404, detail="任务不存在")

        return {
            "status": task.status,
            "output_url": task.output_url,
            "error_message": task.error_message,
        }


@router.get("/task/list")
def list_tasks(device_id: str) -> dict[str, list[dict[str, int | str | None]]]:
    with SessionLocal() as db:
        user = db.query(User).filter(User.device_id == device_id).first()
        if user is None:
            logger.warning("Task list failed, user not found: device_id=%s", device_id)
            raise HTTPException(status_code=404, detail="用户不存在")

        tasks = (
            db.query(Task)
            .filter(Task.user_id == user.id)
            .order_by(Task.created_at.desc())
            .all()
        )

        return {
            "tasks": [
                {
                    "id": task.id,
                    "type": task.type,
                    "douyin_url": task.douyin_url,
                    "source_video_url": task.source_video_url,
                    "photo_url": task.photo_url,
                    "audio_url": task.audio_url,
                    "output_url": task.output_url,
                    "status": task.status,
                    "error_message": task.error_message,
                    "created_at": task.created_at.isoformat() if task.created_at else None,
                }
                for task in tasks
            ]
        }
