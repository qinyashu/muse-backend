import base64
import json
import logging
import os
import subprocess
import time
import re
import tempfile
from collections.abc import Iterable
from typing import Any, Optional

import requests

from config import (
    COMFYUI_AUTH_TOKEN,
    COMFYUI_NEGATIVE_PROMPT,
    COMFYUI_PROMPT,
    COMFYUI_REQUEST_MODE,
    COMFYUI_SERVICE_URL,
    DANCE_ALLOW_DEGRADED_OUTPUT,
    DOUYIN_COOKIE,
    DOUYIN_COOKIE_B64,
    EAS_AUTH_TOKEN,
    EAS_DANCE_AUTH_TOKEN,
    EAS_DANCE_SERVICE_URL,
    EAS_SERVICE_URL,
    REPLICATE_API_TOKEN,
    REPLICATE_MODEL_VERSION,
    SING_MODEL_PROVIDER,
    SYNC_API_BASE_URL,
    SYNC_API_KEY,
    SYNC_GENERATION_OPTIONS_JSON,
    SYNC_MODEL,
    SYNC_POLL_INTERVAL_SECONDS,
    SYNC_TIMEOUT_SECONDS,
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


def _probe_video_metadata(video_path: str) -> tuple[int, int, float]:
    """Return width, height, and duration for the first video stream."""
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height:format=duration",
        "-of",
        "json",
        video_path,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        logger.warning(
            "ffprobe failed for %s, using default portrait size: %s",
            video_path,
            _short_error(result.stderr or result.stdout),
        )
        return 720, 1280, 0.0

    try:
        data = json.loads(result.stdout or "{}")
        streams = data.get("streams") if isinstance(data, dict) else []
        width = 0
        height = 0
        if isinstance(streams, list) and streams:
            stream = streams[0]
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)

        duration = 0.0
        video_format = data.get("format") if isinstance(data, dict) else {}
        if isinstance(video_format, dict):
            duration = float(video_format.get("duration") or 0)

        if width > 0 and height > 0:
            return width, height, duration
    except Exception as exc:
        logger.warning("Failed to parse ffprobe output for %s: %s", video_path, exc)

    return 720, 1280, 0.0


def _probe_media_duration(media_path: str) -> float:
    """Return container duration for audio or video files."""
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        media_path,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        logger.warning(
            "ffprobe duration failed for %s: %s",
            media_path,
            _short_error(result.stderr or result.stdout),
        )
        return 0.0

    try:
        return float((result.stdout or "0").strip() or 0)
    except ValueError:
        logger.warning("Failed to parse media duration for %s: %s", media_path, result.stdout)
        return 0.0


def _validate_sing_output_quality(audio_path: str, generated_video_path: str) -> None:
    """Reject singing model outputs that are clearly too short or too low-res."""
    audio_duration = _probe_media_duration(audio_path)
    generated_width, generated_height, generated_duration = _probe_video_metadata(generated_video_path)
    longest_side = max(generated_width, generated_height)
    shortest_side = min(generated_width, generated_height)
    if longest_side and shortest_side and (longest_side < 640 or shortest_side < 360):
        raise RuntimeError(
            "Singing model output resolution is too low: "
            f"{generated_width}x{generated_height}. "
            "Please fix the ComfyUI workflow output selection/resolution before retrying."
        )

    if audio_duration <= 3.0 or generated_duration <= 0:
        return

    minimum_duration = max(2.5, min(audio_duration * 0.75, audio_duration - 1.0))
    logger.info(
        "Singing output duration check: audio=%.2fs, generated=%.2fs, minimum=%.2fs",
        audio_duration,
        generated_duration,
        minimum_duration,
    )
    if generated_duration < minimum_duration:
        raise RuntimeError(
            "Singing model output is too short: "
            f"audio={audio_duration:.2f}s, generated={generated_duration:.2f}s. "
            "Please fix the ComfyUI workflow frame/length settings before retrying."
        )


