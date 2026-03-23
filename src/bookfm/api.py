from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.websockets import WebSocketState
import uvicorn

from .api_models import BaseTextRequest, GenerateTextRequest
from .api_services import generate_live_from_document, inspect_payload, prepare_from_upload
from .config import (
    DEFAULT_ANALYSIS_MODEL,
    DEFAULT_CROSSFADE_SECONDS,
    DEFAULT_PREFETCH_COUNT,
    DEFAULT_READING_SPEED_WPM,
    DEFAULT_STREAM_GAIN,
    ENABLE_SEMANTIC_CHUNKING,
    OUTPUT_DIR,
    PER_SECTION_OVERHEAD_SECONDS,
    READING_PACE_BUFFER_RATIO,
    SECTION_MAX_STREAM_SECONDS,
    SECTION_MIN_STREAM_SECONDS,
)
from .lyria_session import LyriaSessionManager
from .pipeline import build_section_plans, prepare_document
from .timing import build_stream_durations

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)
# ────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="BookFM API", version="0.1.0")



def _resolve_ui_dir() -> Path:
    env_ui_dir = os.getenv("BOOKFM_UI_DIR")
    candidates = []
    if env_ui_dir:
        candidates.append(Path(env_ui_dir).expanduser())

    candidates.extend(
        [
            Path(__file__).resolve().parents[2] / "ui",
            Path.cwd() / "ui",
        ]
    )

    for candidate in candidates:
        if candidate.exists() and candidate.is_dir():
            return candidate

    # Keep first candidate as the default error path for clearer 404 messages.
    return candidates[0]


UI_DIR = _resolve_ui_dir()


def _ui_file(name: str) -> FileResponse:
    path = UI_DIR / name
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"UI asset not found: {path}")

    media_type = None
    if name.endswith(".css"):
        media_type = "text/css"
    elif name.endswith(".js"):
        media_type = "application/javascript"
    elif name.endswith(".png"):
        media_type = "image/png"
    elif name.endswith(".ico"):
        media_type = "image/x-icon"
    elif name.endswith(".webmanifest"):
        media_type = "application/manifest+json"

    return FileResponse(path, media_type=media_type)


def _multipart_available() -> bool:
    try:
        import multipart  # noqa: F401
    except Exception:
        return False
    return True


UPLOADS_AVAILABLE = _multipart_available()

