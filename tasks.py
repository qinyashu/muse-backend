import base64
import logging
import os
import subprocess
import time

import requests

from config import (
    DOUYIN_COOKIE,
    DOUYIN_COOKIE_B64,
    EAS_AUTH_TOKEN,
    EAS_DANCE_AUTH_TOKEN,
    EAS_DANCE_SERVICE_URL,
    EAS_SERVICE_URL,
)
from database import SessionLocal
from models import Task
from oss_utils import upload_file


logger = logging.getLogger(__name__)

DOUYIN_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


def _short_error(message: str, limit: int = 800) -> str:
    """Compress an error message before storing it in the database."""
    clean_message = " ".join(str(message).split())
    if len(clean_message) <= limit:
        return clean_message
    return f"{clean_message[:limit]}..."


def _get_douyin_cookie() -> str:
    """Read Douyin cookie from plain text env first, then Base64 env."""
    if DOUYIN_COOKIE:
        return DOUYIN_COOKIE

    if not DOUYIN_COOKIE_B64:
        return ""

    try:
        return base64.b64decode(DOUYIN_COOKIE_B64).decode("utf-8")
    except Exception as exc:
        logger.warning("Failed to decode DOUYIN_COOKIE_B64: %s", exc)
        return ""


def _redact_command(command: list[str]) -> list[str]:
    """Hide Cookie content when logging shell commands."""
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
    """Run an external command and raise a concise error on failure."""
    logger.info("Running %s command: %s", step_name, _redact_command(command))
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        error_text = result.stderr or result.stdout or f"exit code {result.returncode}"
        logger.error("%s command failed: %s", step_name, error_text)
        raise RuntimeError(f"{step_name} failed: {_short_error(error_text)}")
    logger.info("%s command succeeded", step_name)


def _download_file(url: str, local_path: str) -> None:
    """Download a remote file to a local temporary path."""
    logger.info("Downloading generated video: %s -> %s", url, local_path)
    response = requests.get(url, stream=True, timeout=600)
    response.raise_for_status()

    with open(local_path, "wb") as file_obj:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                file_obj.write(chunk)

    logger.info("Download completed: %s", local_path)


def _cleanup_files(paths: list[str]) -> None:
    """Remove temporary files produced during task processing."""
    for path in paths:
        try:
            if os.path.exists(path):
                os.remove(path)
                logger.info("Removed temporary file: %s", path)
        except Exception as exc:
            logger.warning("Failed to remove temporary file %s: %s", path, exc)


def _write_douyin_cookie_file(cookie_header: str, cookie_path: str) -> None:
    """Convert browser Cookie header text into a Netscape cookies file for yt-dlp."""
    cookie_text = cookie_header.strip()
    if cookie_text.lower().startswith("cookie:"):
        cookie_text = cookie_text.split(":", 1)[1].strip()

    cookie_parts = [part.strip() for part in cookie_text.split(";") if "=" in part]
    if not cookie_parts:
        raise RuntimeError("DOUYIN_COOKIE format is invalid; no name=value pair was parsed")

    lines = [
        "# Netscape HTTP Cookie File",
        "# Generated from DOUYIN_COOKIE by backend.",
    ]
    cookie_domains = (
        (".douyin.com", "TRUE"),
        ("www.douyin.com", "FALSE"),
    )
    for domain, include_subdomains in cookie_domains:
        for part in cookie_parts:
            name, value = part.split("=", 1)
            name = name.strip()
            value = value.strip().replace("\r", "").replace("\n", "").replace("\t", " ")
            if not name:
                continue
            lines.append(f"{domain}\t{include_subdomains}\t/\tTRUE\t2147483647\t{name}\t{value}")

    with open(cookie_path, "w", encoding="utf-8") as file_obj:
        file_obj.write("\n".join(lines) + "\n")


def _build_douyin_download_command(video_path: str, douyin_url: str, cookie_path: str) -> list[str]:
    """Build a yt-dlp command with optional Douyin cookie support."""
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
        command.extend(
            [
                "--cookies",
                cookie_path,
                "--add-headers",
                f"Cookie: {douyin_cookie}",
            ]
        )
        logger.info("Douyin cookie is configured for yt-dlp")
    else:
        logger.warning("DOUYIN_COOKIE is not configured; Douyin download may fail")

    command.append(douyin_url)
    return command


def _extract_model_video_url(response: requests.Response) -> str:
    """Extract generated video URL from an EAS JSON response."""
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Model API returned non-JSON response: {_short_error(response.text)}") from exc

    if isinstance(data, dict):
        for key in ("video_url", "output_url", "result_url", "url"):
            value = data.get(key)
            if value:
                return str(value)

        error = data.get("error") or data.get("message") or data.get("detail")
        if error:
            raise RuntimeError(f"Model API error: {error}")

    raise RuntimeError(f"Model API did not return video_url: {data}")


