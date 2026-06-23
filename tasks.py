import base64
import logging
import os
import subprocess
import time

import requests

from config import AUTODL_MODEL_API_URL, DOUYIN_COOKIE, DOUYIN_COOKIE_B64
from database import SessionLocal
from gpu_manager import gpu_manager
from models import Task
from oss_utils import upload_file


logger = logging.getLogger(__name__)

DOUYIN_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


def _short_error(message: str, limit: int = 800) -> str:
    """压缩错误信息，避免数据库里写入过长日志。"""
    clean_message = " ".join(str(message).split())
    if len(clean_message) <= limit:
        return clean_message
    return f"{clean_message[:limit]}..."


def _get_douyin_cookie() -> str:
    """读取抖音 Cookie，优先使用明文变量，其次使用 Base64 变量。"""
    if DOUYIN_COOKIE:
        return DOUYIN_COOKIE

    if not DOUYIN_COOKIE_B64:
        return ""

    try:
        return base64.b64decode(DOUYIN_COOKIE_B64).decode("utf-8")
    except Exception as exc:
        logger.warning("DOUYIN_COOKIE_B64 解码失败: %s", exc)
        return ""


def _redact_command(command: list[str]) -> list[str]:
    """日志打印命令时隐藏 Cookie，避免敏感信息进入 Railway 日志。"""
    redacted: list[str] = []
    hide_next = False
    for item in command:
        if hide_next:
            redacted.append("Cookie: <redacted>")
            hide_next = False
            continue

        redacted.append(item)
        if item in {"--add-header", "--add-headers"}:
            hide_next = True

    return redacted


def _run_command(command: list[str], step_name: str, timeout: int = 300) -> None:
    """执行外部命令，并在失败时抛出包含 stderr 的异常。"""
    logger.info("开始执行%s命令: %s", step_name, _redact_command(command))
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        error_text = result.stderr or result.stdout or f"退出码 {result.returncode}"
        logger.error("%s命令执行失败: %s", step_name, error_text)
        raise RuntimeError(f"{step_name}失败: {_short_error(error_text)}")
    logger.info("%s命令执行成功", step_name)


def _download_file(url: str, local_path: str) -> None:
    """使用 requests 将远程视频下载到本地临时路径。"""
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


def _write_douyin_cookie_file(cookie_header: str, cookie_path: str) -> None:
    """把浏览器复制出的 Cookie 字符串转换为 yt-dlp 可读取的 Netscape cookies 文件。"""
    cookie_text = cookie_header.strip()
    if cookie_text.lower().startswith("cookie:"):
        cookie_text = cookie_text.split(":", 1)[1].strip()

    cookie_parts = [part.strip() for part in cookie_text.split(";") if "=" in part]
    if not cookie_parts:
        raise RuntimeError("DOUYIN_COOKIE 格式无效，未解析到 name=value")

    lines = [
        "# Netscape HTTP Cookie File",
        "# Generated from DOUYIN_COOKIE by backend.",
    ]
    for domain in (".douyin.com", "www.douyin.com"):
        for part in cookie_parts:
            name, value = part.split("=", 1)
            name = name.strip()
            value = value.strip().replace("\r", "").replace("\n", "").replace("\t", " ")
            if not name:
                continue
            lines.append(f"{domain}\tTRUE\t/\tTRUE\t2147483647\t{name}\t{value}")

    with open(cookie_path, "w", encoding="utf-8") as file_obj:
        file_obj.write("\n".join(lines) + "\n")


