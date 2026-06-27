#!/usr/bin/env bash
set -Eeuo pipefail

# Add async job-based endpoints (POST /jobs, GET /jobs/{id}) to the live
# AutoDL singing wrapper so Railway can poll instead of waiting on a single
# long HTTP request that Cloudflare may kill (524).

SERVICE_DIR="${SERVICE_DIR:-/root/autodl-tmp/xingsu-comfyui/service}"
API_FILE="$SERVICE_DIR/api_comfyui_sing.py"

if [ ! -f "$API_FILE" ]; then
  echo "ERROR: $API_FILE not found" >&2
  exit 1
fi

log() { printf '\n[%s] %s\n' "$(date '\''+%F %T'\'')" "$*"; }

log "Patching $API_FILE to add async /jobs endpoints"

/root/miniconda3/bin/python - <<'PY'
import re
from pathlib import Path

p = Path("/root/autodl-tmp/xingsu-comfyui/service/api_comfyui_sing.py")
text = p.read_text(encoding="utf-8", errors="replace")

if "_run_job_background" in text:
    print("Async /jobs endpoints already present, skipping patch")
    raise SystemExit(0)

lines = text.split("\n")
last_import_line = 0
for i, line in enumerate(lines):
    stripped = line.strip()
    if stripped.startswith("import ") or stripped.startswith("from "):
        last_import_line = i + 1

new_lines = list(lines)
new_lines.insert(last_import_line, "import threading")
new_lines.insert(last_import_line + 1, "_job_lock = threading.Lock()")
new_lines.insert(last_import_line + 2, "_jobs: dict[str, dict] = {}")
text = "\n".join(new_lines)

bg_runner = """

def _run_job_background(job_id: str, image_url: str, audio_url: str, request: GenerateRequest) -> None:
    try:
        video_url = run_generation(image_url, audio_url, request)
        with _job_lock:
            _jobs[job_id] = {
                "status": "done",
                "video_url": video_url,
                "mode": "comfyui-infinitetalk",
                "error": None,
            }
        logger.info("Async job completed: job_id=%s video_url=%s", job_id, video_url)
    except Exception as exc:
        logger.exception("Async job failed: job_id=%s", job_id)
        with _job_lock:
            _jobs[job_id] = {
                "status": "failed",
                "video_url": None,
                "mode": "failed",
                "error": _short_error(str(exc)),
            }


@app.post("/jobs")
def create_job(request: GenerateRequest) -> dict[str, str]:
    if not request.image_url or not request.audio_url:
        raise HTTPException(status_code=400, detail="image_url and audio_url are required")
    job_id = uuid.uuid4().hex
    with _job_lock:
        _jobs[job_id] = {"status": "pending", "video_url": None, "mode": None, "error": None}
    thread = threading.Thread(
        target=_run_job_background,
        args=(job_id, request.image_url, request.audio_url, request),
        daemon=True,
    )
    thread.start()
    logger.info("Async job created: job_id=%s", job_id)
    return {"job_id": job_id, "status": "pending"}


@app.get("/jobs/{job_id}")
def get_job_status(job_id: str) -> dict[str, str | None]:
    with _job_lock:
        job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "job_id": job_id,
        "status": job.get("status"),
        "video_url": job.get("video_url"),
        "mode": job.get("mode"),
        "error": job.get("error"),
    }
"""

health_marker = '@app.get("/")\ndef health_check'
insert_before = text.find(health_marker)
if insert_before == -1:
    health_marker = '@app.get("/")'
    insert_before = text.find(health_marker)

if insert_before > 0:
    text = text[:insert_before] + bg_runner + "\n\n" + text[insert_before:]
else:
    text = text + bg_runner

p.write_text(text, encoding="utf-8")
print("Patched api_comfyui_sing.py with async /jobs endpoints")
PY

log "Restarting singing API"
screen -S singapi -X quit >/dev/null 2>&1 || true
pkill -f 'uvicorn api_comfyui_sing:app' >/dev/null 2>&1 || true
sleep 1
cd "$SERVICE_DIR" && screen -dmS singapi bash start_comfyui_sing_api.sh

log "Waiting for singing API"
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:5002/ >/dev/null 2>&1; then
    log "Singing API healthy"
    break
  fi
  sleep 2
done

log "Verifying /jobs endpoint"
curl -sS --max-time 10 http://127.0.0.1:5002/jobs | head -c 500 || echo "no jobs response"
echo

log "Async wrapper patch complete"