def _read_video_frame_signature(video_path: str, timestamp: float) -> bytes:
    """Return a tiny grayscale frame used to detect unchanged model outputs."""
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(timestamp, 0.0):.3f}",
        "-i",
        video_path,
        "-frames:v",
        "1",
        "-vf",
        "scale=24:24:flags=area,format=gray",
        "-f",
        "rawvideo",
        "-",
    ]
    result = subprocess.run(command, capture_output=True, timeout=90)
    if result.returncode != 0 or not result.stdout:
        logger.warning(
            "Failed to sample video frame: path=%s, timestamp=%.3f, error=%s",
            video_path,
            timestamp,
            _short_error(result.stderr.decode("utf-8", errors="ignore") if result.stderr else ""),
        )
        return b""
    return result.stdout


def _mean_absolute_byte_delta(first: bytes, second: bytes) -> float:
    """Return the average absolute difference between two byte sequences."""
    length = min(len(first), len(second))
    if length <= 0:
        return 255.0
    return sum(abs(first[index] - second[index]) for index in range(length)) / length


def _videos_look_like_same_content(source_video_path: str, generated_video_path: str) -> bool:
    """Detect model services that re-encode or proxy the original dance video."""
    source_width, source_height, source_duration = _probe_video_metadata(source_video_path)
    generated_width, generated_height, generated_duration = _probe_video_metadata(generated_video_path)
    if source_duration <= 0 or generated_duration <= 0:
        return False

    duration_gap = abs(source_duration - generated_duration)
    if duration_gap > max(2.0, min(source_duration, generated_duration) * 0.08):
        return False

    sample_duration = min(source_duration, generated_duration)
    sample_times = [
        max(0.1, sample_duration * 0.18),
        max(0.1, sample_duration * 0.50),
        max(0.1, sample_duration * 0.82),
    ]
    deltas: list[float] = []
    for timestamp in sample_times:
        source_frame = _read_video_frame_signature(source_video_path, timestamp)
        generated_frame = _read_video_frame_signature(generated_video_path, timestamp)
        if source_frame and generated_frame:
            deltas.append(_mean_absolute_byte_delta(source_frame, generated_frame))

    if len(deltas) < 2:
        return False

    average_delta = sum(deltas) / len(deltas)
    max_delta = max(deltas)
    looks_same = average_delta < 5.0 and max_delta < 9.0
    logger.info(
        "Dance output similarity check: source=%sx%s %.2fs, generated=%sx%s %.2fs, "
        "avg_delta=%.2f, max_delta=%.2f, looks_same=%s",
        source_width,
        source_height,
        source_duration,
        generated_width,
        generated_height,
        generated_duration,
        average_delta,
        max_delta,
        looks_same,
    )
    return looks_same


def _render_dance_composite(photo_path: str, source_video_path: str, output_path: str) -> None:
    """Create a visible dance fallback that keeps the submitted photo as the main subject."""
    width, height, duration = _probe_video_metadata(source_video_path)
    photo_box_w = max(320, int(width * 0.82))
    photo_box_h = max(320, int(height * 0.82))
    filter_complex = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},boxblur=18:1,eq=brightness=-0.05:saturation=0.88[bg];"
        f"[1:v]scale={photo_box_w}:{photo_box_h}:force_original_aspect_ratio=decrease,"
        "setsar=1,format=rgba,colorchannelmixer=aa=0.98[fg];"
        "[bg][fg]overlay="
        "x='(W-w)/2+12*sin(2*PI*t/4)':"
        "y='(H-h)/2+8*cos(2*PI*t/3)':"
        "shortest=1[v]"
    )
    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        source_video_path,
        "-loop",
        "1",
        "-framerate",
        "25",
        "-i",
        photo_path,
        "-filter_complex",
        filter_complex,
        "-map",
        "[v]",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-shortest",
        "-movflags",
        "+faststart",
        output_path,
    ]
    if duration > 0:
        command[-1:-1] = ["-t", f"{duration:.3f}"]
    _run_command(command, "Dance fallback rendering", timeout=1800)


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