def _build_douyin_download_command(video_path: str, douyin_url: str, cookie_path: str) -> list[str]:
    """构建 yt-dlp 下载命令，支持抖音 Cookie 和常见浏览器请求头。"""
    command = [
        "yt-dlp",
        "--no-playlist",
        "--retries",
        "3",
        "--force-overwrites",
        "--user-agent",
        DOUYIN_USER_AGENT,
        "--referer",
        "https://www.douyin.com/",
        "-o",
        video_path,
    ]

    douyin_cookie = _get_douyin_cookie()
    if douyin_cookie:
        _write_douyin_cookie_file(douyin_cookie, cookie_path)
        command.extend(["--cookies", cookie_path])
        logger.info("已配置抖音 Cookie，将通过 yt-dlp cookies 文件下载视频")
    else:
        logger.warning("未配置 DOUYIN_COOKIE，抖音链接可能因需要登录 Cookie 而下载失败")

    command.append(douyin_url)
    return command


def process_task(task_id: int) -> None:
    """后台处理任务：下载抖音音频，调用模型生成视频，并上传最终结果到 OSS。"""
    started_at = time.time()
    db = SessionLocal()
    task: Task | None = None
    gpu_acquired = False

    video_path = f"/tmp/{task_id}.mp4"
    audio_path = f"/tmp/{task_id}.mp3"
    generated_video_path = f"/tmp/{task_id}_gen.mp4"
    douyin_cookie_path = f"/tmp/{task_id}_douyin_cookies.txt"
    temp_files = [video_path, audio_path, generated_video_path, douyin_cookie_path]

    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if task is None:
            logger.error("未找到任务记录: task_id=%s", task_id)
            return

        task.status = "processing"
        task.error_message = None
        db.commit()
        logger.info("任务已标记为 processing: task_id=%s", task_id)

        manage_autodl_instance = not AUTODL_MODEL_API_URL
        if manage_autodl_instance:
            gpu_manager.acquire()
            gpu_acquired = True

            if not gpu_manager.start_instance():
                raise RuntimeError("GPU 实例启动失败")
        else:
            logger.info("已配置 AUTODL_MODEL_API_URL，跳过 AutoDL 官方开机 API")

        if not task.douyin_url:
            raise ValueError("任务缺少 douyin_url")
        if not task.photo_url:
            raise ValueError("任务缺少 photo_url")

        _run_command(
            _build_douyin_download_command(video_path, task.douyin_url, douyin_cookie_path),
            "抖音视频下载",
            timeout=420,
        )

        _run_command(
            ["ffmpeg", "-y", "-i", video_path, "-q:a", "0", "-map", "a", audio_path],
            "音频提取",
            timeout=300,
        )

        audio_url = upload_file(audio_path, f"audios/{task_id}.mp3")
        if not audio_url:
            raise RuntimeError("音频上传 OSS 失败")
        logger.info("音频已上传 OSS: %s", audio_url)

        generated_video_url = gpu_manager.call_model(task.photo_url, audio_url)
        if not generated_video_url:
            model_error = getattr(gpu_manager, "last_error", "") or "模型未返回生成视频 URL"
            raise RuntimeError(model_error)
        logger.info("模型生成视频 URL: %s", generated_video_url)

        _download_file(generated_video_url, generated_video_path)

        output_url = upload_file(generated_video_path, f"outputs/{task_id}.mp4")
        if not output_url:
            raise RuntimeError("最终视频上传 OSS 失败")
        logger.info("最终视频已上传 OSS: %s", output_url)

        task.audio_url = audio_url
        task.output_url = output_url
        task.status = "done"
        task.error_message = None
        db.commit()
        logger.info("任务处理完成: task_id=%s, output_url=%s", task_id, output_url)

    except Exception as exc:
        error_message = _short_error(str(exc))
        logger.exception("任务处理失败: task_id=%s, 错误: %s", task_id, error_message)
        db.rollback()
        if task is not None:
            task.status = "failed"
            task.error_message = error_message
            db.commit()
            logger.info("任务已标记为 failed: task_id=%s", task_id)

    finally:
        if gpu_acquired:
            gpu_manager.release()
        db.close()
        _cleanup_files(temp_files)
        logger.info("任务后台流程结束: task_id=%s, 耗时=%.2fs", task_id, time.time() - started_at)
