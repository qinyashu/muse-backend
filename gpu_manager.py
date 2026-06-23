import logging
import threading
import time
from typing import Any

import requests

from config import AUTODL_INSTANCE_ID, AUTODL_MODEL_API_URL, AUTODL_TOKEN


logger = logging.getLogger(__name__)


class GPUManager:
    """管理 AutoDL 实例开关机，并封装模型 API 调用。"""

    INFO_URL = "https://api.autodl.com/v1/instance/info"
    START_URL = "https://api.autodl.com/v1/instance/start"
    STOP_URL = "https://api.autodl.com/v1/instance/stop"

    def __init__(self) -> None:
        # 当前正在使用 GPU 的请求数量。
        self.active_requests = 0
        # 保护 active_requests 和 shutdown_timer 的线程锁。
        self.lock = threading.Lock()
        # 延迟关机定时器；有新请求进入时会取消。
        self.shutdown_timer: threading.Timer | None = None
        # 最近一次模型调用失败原因，供后台任务写入数据库。
        self.last_error = ""

    def _headers(self) -> dict[str, str]:
        """生成 AutoDL API 请求头。"""
        return {"Authorization": f"Bearer {AUTODL_TOKEN}"}

    def _extract_status(self, payload: dict[str, Any]) -> str:
        """从 AutoDL 返回数据中提取实例状态。"""
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("status", "instance_status", "machine_status"):
                status = data.get(key)
                if status:
                    return str(status)

            instance = data.get("instance")
            if isinstance(instance, dict) and instance.get("status"):
                return str(instance["status"])

        status = payload.get("status")
        if status:
            return str(status)

        return "unknown"

    def _extract_video_url(self, payload: dict[str, Any]) -> str:
        """从模型 API 返回数据中提取生成视频 URL。"""
        for key in ("video_url", "output_url", "url", "temporary_url", "temp_url"):
            value = payload.get(key)
            if value:
                return str(value)

        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("video_url", "output_url", "url", "temporary_url", "temp_url"):
                value = data.get(key)
                if value:
                    return str(value)
        elif isinstance(data, str):
            return data

        return ""

    def get_status(self) -> str:
        """查询 AutoDL 实例状态，返回状态字符串。"""
        try:
            response = requests.get(
                self.INFO_URL,
                params={"instance_id": AUTODL_INSTANCE_ID},
                headers=self._headers(),
                timeout=15,
            )
            response.raise_for_status()
            status = self._extract_status(response.json())
            logger.info("AutoDL 实例状态: %s", status)
            return status
        except Exception as exc:
            logger.exception("查询 AutoDL 实例状态失败: %s", exc)
            return "unknown"

    def start_instance(self) -> bool:
        """启动 AutoDL 实例，并等待状态变为 running。"""
        try:
            response = requests.post(
                self.START_URL,
                json={"instance_id": AUTODL_INSTANCE_ID},
                headers=self._headers(),
                timeout=15,
            )
            response.raise_for_status()
            logger.info("AutoDL 实例启动请求已发送: %s", AUTODL_INSTANCE_ID)
        except Exception as exc:
            logger.exception("启动 AutoDL 实例失败: %s", exc)
            return False

        deadline = time.time() + 120
        while time.time() < deadline:
            status = self.get_status().lower()
            if status == "running":
                logger.info("AutoDL 实例已运行: %s", AUTODL_INSTANCE_ID)
                return True
            time.sleep(5)

        logger.warning("AutoDL 实例启动超时: %s", AUTODL_INSTANCE_ID)
        return False

    def stop_instance(self) -> None:
        """关闭 AutoDL 实例。"""
        try:
            response = requests.post(
                self.STOP_URL,
                json={"instance_id": AUTODL_INSTANCE_ID},
                headers=self._headers(),
                timeout=15,
            )
            response.raise_for_status()
            logger.info("AutoDL 实例关机请求已发送: %s", AUTODL_INSTANCE_ID)
        except Exception as exc:
            logger.exception("关闭 AutoDL 实例失败: %s", exc)

    def call_model(self, image_url: str, audio_url: str) -> str:
        """
        调用 AutoDL 上部署的模型 API。

        参数:
            image_url: 输入图片 URL。
            audio_url: 输入音频 URL。

        返回:
            生成视频的临时 URL；失败时返回空字符串，并写入 last_error。
        """
        try:
            self.last_error = ""
            if not AUTODL_MODEL_API_URL:
                self.last_error = "AUTODL_MODEL_API_URL 未配置，无法调用模型 API"
                logger.error(self.last_error)
                return ""

            logger.info("Calling model API at %s", AUTODL_MODEL_API_URL)
            response = requests.post(
                AUTODL_MODEL_API_URL,
                json={"image_url": image_url, "audio_url": audio_url},
                timeout=600,
            )
            logger.info("模型 API 返回状态码: %s", response.status_code)
            if response.status_code >= 400:
                self.last_error = f"模型 API 请求失败: HTTP {response.status_code}, {response.text[:500]}"
                logger.error(self.last_error)
            response.raise_for_status()

            try:
                video_url = self._extract_video_url(response.json())
            except ValueError:
                video_url = response.text.strip()

            if not video_url:
                self.last_error = f"模型 API 未返回视频 URL: {response.text[:500]}"
                logger.warning(self.last_error)
                return ""

            logger.info("模型 API 调用成功，视频 URL: %s", video_url)
            return video_url
        except Exception as exc:
            if not self.last_error:
                self.last_error = f"调用模型 API 失败: {exc}"
            logger.exception("调用模型 API 失败: %s", exc)
            return ""

    def acquire(self) -> None:
        """登记一个新的活跃请求，并取消已安排的延迟关机。"""
        with self.lock:
            self.active_requests += 1
            if self.shutdown_timer is not None:
                self.shutdown_timer.cancel()
                self.shutdown_timer = None
            logger.info("GPU 活跃请求数增加为: %s", self.active_requests)

    def release(self) -> None:
        """释放一个活跃请求；当请求数归零时安排 5 分钟后关机。"""
        with self.lock:
            if self.active_requests > 0:
                self.active_requests -= 1
            else:
                logger.warning("GPU 活跃请求数已经为 0，忽略 release 调用")

            logger.info("GPU 活跃请求数减少为: %s", self.active_requests)
            should_schedule_shutdown = self.active_requests == 0

        if should_schedule_shutdown:
            self.schedule_shutdown(5)

    def schedule_shutdown(self, delay_minutes: int = 5) -> None:
        """安排延迟关机，delay_minutes 表示延迟分钟数。"""
        delay_seconds = max(delay_minutes, 0) * 60

        with self.lock:
            if self.shutdown_timer is not None:
                self.shutdown_timer.cancel()

            self.shutdown_timer = threading.Timer(delay_seconds, self._do_shutdown)
            self.shutdown_timer.daemon = True
            self.shutdown_timer.start()
            logger.info("已安排 %s 分钟后关闭 AutoDL 实例", delay_minutes)

    def _do_shutdown(self) -> None:
        """定时器触发后确认无活跃请求，再执行关机。"""
        with self.lock:
            if self.active_requests > 0:
                logger.info("仍有活跃请求，取消本次 AutoDL 延迟关机")
                return

            self.shutdown_timer = None

        logger.info("无活跃请求，开始关闭 AutoDL 实例")
        self.stop_instance()


gpu_manager = GPUManager()
