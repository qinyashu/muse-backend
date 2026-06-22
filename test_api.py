import os
import uuid

import requests


# 后端服务地址，可通过 API_BASE_URL 覆盖。
BASE_URL = os.environ.get("API_BASE_URL", "http://127.0.0.1:8000")

# 测试设备 ID 默认每次生成新的，避免剩余次数被历史测试消耗。
DEVICE_ID = os.environ.get("TEST_DEVICE_ID", f"test-device-{uuid.uuid4().hex}")
TASK_TYPE = os.environ.get("TEST_TASK_TYPE", "sing")
DOUYIN_URL = os.environ.get("TEST_DOUYIN_URL", "https://v.douyin.com/test/")


def print_result(name: str, response: requests.Response) -> None:
    """打印接口测试结果。"""
    print(f"\n[{name}] HTTP {response.status_code}")
    try:
        print(response.json())
    except ValueError:
        print(response.text)


def test_register_user() -> None:
    """测试用户注册接口。"""
    response = requests.post(
        f"{BASE_URL}/api/user/register",
        json={"device_id": DEVICE_ID},
        timeout=30,
    )
    print_result("注册用户", response)
    response.raise_for_status()


def test_get_user_info() -> None:
    """测试用户信息查询接口。"""
    response = requests.get(
        f"{BASE_URL}/api/user/info",
        params={"device_id": DEVICE_ID},
        timeout=30,
    )
    print_result("查询用户", response)
    response.raise_for_status()


def test_create_task() -> int | None:
    """测试创建任务接口，上传一张最小测试图片。"""
    image_bytes = (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00"
        b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t"
        b"\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a"
        b"\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342"
        b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
        b"\xff\xc4\x00\x14\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
        b"\x00\x00\x00\x00\x00\x00\x00"
        b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xd2\xcf \xff\xd9"
    )
    files = {"photo": ("test.jpg", image_bytes, "image/jpeg")}
    data = {
        "device_id": DEVICE_ID,
        "type": TASK_TYPE,
        "douyin_url": DOUYIN_URL,
    }
    response = requests.post(
        f"{BASE_URL}/api/task/create",
        data=data,
        files=files,
        timeout=60,
    )
    print_result("创建任务", response)
    response.raise_for_status()
    return response.json().get("task_id")


def test_get_task_status(task_id: int) -> None:
    """测试任务状态查询接口。"""
    response = requests.get(
        f"{BASE_URL}/api/task/status",
        params={"task_id": task_id},
        timeout=30,
    )
    print_result("查询任务状态", response)
    response.raise_for_status()


def main() -> None:
    """按顺序测试注册、查询用户、创建任务和查询任务状态。"""
    print(f"测试后端地址: {BASE_URL}")
    print(f"测试设备 ID: {DEVICE_ID}")

    test_register_user()
    test_get_user_info()
    task_id = test_create_task()
    if task_id is not None:
        test_get_task_status(task_id)


if __name__ == "__main__":
    main()
