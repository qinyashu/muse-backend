#!/usr/bin/env bash
set -Eeuo pipefail

# Xingsu AI ComfyUI dance motion-transfer service installer for AutoDL.
# Target workflow: Wan2.2 Animate / VACE style pose-driven motion transfer.
# Public API:
#   POST /dance {"image_url": "...", "video_url": "..."}

ROOT_DIR="${ROOT_DIR:-/root/autodl-tmp/xingsu-comfyui-dance}"
ENV_NAME="${ENV_NAME:-xingsu_comfyui_dance}"
COMFYUI_DIR="${COMFYUI_DIR:-$ROOT_DIR/ComfyUI}"
SERVICE_DIR="${SERVICE_DIR:-$ROOT_DIR/service}"
WORKFLOW_URL="${WORKFLOW_URL:-https://raw.githubusercontent.com/Comfy-Org/workflow_templates/refs/heads/main/templates/video_wan2_2_14B_animate.json}"
WORKFLOW_PATH="${WORKFLOW_PATH:-$SERVICE_DIR/workflows/video_wan2_2_14B_animate_api.json}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.org/simple}"
GIT_CLONE_TIMEOUT_SECONDS="${GIT_CLONE_TIMEOUT_SECONDS:-900}"
GIT_MIRROR_PREFIX="${GIT_MIRROR_PREFIX:-}"
CONDA_BASE="${CONDA_BASE:-}"
ENV_PREFIX="${ENV_PREFIX:-}"

log() {
  printf '\n[%s] %s\n' "$(date '+%F %T')" "$*"
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing command: $1"
    exit 1
  }
}

resolve_git_url() {
  local repo_url="$1"
  if [ -n "${GIT_MIRROR_PREFIX:-}" ]; then
    printf '%s/%s' "${GIT_MIRROR_PREFIX%/}" "$repo_url"
  else
    printf '%s' "$repo_url"
  fi
}

refresh_env_paths() {
  if [ -z "${CONDA_BASE:-}" ]; then
    CONDA_BASE="$(conda info --base)"
  fi
  ENV_PREFIX="${ENV_PREFIX:-$CONDA_BASE/envs/$ENV_NAME}"
  ENV_PYTHON="$ENV_PREFIX/bin/python"
  ENV_PIP="$ENV_PYTHON -m pip"
}

run_in_env() {
  "$ENV_PYTHON" -m pip "$@"
}

