# 需要先安装 oss2 库（已在 requirements.txt 中包含）。
import logging

import oss2

from config import (
    OSS_ACCESS_KEY_ID,
    OSS_ACCESS_KEY_SECRET,
    OSS_BUCKET_NAME,
    OSS_ENDPOINT,
)


logger = logging.getLogger(__name__)


def _get_bucket() -> oss2.Bucket:
    """创建并返回 OSS Bucket 实例。"""
    auth = oss2.Auth(OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET)
    return oss2.Bucket(auth, OSS_ENDPOINT, OSS_BUCKET_NAME)


def _build_public_url(oss_path: str) -> str:
    """根据 bucket、endpoint 和 OSS 路径生成公开访问 URL。"""
    endpoint = OSS_ENDPOINT.replace("https://", "").replace("http://", "").rstrip("/")
    path = oss_path.lstrip("/")
    return f"https://{OSS_BUCKET_NAME}.{endpoint}/{path}"


def upload_file(local_path: str, oss_path: str) -> str:
    """
    上传本地文件到阿里云 OSS。

    参数:
        local_path: 本地文件路径。
        oss_path: 上传到 OSS 后的对象路径。

    返回:
        上传成功后的公开访问 URL；失败时返回空字符串。
    """
    try:
        bucket = _get_bucket()
        bucket.put_object_from_file(oss_path, local_path)
        url = _build_public_url(oss_path)
        logger.info("OSS 文件上传成功: %s -> %s", local_path, url)
        return url
    except Exception as exc:
        logger.exception("OSS 文件上传失败: %s -> %s, 错误: %s", local_path, oss_path, exc)
        return ""


def download_file(oss_path: str, local_path: str) -> bool:
    """
    从阿里云 OSS 下载文件到本地。

    参数:
        oss_path: OSS 上的对象路径。
        local_path: 下载后的本地保存路径。

    返回:
        下载成功返回 True，失败返回 False。
    """
    try:
        bucket = _get_bucket()
        bucket.get_object_to_file(oss_path, local_path)
        logger.info("OSS 文件下载成功: %s -> %s", oss_path, local_path)
        return True
    except Exception as exc:
        logger.exception("OSS 文件下载失败: %s -> %s, 错误: %s", oss_path, local_path, exc)
        return False