# Read allowed origin from env for production. Falls back to localhost only in dev.
_ALLOWED_ORIGIN = os.getenv("BOOKFM_CORS_ORIGIN", "")
_CORS_ORIGINS = [o.strip() for o in _ALLOWED_ORIGIN.split(",") if o.strip()] or [
    "http://localhost:4173",
    "http://127.0.0.1:4173",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.get("/v1/health")
async def health() -> dict:
    return {
        "ok": True,
        "uploads_available": UPLOADS_AVAILABLE,
        "ui_available": UI_DIR.exists(),
        "ui_dir": str(UI_DIR),
        "streaming_available": True,
        "analysis_model": DEFAULT_ANALYSIS_MODEL,
        "stream_gain": DEFAULT_STREAM_GAIN,
        "timing": {
            "pace_buffer_ratio": READING_PACE_BUFFER_RATIO,
            "section_min_stream_seconds": SECTION_MIN_STREAM_SECONDS,
            "section_max_stream_seconds": SECTION_MAX_STREAM_SECONDS,
            "per_section_overhead_seconds": PER_SECTION_OVERHEAD_SECONDS,
        },
    }


@app.post("/v1/inspect")
async def inspect_text(req: BaseTextRequest) -> dict:
    document = await prepare_document(
        text=req.text,
        reading_speed_wpm=req.reading_speed_wpm,
        semantic=ENABLE_SEMANTIC_CHUNKING,
    )
    return inspect_payload(document)


@app.post("/v1/generate/live")
async def generate_live_text(req: GenerateTextRequest) -> dict:
    document = await prepare_document(
        text=req.text,
        reading_speed_wpm=req.reading_speed_wpm,
        semantic=ENABLE_SEMANTIC_CHUNKING,
    )
    return await generate_live_from_document(
        document=document,
        reading_speed_wpm=req.reading_speed_wpm,
        section_index=req.section_index,
        count=req.count,
        show_prompts=req.show_prompts,
    )


if UPLOADS_AVAILABLE:
    @app.post("/v1/inspect/upload")
    async def inspect_upload(
        file: UploadFile = File(...),
        reading_speed_wpm: int = Form(DEFAULT_READING_SPEED_WPM),
        semantic: bool = Form(ENABLE_SEMANTIC_CHUNKING),
    ) -> dict:
        document = await prepare_from_upload(
            file,
            reading_speed_wpm=reading_speed_wpm,
            semantic=ENABLE_SEMANTIC_CHUNKING,
        )
        return inspect_payload(document)


    @app.post("/v1/generate/live/upload")
    async def generate_live_upload(
        file: UploadFile = File(...),
        section_index: int = Form(0),
        count: int = Form(DEFAULT_PREFETCH_COUNT),
        reading_speed_wpm: int = Form(DEFAULT_READING_SPEED_WPM),
        semantic: bool = Form(ENABLE_SEMANTIC_CHUNKING),
        show_prompts: bool = Form(False),
    ) -> dict:
        document = await prepare_from_upload(
            file,
            reading_speed_wpm=reading_speed_wpm,
            semantic=ENABLE_SEMANTIC_CHUNKING,
        )
        return await generate_live_from_document(
            document=document,
            reading_speed_wpm=reading_speed_wpm,
            section_index=section_index,
            count=count,
            show_prompts=show_prompts,
        )


@app.get("/v1/files/{name}")
async def get_generated_file(name: str):
    safe_name = Path(name).name
    path = OUTPUT_DIR / safe_name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    media_type = "audio/wav" if path.suffix.lower() == ".wav" else "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=safe_name)


@app.websocket("/v1/stream/live")
async def stream_live_text(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        payload = await websocket.receive_json()
        
        gemini_api_key = str(payload.get("gemini_api_key") or "").strip()
        if not gemini_api_key:
            log.error("Rejected connection: Missing Gemini API Key")
            await websocket.send_json({"event": "error", "detail": "A valid Gemini API Key is required."})
            return

        text = str(payload.get("text") or "").strip()
        if not text:
            await websocket.send_json({"event": "error", "detail": "Text is required for live streaming."})
            return

        # Clamp params to safe ranges
        reading_speed_wpm = max(60, min(600, int(payload.get("reading_speed_wpm", DEFAULT_READING_SPEED_WPM))))
        section_index = max(0, int(payload.get("section_index", 0)))
        count = max(1, min(8, int(payload.get("count", DEFAULT_PREFETCH_COUNT))))
        show_prompts = bool(payload.get("show_prompts", False))

        # Log session info — never log the API key or raw key chars
        log.info("====== NEW READING SESSION ======")
        log.info("WPM: %d | Sections requested: %d", reading_speed_wpm, count)
        log.info("Text (first 200 chars): %r", text[:200])
        log.info("=================================")

        document = await prepare_document(
            text=text,
            reading_speed_wpm=reading_speed_wpm,
            semantic=ENABLE_SEMANTIC_CHUNKING,
            api_key=gemini_api_key,
        )
        if not document.sections:
            await websocket.send_json({"event": "error", "detail": "No readable sections were created from the input."})
            return

        planned_sections = await build_section_plans(
            document,
            api_key=gemini_api_key,
            reading_speed_wpm=reading_speed_wpm,
            start_index=section_index,
            count=count,
        )
        
        if planned_sections:
            first_plan = planned_sections[0][1]

        plans = [plan for _, plan, _ in planned_sections]
        section_window = [sec for sec, _, _ in planned_sections]
        durations = build_stream_durations(section_window, reading_speed_wpm=reading_speed_wpm)
        log.info(
            "[Timing] Section budgets: %s",
            ", ".join(
                f"#{sec.index} words={sec.word_count} paras={sec.paragraph_count} -> {dur}s"
                for sec, dur in zip(section_window, durations, strict=True)
            ),
        )
        log.info("[Timing] Total budget=%ds at %d WPM", sum(durations), reading_speed_wpm)
        min_duration = min(durations) if durations else 12
        crossfade_seconds = min(DEFAULT_CROSSFADE_SECONDS, max(2, int(min_duration * 0.25)))
        output_pcm = OUTPUT_DIR / f"live_{int(time.time() * 1000)}.pcm"

        await websocket.send_json(
            {
                "event": "session_start",
                "document": inspect_payload(document),
                "stream": {
                    "sample_rate": 48_000,
                    "channels": 2,
                    "sample_format": "s16le",
                },
                "playback": {
                    "start_section_index": max(0, min(section_index, len(document.sections) - 1)),
                    "section_count": len(planned_sections),
                    "crossfade_seconds": crossfade_seconds,
                    "durations": durations,
                },
            }
        )

        session_manager = LyriaSessionManager(api_key=gemini_api_key)

        async def send_chunk(data: bytes) -> None:
            await websocket.send_bytes(data)

        live_clip = await session_manager.stream_sections(
            plans,
            reading_speed_wpm=reading_speed_wpm,
            durations=durations,
            output_pcm=output_pcm,
            crossfade_seconds=crossfade_seconds,
            on_chunk=send_chunk,
        )

        await websocket.send_json(
            {
                "event": "complete",
                "mode": "live",
                "start_section_index": max(0, min(section_index, len(document.sections) - 1)),
                "section_count": len(planned_sections),
                "crossfade_seconds": crossfade_seconds,
                "pcm_path": str(live_clip.output_path),
                "wav_path": str(live_clip.output_path.with_suffix(".wav")),
                "bytes_written": live_clip.bytes_written,
                "duration_seconds": live_clip.duration_seconds,
                "sections": [
                    {
                        "section_index": section.index,
                        "estimated_seconds": section.estimated_seconds,
                        "stream_seconds": duration,
                    }
                    for (section, _, _), duration in zip(planned_sections, durations, strict=True)
                ],
                "model_inputs": [
                    {
                        "section_index": section.index,
                        "music_config": {
                            "bpm": plan.bpm,
                            "density": plan.density,
                            "brightness": plan.brightness,
                            "guidance": plan.guidance,
                            "temperature": plan.temperature,
                        },
                    }
                    for section, plan, _ in planned_sections
                ] if show_prompts else None,
            }
        )
    except WebSocketDisconnect:
        return
    except Exception as exc:
        log.exception(f"WebSocket error: {exc}")
        try:
            await websocket.send_json({"event": "error", "detail": str(exc)})
        except RuntimeError:
            pass
    finally:
        if websocket.application_state == WebSocketState.CONNECTED:
            await websocket.close()


@app.get("/")
async def index():
    return _ui_file("index.html")


@app.get("/room")
async def room():
    return _ui_file("room.html")


@app.get("/room/")
async def room_slash():
    return _ui_file("room.html")


@app.get("/room.html")
async def room_html():
    return _ui_file("room.html")


@app.get("/{name:path}")
async def static_files(name: str):
    return _ui_file(name)


def run() -> None:
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("bookfm.api:app", host="0.0.0.0", port=port, reload=False)


if __name__ == "__main__":
    run()