def _request_json_or_raise(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 120,
    **kwargs: Any,
) -> dict[str, Any]:
    response = requests.request(method, url, headers=headers or {}, timeout=timeout, **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(
            f"HTTP {response.status_code} from {url}: body={_short_error(response.text)}"
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Non-JSON response from {url}: {_short_error(response.text)}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected JSON response from {url}: {data}")
    return data


def _call_comfyui_sing_model(image_url: str, audio_url: str, task_id: int) -> str:
    if not COMFYUI_SERVICE_URL:
        raise RuntimeError("comfyui sing model service URL is not configured")

    base_url = COMFYUI_SERVICE_URL.rstrip("/")
    headers: dict[str, str] = {}
    if COMFYUI_AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {COMFYUI_AUTH_TOKEN}"

    payload = {
        "image_url": image_url,
        "audio_url": audio_url,
        "prompt": COMFYUI_PROMPT,
        "negative_prompt": COMFYUI_NEGATIVE_PROMPT,
    }
    request_mode = (COMFYUI_REQUEST_MODE or "generate").strip().lower()
    if request_mode not in {"jobs", "async", "job"}:
        return _call_eas_model(
            COMFYUI_SERVICE_URL,
            COMFYUI_AUTH_TOKEN,
            "/generate",
            payload,
            "comfyui sing",
        )

    create_url = f"{base_url}/jobs"
    logger.info("Creating ComfyUI singing job: task_id=%s, url=%s", task_id, create_url)
    created = _request_json_or_raise("POST", create_url, headers=headers, json=payload, timeout=120)
    job_id = created.get("job_id") or created.get("id")
    if not job_id:
        raise RuntimeError(f"ComfyUI jobs API did not return job_id: {created}")
    job_id = str(job_id)

    started_at = time.time()
    timeout_seconds = max(SYNC_TIMEOUT_SECONDS, 3600)
    poll_interval = max(SYNC_POLL_INTERVAL_SECONDS, 5)
    status_url = f"{base_url}/jobs/{job_id}"
    while True:
        elapsed = time.time() - started_at
        if elapsed > timeout_seconds:
            raise RuntimeError(f"ComfyUI singing job timed out after {timeout_seconds}s: job_id={job_id}")

        status_data = _request_json_or_raise("GET", status_url, headers=headers, timeout=120)
        status = str(status_data.get("status") or "").lower()
        logger.info(
            "ComfyUI singing job status: task_id=%s, job_id=%s, status=%s, elapsed=%.1fs",
            task_id,
            job_id,
            status or "<empty>",
            elapsed,
        )

        if status in {"done", "completed", "success", "succeeded"}:
            return _extract_comfyui_job_video_url(status_data)

        if status in {"failed", "error", "cancelled", "canceled"}:
            error = status_data.get("error") or status_data.get("message") or status_data
            raise RuntimeError(
                f"ComfyUI singing job failed: job_id={job_id}, error={_short_error(str(error))}"
            )

        time.sleep(poll_interval)


def _extract_comfyui_job_video_url(data: dict[str, Any]) -> str:
    for key in ("video_url", "output_url", "result_url", "url"):
        value = data.get(key)
        if value:
            return str(value)
    result = data.get("result")
    if isinstance(result, dict):
        for key in ("video_url", "output_url", "result_url", "url"):
            value = result.get(key)
            if value:
                return str(value)
    raise RuntimeError(f"ComfyUI singing job completed without video_url: {data}")


def _sync_api_url(path: str) -> str:
    return f"{SYNC_API_BASE_URL.rstrip('/')}/{path.lstrip('/')}"


def _build_sync_generation_payload(
    image_url: str,
    audio_url: str,
    output_file_name: Optional[str] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": SYNC_MODEL,
        "input": [
            {
                "type": "image",
                "url": image_url,
            },
            {
                "type": "audio",
                "url": audio_url,
            },
        ],
    }

    if output_file_name:
        payload["outputFileName"] = output_file_name

    if SYNC_GENERATION_OPTIONS_JSON:
        try:
            payload["options"] = json.loads(SYNC_GENERATION_OPTIONS_JSON)
        except json.JSONDecodeError as exc:
            raise RuntimeError("SYNC_GENERATION_OPTIONS_JSON is not valid JSON") from exc

    return payload


def _extract_sync_generation_id(data: dict[str, Any]) -> str:
    generation_id = data.get("id") or data.get("generation_id") or data.get("generationId")
    if not generation_id:
        raise RuntimeError(f"Sync Labs did not return a generation id: {data}")
    return str(generation_id)


def _extract_sync_output_url(data: dict[str, Any]) -> str:
    for key in ("outputUrl", "output_url", "video_url", "url"):
        value = data.get(key)
        if value:
            return str(value)
    raise RuntimeError(f"Sync Labs completed without outputUrl: {data}")


def _sync_request_json(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    if not SYNC_API_KEY:
        raise RuntimeError("SYNC_API_KEY is not configured")

    headers = {
        "x-api-key": SYNC_API_KEY,
        "Content-Type": "application/json",
    }
    response = requests.request(
        method,
        _sync_api_url(path),
        headers=headers,
        timeout=kwargs.pop("timeout", 120),
        **kwargs,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Sync Labs API failed: HTTP {response.status_code}, "
            f"body={_short_error(response.text)}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Sync Labs returned non-JSON response: {_short_error(response.text)}") from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"Sync Labs returned unexpected response: {data}")
    return data


def _call_sync_sing_model(image_url: str, audio_url: str, task_id: int) -> str:
    """Create a Sync Labs sync-3 image+audio generation and wait for the MP4 URL."""
    payload = _build_sync_generation_payload(image_url, audio_url, f"xingsu_sing_{task_id}")
    logger.info(
        "Calling Sync Labs singing model: model=%s, image_url=%s, audio_url=%s",
        SYNC_MODEL,
        image_url,
        audio_url,
    )
    created = _sync_request_json("POST", "/v2/generate", json=payload, timeout=120)
    generation_id = _extract_sync_generation_id(created)
    logger.info("Sync Labs generation created: task_id=%s, generation_id=%s", task_id, generation_id)

    started_at = time.time()
    while True:
        elapsed = time.time() - started_at
        if elapsed > SYNC_TIMEOUT_SECONDS:
            raise RuntimeError(
                f"Sync Labs generation timed out after {SYNC_TIMEOUT_SECONDS}s: "
                f"generation_id={generation_id}"
            )

        status_data = _sync_request_json(
            "GET",
            f"/v2/generate/{generation_id}",
            params={"wait": "true", "timeout": "55"},
            timeout=70,
        )
        status = str(status_data.get("status", "")).upper()
        logger.info(
            "Sync Labs generation status: task_id=%s, generation_id=%s, status=%s",
            task_id,
            generation_id,
            status or "<empty>",
        )

        if status == "COMPLETED":
            return _extract_sync_output_url(status_data)

        if status in {"FAILED", "REJECTED", "CANCELED", "CANCELLED"}:
            error = status_data.get("error") or status_data.get("errorCode") or status_data
            raise RuntimeError(
                f"Sync Labs generation {status.lower()}: "
                f"generation_id={generation_id}, error={_short_error(str(error))}"
            )

        time.sleep(max(SYNC_POLL_INTERVAL_SECONDS, 1))


def _extract_replicate_video_url(output: Any) -> str:
    """Extract a downloadable video URL from Replicate output."""
    if output is None:
        raise RuntimeError("Replicate model returned no output")

    if isinstance(output, str):
        video_url = output.strip()
        if video_url:
            return video_url

    if hasattr(output, "url"):
        video_url = str(getattr(output, "url") or "").strip()
        if video_url:
            return video_url

    if isinstance(output, dict):
        for key in ("video_url", "output_url", "result_url", "url"):
            value = output.get(key)
            if value:
                return str(value)

        for key in ("output", "outputs", "result"):
            nested_value = output.get(key)
            if nested_value is not None:
                try:
                    return _extract_replicate_video_url(nested_value)
                except RuntimeError:
                    pass

    if isinstance(output, (list, tuple)):
        for item in output:
            try:
                return _extract_replicate_video_url(item)
            except RuntimeError:
                continue

    if isinstance(output, Iterable) and not isinstance(output, (str, bytes, dict)):
        for item in output:
            try:
                return _extract_replicate_video_url(item)
            except RuntimeError:
                continue

    raise RuntimeError(f"Replicate model did not return a video URL: {output}")


def _call_replicate_sing_model(photo_url: str, audio_url: str, task_id: int) -> str:
    """Call Replicate's MuseTalk model and return the generated video URL."""
    if not REPLICATE_API_TOKEN:
        raise RuntimeError("REPLICATE_API_TOKEN is not configured")

    try:
        os.environ.setdefault("REPLICATE_API_TOKEN", REPLICATE_API_TOKEN)
        import replicate
    except ImportError as exc:
        raise RuntimeError("replicate package is not installed") from exc

    model_id = REPLICATE_MODEL_VERSION or "tencentarc/musetalk:b76242b40d67c76ab6742e987628a2a9ac019e11d56ab96c4e91ce03b79b2787"
    logger.info(
        "Calling Replicate singing model: task_id=%s, model=%s, photo_url=%s, audio_url=%s",
        task_id,
        model_id,
        photo_url,
        audio_url,
    )

    try:
        output = replicate.run(
            model_id,
            input={
                "image": photo_url,
                "audio": audio_url,
            },
        )
        video_url = _extract_replicate_video_url(output)
        logger.info(
            "Replicate singing model succeeded: task_id=%s, model=%s, video_url=%s",
            task_id,
            model_id,
            video_url,
        )
        return video_url
    except Exception as exc:
        error_text = _short_error(str(exc))
        logger.warning(
            "Replicate singing model failed: task_id=%s, model=%s, error=%s",
            task_id,
            model_id,
            error_text,
        )
        raise RuntimeError(f"Replicate singing model failed: {error_text}") from exc


def _call_sing_model(image_url: str, audio_url: str, task_id: int) -> str:
    provider = (SING_MODEL_PROVIDER or "eas").strip().lower()
    if provider == "replicate":
        return _call_replicate_sing_model(image_url, audio_url, task_id)

    if provider == "sync":
        return _call_sync_sing_model(image_url, audio_url, task_id)

    if provider == "comfyui":
        return _call_comfyui_sing_model(image_url, audio_url, task_id)

    if provider != "eas":
        raise RuntimeError(f"Unsupported SING_MODEL_PROVIDER: {provider}")

    return _call_eas_model(
        EAS_SERVICE_URL,
        EAS_AUTH_TOKEN,
        "/generate",
        {
            "image_url": image_url,
            "audio_url": audio_url,
        },
        "sing",
    )


def _should_fallback_dance_model_error(exc: Exception) -> bool:
    """Return True when a dance model error can safely degrade to the source video."""
    if isinstance(exc, requests.RequestException):
        return True

    error_text = _short_error(str(exc)).lower()
    http_status_match = re.search(r"http\s+(\d{3})", error_text)
    if http_status_match and int(http_status_match.group(1)) >= 500:
        return True

    fallback_markers = (
        "xformers",
        "modulenotfounderror",
        "did not return video_url",
        "returned non-json response",
        "model service url is not configured",
        "connection refused",
        "failed to establish a new connection",
        "read timed out",
        "timed out",
        "timeout",
    )
    return any(marker in error_text for marker in fallback_markers)


def _should_render_local_dance_result(generated_video_url: str, source_video_url: str) -> bool:
    """Return True when a dance response is effectively the source video."""
    generated_video_url = generated_video_url.strip()
    source_video_url = source_video_url.strip()
    return not generated_video_url or generated_video_url == source_video_url


def _handle_unavailable_dance_model(
    reason: str,
    temp_photo_path: str,
    source_video_path: str,
    generated_video_path: str,
) -> bool:
    """Fail by default when the real dance model cannot produce motion transfer."""
    if not DANCE_ALLOW_DEGRADED_OUTPUT:
        raise RuntimeError(reason)

    if not os.path.exists(temp_photo_path):
        raise RuntimeError("Local dance fallback is missing the uploaded photo")

    logger.warning("DANCE_ALLOW_DEGRADED_OUTPUT is enabled; rendering degraded dance output: %s", reason)
    _render_dance_composite(temp_photo_path, source_video_path, generated_video_path)
    return True


def _runtime_temp_path(filename: str) -> str:
    return os.path.join(tempfile.gettempdir(), filename)


def process_task(
    task_id: int,
    temp_photo_path: str = "",
    photo_suffix: str = ".jpg",
    temp_video_path: str = "",
    video_suffix: str = ".mp4",
) -> None:
    """Process a generation task, then upload the final output video to OSS."""
    started_at = time.time()
    db = SessionLocal()
    task: Task | None = None

    video_path = _runtime_temp_path(f"{task_id}.mp4")
    audio_path = _runtime_temp_path(f"{task_id}.mp3")
    generated_video_path = _runtime_temp_path(f"{task_id}_gen.mp4")
    douyin_cookie_path = _runtime_temp_path(f"{task_id}_douyin_cookies.txt")
    temp_files = [video_path, audio_path, generated_video_path, douyin_cookie_path]
    if temp_photo_path:
        temp_files.append(temp_photo_path)
    if temp_video_path:
        temp_files.append(temp_video_path)

    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if task is None:
            logger.error("Task not found: task_id=%s", task_id)
            return

        task.status = "processing"
        task.error_message = None
        db.commit()
        logger.info("Task marked as processing: task_id=%s, type=%s", task_id, task.type)

        if temp_photo_path and os.path.exists(temp_photo_path) and not task.photo_url:
            safe_photo_suffix = photo_suffix or ".jpg"
            photo_url = upload_file(temp_photo_path, f"photos/{task_id}{safe_photo_suffix}")
            if not photo_url:
                raise RuntimeError("Photo upload to OSS failed")
            task.photo_url = photo_url
            db.commit()
            logger.info("Photo uploaded to OSS: task_id=%s, photo_url=%s", task_id, photo_url)

        if temp_video_path and os.path.exists(temp_video_path) and not task.source_video_url:
            safe_video_suffix = video_suffix or ".mp4"
            source_video_url = upload_file(temp_video_path, f"source_videos/{task_id}{safe_video_suffix}")
            if not source_video_url:
                raise RuntimeError("Source video upload to OSS failed")
            task.source_video_url = source_video_url
            db.commit()
            logger.info("Source video uploaded to OSS: task_id=%s, source_video_url=%s", task_id, source_video_url)

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

        generated_video_from_model = False
        if task.type == "dance":
            input_media_url = source_video_url
            try:
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
                generated_video_from_model = not _should_render_local_dance_result(generated_video_url, source_video_url)
                if not generated_video_from_model:
                    reason = "Dance model did not perform motion transfer: it returned the original source video URL"
                    logger.error(
                        "%s, task_id=%s, service_url=%s",
                        reason,
                        task_id,
                        EAS_DANCE_SERVICE_URL or "<unset>",
                    )
                    _handle_unavailable_dance_model(
                        reason,
                        temp_photo_path,
                        video_path,
                        generated_video_path,
                    )
            except Exception as exc:
                if not _should_fallback_dance_model_error(exc) or not DANCE_ALLOW_DEGRADED_OUTPUT:
                    raise

                logger.warning(
                    "Dance model failed, rendering degraded dance output: task_id=%s, service_url=%s, error=%s",
                    task_id,
                    EAS_DANCE_SERVICE_URL or "<unset>",
                    _short_error(str(exc)),
                )
                _handle_unavailable_dance_model(
                    f"Dance model failed: {_short_error(str(exc))}",
                    temp_photo_path,
                    video_path,
                    generated_video_path,
                )

            if not generated_video_from_model:
                logger.info(
                    "Degraded dance output rendered: task_id=%s, generated_video_path=%s",
                    task_id,
                    generated_video_path,
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

            generated_video_url = _call_sing_model(task.photo_url, input_media_url, task_id)
            generated_video_from_model = True

        if generated_video_from_model:
            logger.info("Model generated video URL: %s", generated_video_url)
            _download_file(generated_video_url, generated_video_path)
            if task.type == "dance":
                if _videos_look_like_same_content(video_path, generated_video_path):
                    raise RuntimeError(
                        "Dance model did not perform motion transfer: generated video content matches the source video"
                    )
            else:
                _validate_sing_output_quality(audio_path, generated_video_path)

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
