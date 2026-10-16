import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request

from app.clients.ieducar import IEducarError
from app.clients.moodle import MoodleError
from app.config import MoodleNotConfiguredError, get_settings
from app.db import Repository
from app.sync.service import SyncAlreadyRunningError, SyncService

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    repo = Repository(settings.database_path)
    async with (
        httpx.AsyncClient(base_url=settings.ieducar_url, timeout=30) as ieducar_http,
        httpx.AsyncClient(base_url=settings.moodle_url, timeout=30) as moodle_http,
    ):
        service = SyncService(settings, repo, ieducar_http, moodle_http)
        app.state.repo = repo
        app.state.sync = service

        task = None
        if settings.sync_interval_seconds > 0:
            task = asyncio.create_task(
                service.run_periodically(settings.sync_interval_seconds)
            )
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


app = FastAPI(
    title="i-Educar ↔ Moodle Integration",
    description="Sincroniza dados do i-Educar (fonte da verdade) para o Moodle.",
    lifespan=lifespan,
)


def _sync(request: Request) -> SyncService:
    return request.app.state.sync


def _repo(request: Request) -> Repository:
    return request.app.state.repo


@app.get("/health", tags=["status"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/dependencias", tags=["status"])
async def health_dependencias(request: Request) -> dict[str, Any]:
    """Verifica se a integração consegue autenticar no i-Educar e no Moodle."""
    sync = _sync(request)
    checks: dict[str, Any] = {}
    try:
        servidores = await sync.ieducar().listar_servidores(modified="2999-01-01")
        checks["ieducar"] = {
            "ok": True,
            "detalhe": f"API acessível ({len(servidores)})",
        }
    except (IEducarError, httpx.HTTPError, ValueError) as exc:
        checks["ieducar"] = {"ok": False, "detalhe": str(exc)}
    try:
        info = await sync.moodle().get_site_info()
        checks["moodle"] = {
            "ok": True,
            "detalhe": f"{info['sitename']} (Moodle {info['release']})",
        }
    except (MoodleNotConfiguredError, MoodleError, httpx.HTTPError, ValueError) as exc:
        checks["moodle"] = {"ok": False, "detalhe": str(exc)}
    return checks


@app.post("/sync/professores", tags=["sincronização"])
async def sync_professores(request: Request, completo: bool = False) -> dict[str, Any]:
    """Sincronização manual. Com `completo=true` ignora o cursor e reprocessa todos os servidores."""
    try:
        result = await _sync(request).sync_professores("manual", completo=completo)
    except SyncAlreadyRunningError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MoodleNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (IEducarError, MoodleError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return asdict(result)


@app.get("/sync/execucoes", tags=["sincronização"])
def listar_execucoes(request: Request, limit: int = 20) -> dict[str, Any]:
    return {
        "em_andamento": _sync(request).running,
        "execucoes": _repo(request).list_runs(limit),
    }


@app.get("/professores", tags=["mapeamentos"])
def listar_professores(request: Request) -> list[dict[str, Any]]:
    """Mapeamento servidor do i-Educar ↔ usuário do Moodle."""
    return [asdict(p) for p in _repo(request).list_professores()]
