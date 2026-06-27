import os

from dotenv import load_dotenv


# Railway reads environment variables from the platform. Local development can
# still use a .env file.
load_dotenv()


# Database.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./app.db")

# New-user default quota.
DEFAULT_REMAINING_COUNT = int(os.getenv("DEFAULT_REMAINING_COUNT", "0"))

# Aliyun OSS.
OSS_ACCESS_KEY_ID = os.environ.get("OSS_ACCESS_KEY_ID", "")
OSS_ACCESS_KEY_SECRET = os.environ.get("OSS_ACCESS_KEY_SECRET", "")
OSS_ENDPOINT = os.environ.get("OSS_ENDPOINT", "")
OSS_BUCKET_NAME = os.environ.get("OSS_BUCKET_NAME", "")

# AutoDL legacy model API.
AUTODL_TOKEN = os.environ.get("AUTODL_TOKEN", "")
AUTODL_INSTANCE_ID = os.environ.get("AUTODL_INSTANCE_ID", "")
AUTODL_MODEL_API_URL = os.environ.get("AUTODL_MODEL_API_URL", "")

# Existing MuseTalk / PAI-EAS singing model.
EAS_SERVICE_URL = os.environ.get("EAS_SERVICE_URL", "")
EAS_AUTH_TOKEN = os.environ.get("EAS_AUTH_TOKEN", "")

# Singing MV provider. Use "comfyui" for the self-hosted ComfyUI workflow,
# "eas" for the legacy MuseTalk service, "sync" for Sync Labs, or
# "replicate" only when explicitly requested.
SING_MODEL_PROVIDER = os.environ.get(
    "SING_MODEL_PROVIDER",
    (
        "comfyui"
        if os.environ.get("COMFYUI_SERVICE_URL")
        else ("sync" if os.environ.get("SYNC_API_KEY") else "eas")
    ),
).strip().lower()
SYNC_API_KEY = os.environ.get("SYNC_API_KEY", "")
SYNC_API_BASE_URL = os.environ.get("SYNC_API_BASE_URL", "https://api.sync.so")
SYNC_MODEL = os.environ.get("SYNC_MODEL", "sync-3")
SYNC_GENERATION_OPTIONS_JSON = os.environ.get("SYNC_GENERATION_OPTIONS_JSON", "")
SYNC_POLL_INTERVAL_SECONDS = float(os.environ.get("SYNC_POLL_INTERVAL_SECONDS", "5"))
SYNC_TIMEOUT_SECONDS = int(os.environ.get("SYNC_TIMEOUT_SECONDS", "1800"))

COMFYUI_SERVICE_URL = os.environ.get("COMFYUI_SERVICE_URL", "")
COMFYUI_AUTH_TOKEN = os.environ.get("COMFYUI_AUTH_TOKEN", "")
COMFYUI_REQUEST_MODE = os.environ.get("COMFYUI_REQUEST_MODE", "generate").strip().lower()
COMFYUI_PROMPT = os.environ.get(
    "COMFYUI_PROMPT",
    "A stylish singer performing to camera, realistic, natural lip sync, cinematic music video, subtle body motion, soft stage lighting",
)
COMFYUI_NEGATIVE_PROMPT = os.environ.get(
    "COMFYUI_NEGATIVE_PROMPT",
    "low quality, blurry, distorted face, extra limbs, bad hands, jitter, warped mouth",
)

# Dance model.
EAS_DANCE_SERVICE_URL = os.environ.get("EAS_DANCE_SERVICE_URL", "")
EAS_DANCE_AUTH_TOKEN = os.environ.get("EAS_DANCE_AUTH_TOKEN", "")
DANCE_ALLOW_DEGRADED_OUTPUT = os.environ.get("DANCE_ALLOW_DEGRADED_OUTPUT", "").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# Admin API.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

# Douyin download cookie.
DOUYIN_COOKIE = os.environ.get("DOUYIN_COOKIE", "")
DOUYIN_COOKIE_B64 = os.environ.get("DOUYIN_COOKIE_B64", "")

# Replicate online API.
REPLICATE_API_TOKEN = os.environ.get("REPLICATE_API_TOKEN", "")
REPLICATE_MODEL_VERSION = os.environ.get(
    "REPLICATE_MODEL_VERSION",
    "tencentarc/musetalk:b76242b40d67c76ab6742e987628a2a9ac019e11d56ab96c4e91ce03b79b2787",
)
