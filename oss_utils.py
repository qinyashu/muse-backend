from __future__ import annotations

import logging
import os
import re
import tempfile

import oss2
from oss2 import resumable
from oss2.exceptions import AccessDenied

from config import (
    OSS_ACCESS_KEY_ID,
    OSS_ACCESS_KEY_SECRET,
    OSS_BUCKET_NAME,
    OSS_ENDPOINT,
)


logger = logging.getLogger(__name__)

_ENDPOINT_RE = re.compile(r"Endpoint['\"]?: ['\"]?([^'\"}]+)", re.IGNORECASE)
_RESUMABLE_THRESHOLD = 10 * 1024 * 1024
_RESUMABLE_PART_SIZE = 10 * 1024 * 1024
_RESUMABLE_STORE_DIR = "xingsu_oss_resumable"
_FORCE_RESUMABLE_PREFIXES = (
    "source_videos/",
    "outputs/",
    "comfyui_sing_outputs/",
    "comfyui_dance_outputs/",
)
_TRANSIENT_ERROR_MARKERS = (
    "timeout",
    "timed out",
    "connection aborted",
    "connection reset",
    "broken pipe",
    "requesterror",
    "temporarily unavailable",
)


def _validate_config() -> None:
    missing = [
        name
        for name, value in {
            "OSS_ACCESS_KEY_ID": OSS_ACCESS_KEY_ID,
            "OSS_ACCESS_KEY_SECRET": OSS_ACCESS_KEY_SECRET,
            "OSS_ENDPOINT": OSS_ENDPOINT,
            "OSS_BUCKET_NAME": OSS_BUCKET_NAME,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"OSS config missing: {', '.join(missing)}")


def _normalize_endpoint(endpoint: str) -> str:
    return endpoint.replace("https://", "").replace("http://", "").rstrip("/")


def _candidate_endpoints(endpoint: str) -> list[str]:
    clean = _normalize_endpoint(endpoint)
    candidates = [clean]
    if "guangzhou" in clean:
        candidates.append(clean.replace("guangzhou", "hangzhou"))
    elif "hangzhou" in clean:
        candidates.append(clean.replace("hangzhou", "guangzhou"))

    unique: list[str] = []
    for item in candidates:
        if item and item not in unique:
            unique.append(item)
    return unique


def _extract_endpoint_from_error(exc: Exception) -> str:
    details = getattr(exc, "details", None)
    if isinstance(details, dict):
        endpoint = details.get("Endpoint") or details.get("endpoint")
        if endpoint:
            return _normalize_endpoint(str(endpoint))

    match = _ENDPOINT_RE.search(str(exc))
    if match:
        return _normalize_endpoint(match.group(1))

    return ""


def _build_bucket(endpoint: str | None = None) -> oss2.Bucket:
    _validate_config()
    auth = oss2.Auth(OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET)
    return oss2.Bucket(
        auth,
        endpoint or OSS_ENDPOINT,
        OSS_BUCKET_NAME,
        connect_timeout=180,
    )


def _public_url(endpoint: str, oss_path: str) -> str:
    clean_endpoint = _normalize_endpoint(endpoint)
    return f"https://{OSS_BUCKET_NAME}.{clean_endpoint}/{oss_path.lstrip('/')}"


def _should_use_resumable_upload(local_path: str, oss_path: str) -> bool:
    normalized_path = oss_path.lstrip("/").lower()
    if normalized_path.startswith(_FORCE_RESUMABLE_PREFIXES):
        return True
    return os.path.getsize(local_path) >= _RESUMABLE_THRESHOLD


def _is_transient_upload_error(exc: Exception) -> bool:
    error_text = str(exc).lower()
    return any(marker in error_text for marker in _TRANSIENT_ERROR_MARKERS)


def _upload_with_endpoint(
    endpoint: str,
    local_path: str,
    oss_path: str,
    use_resumable: bool,
) -> str:
    bucket = _build_bucket(endpoint)
    file_size = os.path.getsize(local_path)

    if use_resumable:
        store = resumable.ResumableStore(root=tempfile.gettempdir(), dir=_RESUMABLE_STORE_DIR)
        resumable.resumable_upload(
            bucket,
            oss_path,
            local_path,
            store=store,
            multipart_threshold=_RESUMABLE_THRESHOLD,
            part_size=_RESUMABLE_PART_SIZE,
            num_threads=4,
        )
        mode = "resumable"
    else:
        bucket.put_object_from_file(oss_path, local_path)
        mode = "direct"

    url = _public_url(endpoint, oss_path)
    logger.info(
        "OSS upload succeeded: mode=%s endpoint=%s size=%s path=%s url=%s",
        mode,
        _normalize_endpoint(endpoint),
        file_size,
        oss_path,
        url,
    )
    return url


def _download_with_endpoint(endpoint: str, oss_path: str, local_path: str) -> None:
    bucket = _build_bucket(endpoint)
    bucket.get_object_to_file(oss_path, local_path)


def upload_file(local_path: str, oss_path: str) -> str:
    """Upload a file to OSS and return the public URL.

    If the configured endpoint is wrong, retry once with the endpoint hinted by
    OSS's AccessDenied response.
    """

    last_error: Exception | None = None
    attempted = _candidate_endpoints(OSS_ENDPOINT)
    prefer_resumable = _should_use_resumable_upload(local_path, oss_path)
    mode_sequence = (True,) if prefer_resumable else (False, True)

    for endpoint in attempted:
        for use_resumable in mode_sequence:
            try:
                url = _upload_with_endpoint(endpoint, local_path, oss_path, use_resumable)
                if _normalize_endpoint(endpoint) != _normalize_endpoint(OSS_ENDPOINT):
                    logger.warning(
                        "OSS upload used fallback endpoint: configured=%s, used=%s, url=%s",
                        OSS_ENDPOINT,
                        endpoint,
                        url,
                    )
                return url
            except AccessDenied as exc:
                last_error = exc
                hinted_endpoint = _extract_endpoint_from_error(exc)
                if hinted_endpoint and hinted_endpoint not in attempted:
                    for hinted_use_resumable in mode_sequence:
                        try:
                            url = _upload_with_endpoint(
                                hinted_endpoint,
                                local_path,
                                oss_path,
                                hinted_use_resumable,
                            )
                            logger.warning(
                                "OSS endpoint corrected automatically: configured=%s, used=%s, url=%s",
                                endpoint,
                                hinted_endpoint,
                                url,
                            )
                            return url
                        except Exception as retry_exc:
                            last_error = retry_exc
                            logger.warning(
                                "OSS retry with hinted endpoint failed: hinted=%s, mode=%s, error=%s",
                                hinted_endpoint,
                                "resumable" if hinted_use_resumable else "direct",
                                retry_exc,
                            )
                    continue

                logger.warning("OSS upload denied with endpoint=%s: %s", endpoint, exc)
                break
            except Exception as exc:
                last_error = exc
                if not use_resumable and _is_transient_upload_error(exc):
                    logger.warning(
                        "OSS direct upload transient error, retrying with resumable: endpoint=%s, path=%s, error=%s",
                        endpoint,
                        oss_path,
                        exc,
                    )
                    continue

                logger.exception(
                    "OSS file upload failed: endpoint=%s, mode=%s, file=%s, path=%s",
                    endpoint,
                    "resumable" if use_resumable else "direct",
                    local_path,
                    oss_path,
                )

    logger.error("OSS file upload failed: %s -> %s, last error=%s", local_path, oss_path, last_error)
    return ""


def download_file(oss_path: str, local_path: str) -> bool:
    """Download a file from OSS into local_path."""

    last_error: Exception | None = None
    attempted = _candidate_endpoints(OSS_ENDPOINT)

    for endpoint in attempted:
        try:
            _download_with_endpoint(endpoint, oss_path, local_path)
            logger.info("OSS file download succeeded: %s -> %s", oss_path, local_path)
            return True
        except AccessDenied as exc:
            last_error = exc
            hinted_endpoint = _extract_endpoint_from_error(exc)
            if hinted_endpoint and hinted_endpoint not in attempted:
                try:
                    _download_with_endpoint(hinted_endpoint, oss_path, local_path)
                    logger.warning(
                        "OSS download endpoint corrected automatically: configured=%s, used=%s",
                        endpoint,
                        hinted_endpoint,
                    )
                    return True
                except Exception as retry_exc:
                    last_error = retry_exc
                    continue
        except Exception as exc:
            last_error = exc
            logger.exception("OSS file download failed: %s -> %s", oss_path, local_path)

    logger.error("OSS file download failed: %s -> %s, last error=%s", oss_path, local_path, last_error)
    return False
