import os
import uuid
import shutil
from pathlib import Path
from typing import Dict, Any

from fastapi import FastAPI, UploadFile, File, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates

from processor import process_recap_pipeline

app = FastAPI(title="Recap Video Retiming Tool")

BASE_DIR = Path(__file__).resolve().parent
UPLOADS_DIR = BASE_DIR / "uploads"
OUTPUTS_DIR = BASE_DIR / "outputs"
TEMPLATES_DIR = BASE_DIR

UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# In-memory job status tracking
TASKS: Dict[str, Dict[str, Any]] = {}


def run_pipeline_task(task_id: str, video_path: str, srt_path: str, zip_path: str, output_path: str, task_dir: str):
    try:
        process_recap_pipeline(
            video_path=video_path,
            srt_path=srt_path,
            audio_zip_path=zip_path,
            output_path=output_path
        )
        TASKS[task_id]["status"] = "completed"
        TASKS[task_id]["output_filename"] = f"{task_id}_final.mp4"
    except Exception as exc:
        TASKS[task_id]["status"] = "failed"
        TASKS[task_id]["error"] = str(exc)
    finally:
        # Clean up temporary uploaded files for this task
        if os.path.exists(task_dir):
            shutil.rmtree(task_dir, ignore_errors=True)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")
@app.post("/api/process")
async def process_video(
    background_tasks: BackgroundTasks,
    video: UploadFile = File(...),
    srt: UploadFile = File(...),
    audio_zip: UploadFile = File(...)
):
    task_id = str(uuid.uuid4())
    task_dir = UPLOADS_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    video_ext = Path(video.filename).suffix or ".mp4"
    video_path = task_dir / f"input_video{video_ext}"
    srt_path = task_dir / "input.srt"
    zip_path = task_dir / "input_audios.zip"
    output_path = OUTPUTS_DIR / f"{task_id}_final.mp4"

    with open(video_path, "wb") as f:
        shutil.copyfileobj(video.file, f)
    with open(srt_path, "wb") as f:
        shutil.copyfileobj(srt.file, f)
    with open(zip_path, "wb") as f:
        shutil.copyfileobj(audio_zip.file, f)

    TASKS[task_id] = {
        "status": "processing",
        "error": None,
        "output_filename": None
    }

    background_tasks.add_task(
        run_pipeline_task,
        task_id=task_id,
        video_path=str(video_path),
        srt_path=str(srt_path),
        zip_path=str(zip_path),
        output_path=str(output_path),
        task_dir=str(task_dir)
    )

    return {"task_id": task_id, "status": "processing"}


@app.get("/api/status/{task_id}")
async def get_task_status(task_id: str):
    if task_id not in TASKS:
        raise HTTPException(status_code=404, detail="Task not found")

    task = TASKS[task_id]
    response = {
        "status": task["status"],
        "error": task.get("error")
    }

    if task["status"] == "completed":
        response["download_url"] = f"/api/download/{task_id}"

    return response


@app.get("/api/download/{task_id}")
async def download_rendered_video(task_id: str):
    file_path = OUTPUTS_DIR / f"{task_id}_final.mp4"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Processed video file not found")

    return FileResponse(
        path=file_path,
        filename="recap_synced_video.mp4",
        media_type="video/mp4"
    )
