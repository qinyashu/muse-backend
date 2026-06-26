#!/usr/bin/env bash
set -Eeuo pipefail

# Patch the already-installed AutoDL ComfyUI singing wrapper in place.
# This avoids a full reinstall when /upload/image fails because the wrapper put
# a subdirectory inside the uploaded filename instead of using ComfyUI's
# subfolder field.

SERVICE_DIR="${SERVICE_DIR:-/root/autodl-tmp/xingsu-comfyui/service}"
ENV_FILE="$SERVICE_DIR/.env.comfyui_sing"
API_FILE="$SERVICE_DIR/api_comfyui_sing.py"
COMFYUI_SCREEN="${COMFYUI_SCREEN:-comfyui}"
SING_API_SCREEN="${SING_API_SCREEN:-singapi}"

log() {
  printf '[%s] %s\n' "$(date '+%F %T')" "$*"
}

need_file() {
  if [ ! -f "$1" ]; then
    echo "Missing required file: $1" >&2
    exit 1
  fi
}

need_file "$ENV_FILE"
need_file "$API_FILE"

log "Updating OSS endpoint in $ENV_FILE"
if grep -q '^OSS_ENDPOINT=' "$ENV_FILE"; then
  sed -i 's/^OSS_ENDPOINT=.*/OSS_ENDPOINT=oss-cn-hangzhou.aliyuncs.com/' "$ENV_FILE"
else
  printf '\nOSS_ENDPOINT=oss-cn-hangzhou.aliyuncs.com\n' >> "$ENV_FILE"
fi

log "Patching upload_to_comfy() in $API_FILE"
API_FILE="$API_FILE" python - <<'PY'
import os
from pathlib import Path

p = Path(os.environ["API_FILE"])
text = p.read_text(encoding="utf-8", errors="ignore")
start = text.index("def upload_to_comfy(")
end = text.index("\ndef convert_workflow_to_api(", start)
new = '''def upload_to_comfy(local_path: str, upload_type: str) -> str:
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
'''
p.write_text(text[:start] + new + text[end:], encoding="utf-8")
print("patched api_comfyui_sing.py")
PY

log "Patching convert_workflow_to_api() in $API_FILE"
API_FILE="$API_FILE" python - <<'PY'
import os
from pathlib import Path

p = Path(os.environ["API_FILE"])
text = p.read_text(encoding="utf-8", errors="ignore")
start = text.index("def convert_workflow_to_api(")
end = text.index("\ndef patch_workflow(", start)
new = '''def convert_workflow_to_api(workflow: dict[str, Any]) -> dict[str, Any]:
    if all(isinstance(value, dict) and "class_type" in value for value in workflow.values()):
        return copy.deepcopy(workflow)

    nodes = {str(node["id"]): node for node in workflow.get("nodes", [])}
    links = {
        link[0]: [str(link[1]), int(link[2])]
        for link in workflow.get("links", [])
    }
    prompt: dict[str, Any] = {}

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

    def is_frontend_helper_node(node: dict[str, Any]) -> bool:
        node_type = str(node.get("type") or "")
        if node_type in {"Reroute", "MarkdownNote"}:
            return True
        try:
            uuid.UUID(node_type)
            return True
        except ValueError:
            return False

    def resolve_link(link_id: int, seen: set[int] | None = None) -> list[Any]:
        seen = seen or set()
        if link_id in seen:
            raise RuntimeError(f"Workflow has a cyclic helper link: {link_id}")
        seen.add(link_id)

        source = links.get(link_id)
        if not source:
            raise RuntimeError(f"Workflow link is missing: {link_id}")

        source_node_id, source_slot = source
        source_node = nodes.get(source_node_id) or {}
        if not is_frontend_helper_node(source_node):
            return [source_node_id, source_slot]

        for input_def in source_node.get("inputs", []) or []:
            helper_link_id = input_def.get("link")
            if helper_link_id is not None:
                return resolve_link(helper_link_id, seen)

        raise RuntimeError(
            f"Workflow helper node {source_node_id} ({source_node.get('type')}) has no input link"
        )

    def add_widget_inputs(node: dict[str, Any], inputs: dict[str, Any]) -> None:
        widget_values = list(node.get("widgets_values") or [])
        input_defs = list(node.get("inputs", []) or [])
        widget_index = 0
        for input_def in input_defs:
            input_name = input_def.get("name")
            if not input_name:
                continue
            if input_name in inputs:
                continue
            link_id = input_def.get("link")
            if link_id is not None and link_id in links:
                inputs[input_name] = resolve_link(link_id)
                continue
            if "widget" in input_def and widget_index < len(widget_values):
                inputs[input_name] = widget_values[widget_index]
                widget_index += 1

        class_type = str(node.get("type") or "")
        if class_type == "AudioConcat" and "direction" not in inputs and widget_values:
            inputs["direction"] = widget_values[0]
        elif class_type == "CreateVideo":
            if "fps" not in inputs and widget_values:
                inputs["fps"] = widget_values[0]
            if "bit_depth" not in inputs and len(widget_values) > 1:
                inputs["bit_depth"] = widget_values[1]
        elif class_type == "SaveVideo":
            if "format" not in inputs and len(widget_values) > 1:
                inputs["format"] = widget_values[1]
            if "codec" not in inputs and len(widget_values) > 2:
                inputs["codec"] = widget_values[2]
        elif class_type == "PrimitiveInt":
            if "value" not in inputs and widget_values:
                inputs["value"] = widget_values[0]
            if "control_after_generate" not in inputs and len(widget_values) > 1:
                inputs["control_after_generate"] = widget_values[1]
        elif class_type == "LoadAudio" and widget_values and "audio" not in inputs:
            inputs["audio"] = widget_values[0]
        elif class_type == "LoadImage" and widget_values and "image" not in inputs:
            inputs["image"] = widget_values[0]

        for field_name, value in zip(NODE_WIDGET_INPUTS.get(class_type, ()), widget_values):
            inputs.setdefault(field_name, value)

    for node_id, node in nodes.items():
        if is_frontend_helper_node(node):
            continue

        inputs: dict[str, Any] = {}
        add_widget_inputs(node, inputs)

        prompt[node_id] = {
            "class_type": node.get("type"),
            "inputs": inputs,
        }
        if node.get("properties"):
            prompt[node_id]["_meta"] = {
                "title": node.get("properties", {}).get("Node name for S&R") or node.get("type")
            }

    return prompt
'''
p.write_text(text[:start] + new + text[end:], encoding="utf-8")
print("patched convert_workflow_to_api")
PY