def _call_eas_model(
    service_url: str,
    auth_token: str,
    endpoint: str,
    payload: dict[str, str],
    task_type: str,
) -> str:
    """Call a PAI-EAS model endpoint and return the generated video URL."""
    if not service_url:
        raise RuntimeError(f"{task_type} model service URL is not configured")

    base_url = service_url.rstrip("/")
    if "/api/predict/" in base_url:
        url = base_url
    else:
        url = f"{base_url}/{endpoint.lstrip('/')}"

    headers: dict[str, str] = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    logger.info("Calling %s model API at %s", task_type, url)
    response = requests.post(url, json=payload, headers=headers, timeout=1800)
    if response.status_code >= 400:
        raise RuntimeError(
            f"{task_type} model API failed: HTTP {response.status_code}, "
            f"body={_short_error(response.text)}"
        )

    return _extract_model_video_url(response)


def process_task(task_id: int) -> None:
    """Process a generation task, then upload the final output video to OSS."""
    started_at = time.time()
    db = SessionLocal()
    task: Task | None = None

    video_path = f"/tmp/{task_id}.mp4"
    audio_path = f"/tmp/{task_id}.mp3"
    generated_video_path = f"/tmp/{task_id}_gen.mp4"
    douyin_cookie_path = f"/tmp/{task_id}_douyin_cookies.txt"
    temp_files = [video_path, audio_path, generated_video_path, douyin_cookie_path]

    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if task is None:
            logger.error("Task not found: task_id=%s", task_id)
            return

        task.status = "processing"
        task.error_message = None
        db.commit()
        logger.info("Task marked as processing: task_id=%s, type=%s", task_id, task.type)

        if not task.photo_url:
            raise ValueError("Task is missing photo_url")

        if task.source_video_url:
            logger.info("Task has uploaded source video, skipping Douyin download: task_id=%s", task_id)
            _download_file(task.source_video_url, video_path)
            source_video_url = task.source_video_url
        else:
            if not task.douyin_url:
                raise ValueError("Task is missing douyin_url or source_video_url")

            _run_command(
                _build_douyin_download_command(video_path, task.douyin_url, douyin_cookie_path),
                "Douyin video download",
                timeout=420,
            )
            source_video_url = upload_file(video_path, f"source_videos/{task_id}.mp4")
            if not source_video_url:
                raise RuntimeError("Source video upload to OSS failed")
            logger.info("Source video uploaded to OSS: %s", source_video_url)

        if task.type == "dance":
            input_media_url = source_video_url
            generated_video_url = _call_eas_model(
                EAS_DANCE_SERVICE_URL,
                EAS_DANCE_AUTH_TOKEN,
                "/dance",
                {
                    "image_url": task.photo_url,
                    "video_url": input_media_url,
                },
                "dance",
            )
        else:
            _run_command(
                ["ffmpeg", "-y", "-i", video_path, "-q:a", "0", "-map", "a", audio_path],
                "Audio extraction",
                timeout=300,
            )

            input_media_url = upload_file(audio_path, f"audios/{task_id}.mp3")
            if not input_media_url:
                raise RuntimeError("Audio upload to OSS failed")
            logger.info("Audio uploaded to OSS: %s", input_media_url)

            generated_video_url = _call_eas_model(
                EAS_SERVICE_URL,
                EAS_AUTH_TOKEN,
                "/generate",
                {
                    "image_url": task.photo_url,
                    "audio_url": input_media_url,
                },
                "sing",
            )

        logger.info("Model generated video URL: %s", generated_video_url)
        _download_file(generated_video_url, generated_video_path)

        output_url = upload_file(generated_video_path, f"outputs/{task_id}.mp4")
        if not output_url:
            raise RuntimeError("Final video upload to OSS failed")
        logger.info("Final video uploaded to OSS: %s", output_url)

        task.audio_url = input_media_url
        task.output_url = output_url
        task.status = "done"
        task.error_message = None
        db.commit()
        logger.info("Task completed: task_id=%s, output_url=%s", task_id, output_url)

    except Exception as exc:
        error_message = _short_error(str(exc))
        logger.exception("Task failed: task_id=%s, error=%s", task_id, error_message)
        db.rollback()
        if task is not None:
            task.status = "failed"
            task.error_message = error_message
            db.commit()
            logger.info("Task marked as failed: task_id=%s", task_id)

    finally:
        db.close()
        _cleanup_files(temp_files)
        logger.info("Task background flow finished: task_id=%s, elapsed=%.2fs", task_id, time.time() - started_at)
