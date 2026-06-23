import logging
import os
import subprocess
import time

import requests

from config import AUTODL_MODEL_API_URL
from database import SessionLocal
from gpu_manager import gpu_manager
from models import Task
from oss_utils import upload_file


logger = logging.getLogger(__name__)


def _run_command(command: list[str], step_name: str) -> None:
    """执行外部命令，并在失败时抛出异常。"""
    logger.info("开始执行%s命令: %s", step_name, command)
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("%s命令执行失败，stderr: %s", step_name, result.stderr)
        raise RuntimeError(f"{step_name}失败: {result.stderr}")
    logger.info("%s命令执行成功", step_name)


def _download_file(url: str, local_path: str) -> None:
    """使用 requests 将远程文件下载到本地路径。"""
    logger.info("开始下载生成视频: %s -> %s", url, local_path)
    response = requests.get(url, stream=True, timeout=600)
    response.raise_for_status()

    with open(local_path, "wb") as file_obj:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                file_obj.write(chunk)

    logger.info("生成视频下载完成: %s", local_path)


def _cleanup_files(paths: list[str]) -> None:
    """删除任务处理过程中产生的临时文件。"""
    for path in paths:
        try:
            if os.path.exists(path):
                os.remove(path)
                logger.info("已清理临时文件: %s", path)
        except Exception as exc:
            logger.warning("清理临时文件失败: %s, 错误: %s", path, exc)


def process_task(task_id: int) -> None:
    """后台处理任务：从抖音视频下载音频，并调用 GPU 模型生成最终 MV。"""
    started_at = time.time()
    db = SessionLocal()
    task: Task | None = None
    gpu_acquired = False

    video_path = f"/tmp/{task_id}.mp4"
    audio_path = f"/tmp/{task_id}.mp3"
    generated_video_path = f"/tmp/{task_id}_gen.mp4"
    temp_files = [video_path, audio_path, generated_video_path]

    try:
        # 读取任务记录，并先标记为处理中，方便前端查询进度。
        task = db.query(Task).filter(Task.id == task_id).first()
        if task is None:
            logger.error("未找到任务记录: task_id=%s", task_id)
            return

        task.status = "processing"
        db.commit()
        logger.info("任务已标记为 processing: task_id=%s", task_id)

        manage_autodl_instance = not AUTODL_MODEL_API_URL
        if manage_autodl_instance:
            # 登记 GPU 活跃请求，避免空闲关机定时器误关闭实例。
            gpu_manager.acquire()
            gpu_acquired = True

            # 确保 AutoDL GPU 实例已开机。
            if not gpu_manager.start_instance():
                raise RuntimeError("GPU 实例启动失败")
        else:
            logger.info("已配置 AUTODL_MODEL_API_URL，跳过 AutoDL 官方开机 API")

        if not task.douyin_url:
            raise ValueError("任务缺少 douyin_url")
        if not task.photo_url:
            raise ValueError("任务缺少 photo_url")

        # 下载抖音视频到临时目录。
        _run_command(
            ["yt-dlp", "-o", video_path, task.douyin_url],
            "抖音视频下载",
        )

        # 使用 ffmpeg 从视频中提取 mp3 音频。
        _run_command(
            ["ffmpeg", "-y", "-i", video_path, "-q:a", "0", "-map", "a", audio_path],
            "音频提取",
        )

        # 上传提取出的音频到 OSS，供模型 API 使用。
        audio_url = upload_file(audio_path, f"audios/{task_id}.mp3")
        if not audio_url:
            raise RuntimeError("音频上传 OSS 失败")
        logger.info("音频已上传 OSS: %s", audio_url)

        # 调用 GPU 模型，根据图片和音频生成视频。
        generated_video_url = gpu_manager.call_model(task.photo_url, audio_url)
        if not generated_video_url:
            raise RuntimeError("模型未返回生成视频 URL")
        logger.info("模型生成视频 URL: %s", generated_video_url)

        # 下载模型生成的视频到本地临时文件。
        _download_file(generated_video_url, generated_video_path)

        # 上传最终视频到 OSS，作为任务输出结果。
        output_url = upload_file(generated_video_path, f"outputs/{task_id}.mp4")
        if not output_url:
            raise RuntimeError("最终视频上传 OSS 失败")
        logger.info("最终视频已上传 OSS: %s", output_url)

        # 更新任务结果和状态。
        task.audio_url = audio_url
        task.output_url = output_url
        task.status = "done"
        db.commit()
        logger.info("任务处理完成: task_id=%s, output_url=%s", task_id, output_url)

    except Exception as exc:
        logger.exception("任务处理失败: task_id=%s, 错误: %s", task_id, exc)
        db.rollback()
        if task is not None:
            task.status = "failed"
            db.commit()
            logger.info("任务已标记为 failed: task_id=%s", task_id)

    finally:
        # 无论成功失败，都释放 GPU 活跃请求、关闭数据库连接并清理临时文件。
        if gpu_acquired:
            gpu_manager.release()
        db.close()
        _cleanup_files(temp_files)
        logger.info("任务后台流程结束: task_id=%s, 耗时=%.2fs", task_id, time.time() - started_at)