safe_remove_partial_repo() {
  local target_path
  local root_path
  target_path="$(realpath -m "$1")"
  root_path="$(realpath -m "$ROOT_DIR")"
  case "$target_path" in
    "$root_path"/*)
      log "Removing incomplete git directory: $target_path"
      rm -rf "$target_path"
      ;;
    *)
      echo "Refusing to remove path outside ROOT_DIR: $target_path"
      exit 1
      ;;
  esac
}

clone_or_update_repo() {
  local repo_url="$1"
  local target_dir="$2"
  local final_url
  final_url="$(resolve_git_url "$repo_url")"

  if [ -d "$target_dir" ] && [ ! -d "$target_dir/.git" ]; then
    safe_remove_partial_repo "$target_dir"
  fi

  if [ ! -d "$target_dir/.git" ]; then
    log "Cloning $repo_url -> $target_dir"
    timeout "$GIT_CLONE_TIMEOUT_SECONDS" git clone --depth 1 "$final_url" "$target_dir"
  else
    log "Updating $target_dir"
    timeout "$GIT_CLONE_TIMEOUT_SECONDS" git -C "$target_dir" pull --ff-only || true
  fi
}

download_model() {
  local url="$1"
  local target="$2"
  mkdir -p "$(dirname "$target")"
  if [ -s "$target" ]; then
    log "Model exists: $target"
    return 0
  fi
  log "Downloading model: $target"
  wget --content-disposition -O "$target" "$url"
}

log "Preparing directories under $ROOT_DIR"
mkdir -p "$ROOT_DIR" "$SERVICE_DIR" "$SERVICE_DIR/workflows"

log "Checking base commands"
need_cmd conda
need_cmd git
need_cmd wget

if command -v apt-get >/dev/null 2>&1; then
  log "Installing system packages"
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    git git-lfs ffmpeg wget curl ca-certificates build-essential
fi
git lfs install || true

if conda config --show solver >/dev/null 2>&1; then
  conda config --set solver libmamba || true
fi

if ! conda env list | awk '{print $1}' | grep -qx "$ENV_NAME"; then
  log "Creating conda env: $ENV_NAME"
  conda create -n "$ENV_NAME" python=3.11 -y
else
  refresh_env_paths
  env_python_version="$("$ENV_PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
  if [ "${env_python_version:-}" != "3.11" ]; then
    log "Existing env $ENV_NAME has Python ${env_python_version:-unknown}; recreating with Python 3.11"
    conda env remove -n "$ENV_NAME" -y || true
    conda create -n "$ENV_NAME" python=3.11 -y
  else
    log "Conda env already exists: $ENV_NAME"
  fi
fi

refresh_env_paths
log "Using Python: $ENV_PYTHON"
"$ENV_PYTHON" -c 'import sys; print("Python", sys.version)'

log "Cloning/updating ComfyUI"
clone_or_update_repo "https://github.com/comfyanonymous/ComfyUI.git" "$COMFYUI_DIR"

log "Installing PyTorch and ComfyUI dependencies"
run_in_env install --upgrade --index-url "$PIP_INDEX_URL" pip setuptools wheel
run_in_env install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
run_in_env install -r "$COMFYUI_DIR/requirements.txt" --index-url "$PIP_INDEX_URL"
run_in_env install fastapi uvicorn requests python-dotenv oss2 aiofiles python-multipart pillow --index-url "$PIP_INDEX_URL"

CUSTOM_NODES_DIR="$COMFYUI_DIR/custom_nodes"
mkdir -p "$CUSTOM_NODES_DIR"

log "Installing ComfyUI Manager"
clone_or_update_repo "https://github.com/Comfy-Org/ComfyUI-Manager.git" "$CUSTOM_NODES_DIR/ComfyUI-Manager"

log "Installing useful dance workflow custom nodes"
clone_or_update_repo "https://github.com/kijai/ComfyUI-WanVideoWrapper.git" "$CUSTOM_NODES_DIR/ComfyUI-WanVideoWrapper"
if [ -f "$CUSTOM_NODES_DIR/ComfyUI-WanVideoWrapper/requirements.txt" ]; then
  run_in_env install -r "$CUSTOM_NODES_DIR/ComfyUI-WanVideoWrapper/requirements.txt" --index-url "$PIP_INDEX_URL"
fi

clone_or_update_repo "https://github.com/kijai/ComfyUI-KJNodes.git" "$CUSTOM_NODES_DIR/ComfyUI-KJNodes"
if [ -f "$CUSTOM_NODES_DIR/ComfyUI-KJNodes/requirements.txt" ]; then
  run_in_env install -r "$CUSTOM_NODES_DIR/ComfyUI-KJNodes/requirements.txt" --index-url "$PIP_INDEX_URL"
fi

clone_or_update_repo "https://github.com/Fannovel16/comfyui_controlnet_aux.git" "$CUSTOM_NODES_DIR/comfyui_controlnet_aux"
if [ -f "$CUSTOM_NODES_DIR/comfyui_controlnet_aux/requirements.txt" ]; then
  run_in_env install -r "$CUSTOM_NODES_DIR/comfyui_controlnet_aux/requirements.txt" --index-url "$PIP_INDEX_URL"
fi

clone_or_update_repo "https://github.com/kijai/ComfyUI-segment-anything-2.git" "$CUSTOM_NODES_DIR/ComfyUI-segment-anything-2" || true
if [ -f "$CUSTOM_NODES_DIR/ComfyUI-segment-anything-2/requirements.txt" ]; then
  run_in_env install -r "$CUSTOM_NODES_DIR/ComfyUI-segment-anything-2/requirements.txt" --index-url "$PIP_INDEX_URL" || true
fi

log "Downloading official Wan2.2 Animate workflow"
python - <<PY
import urllib.request
from pathlib import Path

url = "$WORKFLOW_URL"
target = Path("$WORKFLOW_PATH")
target.parent.mkdir(parents=True, exist_ok=True)
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
with urllib.request.urlopen(req, timeout=120) as response:
    target.write_bytes(response.read())
print(target)
PY

log "Downloading core Wan2.2 Animate models"
download_model \
  "https://huggingface.co/Kijai/WanVideo_comfy_fp8_scaled/resolve/main/Wan22Animate/Wan2_2-Animate-14B_fp8_e4m3fn_scaled_KJ.safetensors" \
  "$COMFYUI_DIR/models/diffusion_models/Wan2_2-Animate-14B_fp8_e4m3fn_scaled_KJ.safetensors"
download_model \
  "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors" \
  "$COMFYUI_DIR/models/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors"
download_model \
  "https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files/vae/wan_2.1_vae.safetensors" \
  "$COMFYUI_DIR/models/vae/wan_2.1_vae.safetensors"
download_model \
  "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/clip_vision/clip_vision_h.safetensors" \
  "$COMFYUI_DIR/models/clip_vision/clip_vision_h.safetensors"
download_model \
  "https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/Lightx2v/lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors" \
  "$COMFYUI_DIR/models/loras/lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors"
download_model \
  "https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/LoRAs/Wan22_relight/WanAnimate_relight_lora_fp16.safetensors" \
  "$COMFYUI_DIR/models/loras/WanAnimate_relight_lora_fp16.safetensors"

log "Writing ComfyUI dance wrapper service"
cat > "$SERVICE_DIR/api_comfyui_dance.py" <<'PY'
from __future__ import annotations

import copy
import json
import logging
import mimetypes
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import oss2
import requests
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel


load_dotenv()
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(message)s")
logger = logging.getLogger("xingsu_comfyui_dance")

COMFYUI_BASE_URL = os.environ.get("COMFYUI_BASE_URL", "http://127.0.0.1:8188")
COMFYUI_WORKFLOW_PATH = os.environ.get(
    "COMFYUI_WORKFLOW_PATH",
    "/root/autodl-tmp/xingsu-comfyui-dance/service/workflows/video_wan2_2_14B_animate_api.json",
)
COMFYUI_TIMEOUT_SECONDS = int(os.environ.get("COMFYUI_TIMEOUT_SECONDS", "3600"))
COMFYUI_POLL_INTERVAL_SECONDS = float(os.environ.get("COMFYUI_POLL_INTERVAL_SECONDS", "5"))
COMFYUI_OUTPUT_PREFIX = os.environ.get("COMFYUI_OUTPUT_PREFIX", "xingsu_dance")
COMFYUI_PROMPT = os.environ.get("COMFYUI_PROMPT", "The character is dancing in the room, cinematic music video, natural body motion")
COMFYUI_NEGATIVE_PROMPT = os.environ.get(
    "COMFYUI_NEGATIVE_PROMPT",
    "low quality, blurry, distorted face, bad hands, bad anatomy, jitter, extra limbs, subtitles, watermark",
)

OSS_ACCESS_KEY_ID = os.environ.get("OSS_ACCESS_KEY_ID", "")
OSS_ACCESS_KEY_SECRET = os.environ.get("OSS_ACCESS_KEY_SECRET", "")
OSS_ENDPOINT = os.environ.get("OSS_ENDPOINT", "")
OSS_BUCKET_NAME = os.environ.get("OSS_BUCKET_NAME", "")

app = FastAPI(title="Xingsu ComfyUI Dance API", version="1.0.0")


class DanceRequest(BaseModel):
    image_url: str
    video_url: str
    prompt: str | None = None
    negative_prompt: str | None = None


def short_error(message: str, limit: int = 1000) -> str:
    clean = " ".join(str(message).split())
    return clean if len(clean) <= limit else clean[:limit] + "..."


def guess_suffix(source: str, default_suffix: str) -> str:
    suffix = os.path.splitext(source.split("?", 1)[0])[1].lower()
    return suffix or default_suffix


def download_file(url: str, output_path: str) -> None:
    logger.info("Downloading %s -> %s", url, output_path)
    response = requests.get(url, stream=True, timeout=600)
    response.raise_for_status()
    with open(output_path, "wb") as file_obj:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                file_obj.write(chunk)


def normalize_video(input_path: str, output_path: str) -> str:
    command = [
        "ffmpeg",
        "-y",
        "-i",
        input_path,
        "-vf",
        "scale='min(832,iw)':-2",
        "-r",
        "16",
        "-an",
        "-t",
        os.environ.get("DANCE_MAX_SECONDS", "8"),
        output_path,
    ]
    logger.info("Normalizing driving video: %s", command)
    result = subprocess.run(command, capture_output=True, text=True, timeout=900)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg video normalization failed: {short_error(result.stderr or result.stdout)}")
    return output_path


def comfy_url(path: str) -> str:
    return f"{COMFYUI_BASE_URL.rstrip('/')}/{path.lstrip('/')}"


def upload_to_comfy(local_path: str, upload_type: str) -> str:
    file_name = Path(local_path).name
    mime_type = mimetypes.guess_type(local_path)[0] or "application/octet-stream"
    with open(local_path, "rb") as file_obj:
        response = requests.post(
            comfy_url("/upload/image"),
            files={"image": (file_name, file_obj, mime_type)},
            data={"type": upload_type, "subfolder": "xingsu", "overwrite": "true"},
            timeout=600,
        )
    if response.status_code >= 400:
        raise RuntimeError(
            f"ComfyUI upload failed: type={upload_type}, file={file_name}, HTTP {response.status_code}, {short_error(response.text)}"
        )
    data = response.json()
    name = data.get("name") or file_name
    subfolder = data.get("subfolder") or "xingsu"
    return f"{subfolder}/{name}" if subfolder else name


NODE_WIDGET_INPUTS = {
    "UNETLoader": ("unet_name", "weight_dtype"),
    "CLIPLoader": ("clip_name", "type", "device"),
    "DualCLIPLoader": ("clip_name1", "clip_name2", "type", "device"),
    "TripleCLIPLoader": ("clip_name1", "clip_name2", "clip_name3", "type", "device"),
    "QuadrupleCLIPLoader": (
        "clip_name1",
        "clip_name2",
        "clip_name3",
        "clip_name4",
        "type",
        "device",
    ),
    "VAELoader": ("vae_name",),
    "ModelPatchLoader": ("name",),
    "AudioEncoderLoader": ("audio_encoder_name",),
    "LoraLoader": ("lora_name", "strength_model", "strength_clip"),
    "LoraLoaderModelOnly": ("lora_name", "strength_model"),
}


def convert_workflow_to_api(workflow: dict[str, Any]) -> dict[str, Any]:
    if all(isinstance(value, dict) and "class_type" in value for value in workflow.values()):
        return copy.deepcopy(workflow)

    nodes = {str(node["id"]): node for node in workflow.get("nodes", [])}
    links = {link[0]: [str(link[1]), int(link[2])] for link in workflow.get("links", [])}
    prompt: dict[str, Any] = {}

    for node_id, node in nodes.items():
        inputs: dict[str, Any] = {}
        widget_values = list(node.get("widgets_values") or [])
        widget_index = 0
        for input_def in node.get("inputs", []) or []:
            input_name = input_def.get("name")
            if not input_name:
                continue
            link_id = input_def.get("link")
            if link_id is not None and link_id in links:
                inputs[input_name] = links[link_id]
                continue
            if "widget" in input_def and widget_index < len(widget_values):
                inputs[input_name] = widget_values[widget_index]
                widget_index += 1

        # Nodes without input sockets keep widget values as named inputs by class.
        class_type = node.get("type")
        if class_type == "LoadImage" and widget_values:
            inputs["image"] = widget_values[0]
        elif class_type == "LoadVideo" and widget_values:
            inputs["video"] = widget_values[0]
        elif class_type == "SaveVideo" and widget_values:
            inputs["filename_prefix"] = widget_values[0]
        elif class_type == "CLIPTextEncode" and widget_values:
            inputs["text"] = widget_values[0]

        for field_name, value in zip(NODE_WIDGET_INPUTS.get(class_type, ()), widget_values):
            inputs.setdefault(field_name, value)

        prompt[node_id] = {
            "class_type": class_type,
            "inputs": inputs,
        }
        if node.get("properties"):
            prompt[node_id]["_meta"] = {
                "title": node.get("properties", {}).get("Node name for S&R") or class_type
            }
    return prompt


def patch_workflow(prompt: dict[str, Any], image_name: str, video_name: str, request: DanceRequest) -> dict[str, Any]:
    patched = copy.deepcopy(prompt)
    output_prefix = f"{COMFYUI_OUTPUT_PREFIX}_{uuid.uuid4().hex[:10]}"
    positive_prompt = request.prompt or COMFYUI_PROMPT
    negative_prompt = request.negative_prompt or COMFYUI_NEGATIVE_PROMPT

    for node in patched.values():
        class_type = node.get("class_type")
        inputs = node.setdefault("inputs", {})
        if class_type == "LoadImage":
            inputs["image"] = image_name
        elif class_type == "LoadVideo":
            inputs["video"] = video_name
        elif class_type == "SaveVideo":
            inputs["filename_prefix"] = output_prefix
        elif class_type == "CLIPTextEncode":
            current = str(inputs.get("text", "")).lower()
            if "low quality" in current or "watermark" in current or "worst" in current:
                inputs["text"] = negative_prompt
            else:
                inputs["text"] = positive_prompt
    return patched


def queue_prompt(prompt: dict[str, Any]) -> str:
    response = requests.post(
        comfy_url("/prompt"),
        json={"prompt": prompt, "client_id": f"xingsu-dance-{uuid.uuid4().hex}"},
        timeout=120,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"ComfyUI prompt failed: HTTP {response.status_code}, {short_error(response.text)}")
    data = response.json()
    prompt_id = data.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI did not return prompt_id: {data}")
    return str(prompt_id)


def wait_for_output(prompt_id: str) -> dict[str, Any]:
    started_at = time.time()
    while True:
        if time.time() - started_at > COMFYUI_TIMEOUT_SECONDS:
            raise RuntimeError(f"ComfyUI generation timed out: prompt_id={prompt_id}")

        response = requests.get(comfy_url(f"/history/{prompt_id}"), timeout=120)
        if response.status_code >= 400:
            raise RuntimeError(f"ComfyUI history failed: HTTP {response.status_code}, {short_error(response.text)}")
        data = response.json()
        item = data.get(prompt_id)
        if item:
            status = item.get("status") or {}
            if status.get("status_str") == "error":
                raise RuntimeError(f"ComfyUI generation failed: {short_error(json.dumps(status, ensure_ascii=False))}")
            outputs = item.get("outputs") or {}
            if outputs:
                return outputs
        time.sleep(max(COMFYUI_POLL_INTERVAL_SECONDS, 1))


def choose_output_file(outputs: dict[str, Any]) -> dict[str, str]:
    for node_output in outputs.values():
        for key in ("videos", "gifs", "images"):
            for item in node_output.get(key, []) or []:
                filename = item.get("filename")
                if filename:
                    return {
                        "filename": filename,
                        "subfolder": item.get("subfolder", ""),
                        "type": item.get("type", "output"),
                    }
    raise RuntimeError(f"ComfyUI did not produce a downloadable output: {outputs}")


def download_comfy_output(file_info: dict[str, str], local_path: str) -> None:
    response = requests.get(comfy_url("/view"), params=file_info, stream=True, timeout=600)
    if response.status_code >= 400:
        raise RuntimeError(f"ComfyUI output download failed: HTTP {response.status_code}, {short_error(response.text)}")
    with open(local_path, "wb") as file_obj:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                file_obj.write(chunk)
    if not os.path.exists(local_path) or os.path.getsize(local_path) == 0:
        raise RuntimeError("ComfyUI output file is empty")


def upload_file(local_path: str, oss_path: str) -> str:
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
    bucket = oss2.Bucket(oss2.Auth(OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET), OSS_ENDPOINT, OSS_BUCKET_NAME)
    bucket.put_object_from_file(oss_path, local_path)
    endpoint = OSS_ENDPOINT.replace("https://", "").replace("http://", "").rstrip("/")
    return f"https://{OSS_BUCKET_NAME}.{endpoint}/{oss_path.lstrip('/')}"


def run_generation(image_url: str, video_url: str, request: DanceRequest) -> str:
    with tempfile.TemporaryDirectory(prefix="xingsu_comfyui_dance_") as tmp_dir:
        image_path = os.path.join(tmp_dir, f"reference{guess_suffix(image_url, '.png')}")
        video_input_path = os.path.join(tmp_dir, f"drive{guess_suffix(video_url, '.mp4')}")
        video_path = os.path.join(tmp_dir, "drive_normalized.mp4")
        output_path = os.path.join(tmp_dir, "output.mp4")

        download_file(image_url, image_path)
        download_file(video_url, video_input_path)
        normalize_video(video_input_path, video_path)

        image_name = upload_to_comfy(image_path, "input")
        video_name = upload_to_comfy(video_path, "input")

        workflow = json.loads(Path(COMFYUI_WORKFLOW_PATH).read_text(encoding="utf-8", errors="ignore"))
        prompt = convert_workflow_to_api(workflow)
        patched_prompt = patch_workflow(prompt, image_name, video_name, request)
        prompt_id = queue_prompt(patched_prompt)
        logger.info("ComfyUI dance prompt queued: prompt_id=%s", prompt_id)
        outputs = wait_for_output(prompt_id)
        file_info = choose_output_file(outputs)
        download_comfy_output(file_info, output_path)
        return upload_file(output_path, f"comfyui_dance_outputs/{uuid.uuid4().hex}.mp4")


@app.get("/")
def health_check() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "service": "xingsu-comfyui-dance",
        "comfyui_url": COMFYUI_BASE_URL,
        "workflow_exists": os.path.exists(COMFYUI_WORKFLOW_PATH),
    }


@app.post("/")
@app.post("/dance")
def dance(request: DanceRequest) -> JSONResponse:
    if not request.image_url or not request.video_url:
        return JSONResponse(status_code=400, content={"error": "image_url and video_url are required"})

    try:
        video_url = run_generation(request.image_url, request.video_url, request)
        return JSONResponse(content={"video_url": video_url, "mode": "comfyui-wan2.2-animate"})
    except Exception as exc:
        logger.exception("ComfyUI dance generation failed")
        return JSONResponse(status_code=500, content={"error": short_error(str(exc)), "mode": "failed"})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5003)
PY

log "Writing env template"
cat > "$SERVICE_DIR/.env.comfyui_dance" <<ENV
COMFYUI_BASE_URL=http://127.0.0.1:8188
COMFYUI_WORKFLOW_PATH=$WORKFLOW_PATH
COMFYUI_TIMEOUT_SECONDS=3600
COMFYUI_POLL_INTERVAL_SECONDS=5
COMFYUI_OUTPUT_PREFIX=xingsu_dance
DANCE_MAX_SECONDS=8

COMFYUI_PROMPT="The character is dancing in the room, cinematic music video, natural body motion"
COMFYUI_NEGATIVE_PROMPT="low quality, blurry, distorted face, bad hands, bad anatomy, jitter, extra limbs, subtitles, watermark"

OSS_ACCESS_KEY_ID=PLEASE_FILL
OSS_ACCESS_KEY_SECRET=PLEASE_FILL
OSS_ENDPOINT=PLEASE_FILL
OSS_BUCKET_NAME=PLEASE_FILL
ENV

log "Writing start scripts"
cat > "$SERVICE_DIR/start_comfyui.sh" <<SH
#!/usr/bin/env bash
set -Eeuo pipefail
cd "$COMFYUI_DIR"
exec "$ENV_PYTHON" main.py --listen 0.0.0.0 --port 8188
SH

cat > "$SERVICE_DIR/start_comfyui_dance_api.sh" <<SH
#!/usr/bin/env bash
set -Eeuo pipefail
set -a
source "$SERVICE_DIR/.env.comfyui_dance"
set +a
cd "$SERVICE_DIR"
exec "$ENV_PYTHON" -m uvicorn api_comfyui_dance:app --host 0.0.0.0 --port 5003
SH

chmod +x "$SERVICE_DIR/start_comfyui.sh" "$SERVICE_DIR/start_comfyui_dance_api.sh"

log "Verifying Python imports"
"$ENV_PYTHON" - <<'PY'
import torch
print('torch', torch.__version__, 'cuda', torch.cuda.is_available())
import fastapi, uvicorn, requests, oss2
print('api deps ok')
PY

cat <<EOF

Install finished.

Next:
1. Start ComfyUI:
   bash $SERVICE_DIR/start_comfyui.sh

2. If ComfyUI reports missing custom nodes/models, copy the log back to Codex.
   The workflow is saved at:
   $WORKFLOW_PATH

3. Edit OSS values:
   nano $SERVICE_DIR/.env.comfyui_dance

4. Start the dance wrapper API in another terminal:
   bash $SERVICE_DIR/start_comfyui_dance_api.sh

5. Test locally:
   curl http://127.0.0.1:5003/

6. Expose wrapper API if needed:
   cloudflared tunnel --url http://127.0.0.1:5003

7. Put the public wrapper URL into Railway:
   EAS_DANCE_SERVICE_URL=https://your-public-comfyui-dance-wrapper-url
   DANCE_ALLOW_DEGRADED_OUTPUT=false

EOF
