import os

from dotenv import load_dotenv


# 加载本地 .env；Railway 上会直接读取平台环境变量。
load_dotenv()


# SQLite 数据库地址，部署时可通过 DATABASE_URL 覆盖。
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./app.db")

# 新用户默认剩余次数。
DEFAULT_REMAINING_COUNT = int(os.getenv("DEFAULT_REMAINING_COUNT", "0"))

# 阿里云 OSS 访问配置。
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

# 抖音下载 Cookie。可直接填浏览器请求里的 Cookie 字符串。
DOUYIN_COOKIE = os.environ.get("DOUYIN_COOKIE", "")

# 抖音 Cookie 的 Base64 版本，适合 Cookie 含特殊字符时在命令行里配置。
DOUYIN_COOKIE_B64 = os.environ.get("DOUYIN_COOKIE_B64", "")
