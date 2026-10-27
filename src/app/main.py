import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import MoodleNotConfiguredError, get_settings
from app.db import Repository
from app.interfaces import IEducarError, MoodleError
from app.sync.service import Entidade, SyncAlreadyRunningError, SyncService
from app.sync.usuarios import ALUNO, PROFESSOR

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

# Status HTTP de cada erro que pode chegar às rotas.
STATUS_DOS_ERROS: dict[type[Exception], int] = {
    SyncAlreadyRunningError: 409,
    MoodleNotConfiguredError: 503,
    IEducarError: 502,
    MoodleError: 502,
    httpx.HTTPError: 502,
}


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


async def _erro_http(request: Request, exc: Exception) -> JSONResponse:
    status = next(s for tipo, s in STATUS_DOS_ERROS.items() if isinstance(exc, tipo))
    return JSONResponse({"detail": str(exc)}, status_code=status)


for tipo in STATUS_DOS_ERROS:
    app.add_exception_handler(tipo, _erro_http)


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
    return await _sync(request).verificar_dependencias()


@app.post("/sync/{entidade}", tags=["sincronização"])
async def sincronizar(
    request: Request, entidade: Entidade, completo: bool = False
) -> dict[str, Any]:
    """Sincronização manual. Rode na ordem professores, turmas e alunos: as turmas
    inscrevem os professores, e os alunos são inscritos nos cursos das turmas. Com
    `completo=true` reenvia ao Moodle tudo o que a integração mantém, corrigindo
    alterações feitas direto no Moodle."""
    return asdict(await _sync(request).sincronizar(entidade, "manual", completo))


@app.get("/sync/execucoes", tags=["sincronização"])
def listar_execucoes(request: Request, limit: int = 20) -> dict[str, Any]:
    return {
        "em_andamento": _sync(request).running,
        "execucoes": _repo(request).list_runs(limit),
    }


@app.get("/professores", tags=["mapeamentos"])
def listar_professores(request: Request) -> list[dict[str, Any]]:
    """Mapeamento servidor do i-Educar ↔ usuário do Moodle."""
    return [asdict(u) for u in _repo(request).list_usuarios(PROFESSOR)]


@app.get("/alunos", tags=["mapeamentos"])
def listar_alunos(request: Request) -> list[dict[str, Any]]:
    """Mapeamento aluno do i-Educar ↔ usuário do Moodle."""
    return [asdict(u) for u in _repo(request).list_usuarios(ALUNO)]


@app.get("/cursos", tags=["mapeamentos"])
def listar_cursos(request: Request, ano: int | None = None) -> list[dict[str, Any]]:
    """Mapeamento turma × componente curricular do i-Educar ↔ curso do Moodle."""
    return [asdict(c) for c in _repo(request).list_cursos(ano)]