log "Validating workflow conversion does not emit frontend helper nodes"
ENV_PYTHON="${ENV_PYTHON:-/root/miniconda3/envs/xingsu_comfyui_311/bin/python}"
if [ ! -x "$ENV_PYTHON" ]; then
  ENV_PYTHON="/root/miniconda3/envs/xingsu_comfyui/bin/python"
fi
API_FILE="$API_FILE" "$ENV_PYTHON" - <<'PY'
import importlib.util
import json
import os
import uuid
from pathlib import Path

api_file = Path(os.environ["API_FILE"])
spec = importlib.util.spec_from_file_location("api_comfyui_sing_live", api_file)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
workflow = json.loads(Path(module.COMFYUI_WORKFLOW_PATH).read_text(encoding="utf-8", errors="ignore"))
prompt = module.convert_workflow_to_api(workflow)

def is_helper_type(value):
    value = str(value or "")
    if value == "Reroute":
        return True
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False

helper_ids = {str(node["id"]) for node in workflow.get("nodes", []) if is_helper_type(node.get("type"))}
bad_nodes = [node_id for node_id, node in prompt.items() if is_helper_type(node.get("class_type"))]
bad_refs = [
    (node_id, input_name, value)
    for node_id, node in prompt.items()
    for input_name, value in (node.get("inputs") or {}).items()
    if isinstance(value, list) and value and str(value[0]) in helper_ids
]
if bad_nodes or bad_refs:
    raise SystemExit(f"Helper-node validation failed: nodes={bad_nodes}, refs={bad_refs[:5]}")
print(f"workflow conversion ok: nodes={len(prompt)}, helpers_removed={len(helper_ids)}")
PY

log "Stopping old sing API process"
screen -S "$SING_API_SCREEN" -X quit >/dev/null 2>&1 || true
pkill -f 'uvicorn api_comfyui_sing:app' >/dev/null 2>&1 || true

cd "$SERVICE_DIR"

log "Starting singing wrapper API in screen: $SING_API_SCREEN"
if command -v screen >/dev/null 2>&1; then
  screen -dmS "$SING_API_SCREEN" bash start_comfyui_sing_api.sh
else
  nohup bash start_comfyui_sing_api.sh > "$SERVICE_DIR/sing_api.log" 2>&1 &
fi

log "Checking wrapper health"
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:5002/; then
    printf '\n'
    log "AutoDL singing service is running"
    exit 0
  fi
  sleep 2
done

echo "Wrapper did not become healthy. Check logs with:" >&2
echo "  screen -r $SING_API_SCREEN" >&2
echo "  screen -r $COMFYUI_SCREEN" >&2
echo "or:" >&2
echo "  tail -n 200 $SERVICE_DIR/sing_api.log" >&2
echo "  tail -n 200 $SERVICE_DIR/comfyui.log" >&2
exit 1
