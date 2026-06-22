from dotenv import load_dotenv

load_dotenv()

import os


# SQLite database URL. Override with DATABASE_URL when deploying.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./app.db")

# Default number of tasks available to a new user.
DEFAULT_REMAINING_COUNT = int(os.getenv("DEFAULT_REMAINING_COUNT", "0"))

# 阿里云 OSS 访问配置，可通过环境变量覆盖。
OSS_ACCESS_KEY_ID = os.environ.get("OSS_ACCESS_KEY_ID", "")
OSS_ACCESS_KEY_SECRET = os.environ.get("OSS_ACCESS_KEY_SECRET", "")
OSS_ENDPOINT = os.environ.get("OSS_ENDPOINT", "")
OSS_BUCKET_NAME = os.environ.get("OSS_BUCKET_NAME", "")

# AutoDL 实例和模型 API 配置。
AUTODL_TOKEN = os.environ.get("AUTODL_TOKEN", "")
AUTODL_INSTANCE_ID = os.environ.get("AUTODL_INSTANCE_ID", "")
AUTODL_MODEL_API_URL = os.environ.get("AUTODL_MODEL_API_URL", "")

# 管理员接口访问令牌。
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
