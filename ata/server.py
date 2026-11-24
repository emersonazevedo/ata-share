"""API FastAPI + página local. Sem framework de frontend: HTML/JS/CSS servidos daqui."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import __version__
from . import db as dbmod
from .asr import warm_up
from .audio import list_devices, list_devices_full
from .config import Config, load_config
from .db import Database
from .events import EventHub
from .pipeline import MeetingPipeline
from .summarize import OllamaClient

log = logging.getLogger("ata.server")
WEB_DIR = Path(__file__).resolve().parents[1] / "web"

state: dict[str, object] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg: Config = load_config()
    cfg.ensure_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(cfg.logs_path / "ata.log", encoding="utf-8")],
    )
    hub = EventHub()
    hub.bind_loop(asyncio.get_running_loop())
    database = Database(cfg.db_path)
    pipeline = MeetingPipeline(cfg, database, hub)
    state.update(cfg=cfg, hub=hub, db=database, pipeline=pipeline)

    pipeline.recover()   # não bloqueia: agenda a recuperação numa thread
    asyncio.get_running_loop().run_in_executor(None, warm_up, cfg)              # modelo ASR quente
    asyncio.get_running_loop().run_in_executor(None, OllamaClient(cfg.ollama).warm, cfg.ollama.model)
    log.info("ata v%s pronto em http://%s:%d", __version__, cfg.server.host, cfg.server.port)
    try:
        yield
    finally:
        await run_in_threadpool(pipeline.shutdown)   # encerra captura ativa com ordem
        database.close()


app = FastAPI(title="ata", version=__version__, lifespan=lifespan)


def _cfg() -> Config:
    return state["cfg"]  # type: ignore[return-value]


def _pipe() -> MeetingPipeline:
    return state["pipeline"]  # type: ignore[return-value]


def _db() -> Database:
    return state["db"]  # type: ignore[return-value]


def _hub() -> EventHub:
    return state["hub"]  # type: ignore[return-value]


# --- páginas estáticas -----------------------------------------------------
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


# --- API -----------------------------------------------------------------
@app.get("/api/health")
async def health() -> dict:
    cfg = _cfg()
    try:
        models = await run_in_threadpool(OllamaClient(cfg.ollama).model_names)
        ollama = {"up": True, "models": models, "configured": cfg.ollama.model,
                  "configured_present": cfg.ollama.model in models}
    except Exception as error:  # noqa: BLE001
        ollama = {"up": False, "error": str(error)}
    return {
        "version": __version__,
        "active_meeting": _pipe().active_id,
        "asr_model": cfg.asr.model,
        "devices": await run_in_threadpool(list_devices),
        "ollama": ollama,
    }


@app.get("/api/devices")
async def devices() -> dict:
    return await run_in_threadpool(list_devices_full)


@app.get("/api/meetings")
async def list_meetings() -> list[dict]:
    return await run_in_threadpool(_db().list_meetings)


@app.post("/api/meetings")
async def create_meeting(body: dict) -> dict:
    title = str(body.get("title", "")).strip()
    language = str(body.get("language", "pt"))
    mic_device_index = body.get("mic_device_index")
    system_device_index = body.get("system_device_index")
    try:
        meeting = await run_in_threadpool(
            _pipe().start, title, language,
            int(mic_device_index) if mic_device_index is not None else None,
            int(system_device_index) if system_device_index is not None else None,
        )
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    project_fields = {k: body.get(k) for k in ("ticket", "project_name", "client", "cloud") if body.get(k)}
    if project_fields:
        await run_in_threadpool(_db().update_project_metadata, meeting["id"], **project_fields)
        meeting = await run_in_threadpool(_db().get_meeting, meeting["id"])
    return meeting


@app.post("/api/meetings/{meeting_id}/stop")
async def stop_meeting(meeting_id: str) -> dict:
    pipe = _pipe()
    if pipe.active_id != meeting_id:
        raise HTTPException(status_code=409, detail="Essa reunião não está ativa.")
    try:
        return await run_in_threadpool(pipe.stop)
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/meetings/{meeting_id}/cancel-auto-stop")
async def cancel_auto_stop(meeting_id: str) -> dict:
    try:
        _pipe().cancel_auto_stop(meeting_id)
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"ok": True}


@app.get("/api/meetings/{meeting_id}")
async def get_meeting(meeting_id: str) -> dict:
    db = _db()
    meeting = await run_in_threadpool(db.get_meeting, meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="Reunião não encontrada.")
    return {
        "meeting": meeting,
        "segments": await run_in_threadpool(db.get_segments, meeting_id),
        "summary": await run_in_threadpool(db.get_summary, meeting_id),
        "notes": await run_in_threadpool(db.get_notes, meeting_id),
    }


@app.put("/api/meetings/{meeting_id}/project")
async def put_project(meeting_id: str, body: dict) -> dict:
    db = _db()
    if await run_in_threadpool(db.get_meeting, meeting_id) is None:
        raise HTTPException(status_code=404, detail="Reunião não encontrada.")
    fields = {k: (str(body[k]).strip() or None) for k in ("ticket", "project_name", "client", "cloud")
              if k in body}
    await run_in_threadpool(db.update_project_metadata, meeting_id, **fields)
    return await run_in_threadpool(db.get_meeting, meeting_id)


@app.post("/api/meetings/{meeting_id}/export-obsidian")
async def export_obsidian(meeting_id: str) -> dict:
    cfg = _cfg()
    if not cfg.obsidian.enabled:
        raise HTTPException(status_code=400, detail="Exportação para Obsidian está desligada (obsidian.enabled=false).")
    if await run_in_threadpool(_db().get_meeting, meeting_id) is None:
        raise HTTPException(status_code=404, detail="Reunião não encontrada.")
    result = await run_in_threadpool(_pipe().export_obsidian, meeting_id)
    if result["status"] == "failed":
        raise HTTPException(status_code=400, detail=result["error"] or "Falha desconhecida na exportação.")
    return result


@app.put("/api/meetings/{meeting_id}/notes")
async def put_notes(meeting_id: str, body: dict) -> dict:
    await run_in_threadpool(_db().save_notes, meeting_id, str(body.get("text", "")))
    await run_in_threadpool(_pipe().export, meeting_id)  # regenera docs com a nota
    return {"ok": True}


@app.post("/api/meetings/{meeting_id}/export")
async def export_meeting(meeting_id: str) -> dict:
    try:
        return await run_in_threadpool(_pipe().export, meeting_id)
    except Exception as error:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/meetings/{meeting_id}/open-folder")
async def open_folder(meeting_id: str) -> dict:
    try:
        folder = await run_in_threadpool(_pipe().open_folder, meeting_id)
    except RuntimeError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"ok": True, "folder": folder}


@app.post("/api/meetings/{meeting_id}/reprocess")
async def reprocess_meeting(meeting_id: str, body: dict) -> dict:
    beam = int(body.get("beam_size", 5))
    try:
        await run_in_threadpool(_pipe().reprocess, meeting_id, beam)
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"ok": True, "beam_size": beam}


@app.delete("/api/meetings/{meeting_id}")
async def delete_meeting(meeting_id: str) -> dict:
    try:
        await run_in_threadpool(_pipe().delete, meeting_id)
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"ok": True}


@app.post("/api/dashboard/rebuild")
async def rebuild_dashboard() -> dict:
    cfg = _cfg()
    if not cfg.obsidian.dashboard_enabled:
        raise HTTPException(status_code=400, detail="Dashboard desligado (obsidian.dashboard_enabled=false).")
    result = await run_in_threadpool(_pipe().rebuild_dashboard)
    if result["status"] == "failed":
        raise HTTPException(status_code=400, detail=result["error"] or "Falha desconhecida no rebuild do Dashboard.")
    return result


@app.get("/api/search")
async def search(q: str = "") -> list[dict]:
    return await run_in_threadpool(_db().search, q)


@app.get("/api/meetings/{meeting_id}/events")
async def meeting_events(meeting_id: str, request: Request, replay: int = 1) -> StreamingResponse:
    hub = _hub()

    async def gen():
        yield ": conectado\n\n"
        async for event in hub.subscribe(meeting_id, replay=bool(replay)):
            if await request.is_disconnected():
                break
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive",
    })


@app.exception_handler(Exception)
async def unhandled(_request: Request, exc: Exception):  # pragma: no cover
    log.exception("erro não tratado")
    return JSONResponse(status_code=500, content={"detail": str(exc)})
