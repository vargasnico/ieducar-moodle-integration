import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import timedelta
from typing import Any, TypeVar

import httpx

from app.clients.ieducar import IEducarClient
from app.clients.moodle import MoodleClient
from app.config import MoodleSetup, Settings, load_moodle_setup
from app.db import Repository
from app.sync.professores import ProfessorSync, SyncResult
from app.sync.turmas import TurmaSync, TurmaSyncResult

log = logging.getLogger(__name__)

R = TypeVar("R")


class SyncAlreadyRunningError(RuntimeError):
    pass


class SyncService:
    """Ponto único de entrada das sincronizações, usado pelo agendador e pelos endpoints."""

    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        ieducar_http: httpx.AsyncClient,
        moodle_http: httpx.AsyncClient,
    ) -> None:
        self._settings = settings
        self._repo = repo
        self._ieducar_http = ieducar_http
        self._moodle_http = moodle_http
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._lock.locked()

    def ieducar(self) -> IEducarClient:
        return IEducarClient(
            self._ieducar_http,
            self._settings.ieducar_access_key,
            self._settings.ieducar_instituicao_id,
        )

    def moodle(self, setup: MoodleSetup | None = None) -> MoodleClient:
        setup = setup or load_moodle_setup(self._settings)
        return MoodleClient(self._moodle_http, setup.token)

    async def _executar(
        self, entity: str, trigger: str, run: Callable[[], Awaitable[R]]
    ) -> R:
        """Executa uma sincronização sob a trava e registra o resultado em sync_run."""
        if self._lock.locked():
            raise SyncAlreadyRunningError("Já existe uma sincronização em andamento.")
        async with self._lock:
            run_id = self._repo.start_run(entity, trigger)
            try:
                result: Any = await run()
            except asyncio.CancelledError:
                self._repo.finish_run(run_id, "cancelled", "Execução interrompida.")
                raise
            except Exception as exc:
                # Qualquer erro fecha a execução; senão ela fica "running" para sempre.
                self._repo.finish_run(run_id, "failed", str(exc) or repr(exc))
                raise
            status = "partial" if result.erros else "success"
            self._repo.finish_run(
                run_id, status, json.dumps(asdict(result), ensure_ascii=False)
            )
            return result

    async def sync_professores(
        self, trigger: str, completo: bool = False
    ) -> SyncResult:
        def run() -> Awaitable[SyncResult]:
            return ProfessorSync(
                self.ieducar(),
                self.moodle(),
                self._repo,
                self._settings.placeholder_email_domain,
                escola_id=self._settings.ieducar_escola_id,
                ano_letivo=self._settings.ieducar_ano_letivo,
                cursor_overlap=timedelta(
                    hours=self._settings.sync_cursor_overlap_hours
                ),
            ).run(completo=completo)

        return await self._executar("professores", trigger, run)

    async def sync_turmas(
        self, trigger: str, completo: bool = False
    ) -> TurmaSyncResult:
        def run() -> Awaitable[TurmaSyncResult]:
            setup = load_moodle_setup(self._settings)
            return TurmaSync(
                self.ieducar(),
                self.moodle(setup),
                self._repo,
                setup,
                escola_id=self._settings.ieducar_escola_id,
                ano_letivo=self._settings.ieducar_ano_letivo,
            ).run(completo=completo)

        return await self._executar("turmas", trigger, run)

    async def run_periodically(self, interval: int) -> None:
        # Professores antes de turmas: as inscrições usam os usuários e os vínculos
        # que a sincronização de professores mantém.
        etapas: list[tuple[str, Callable[[], Awaitable[Any]]]] = [
            ("professores", lambda: self.sync_professores("agendado")),
            ("turmas", lambda: self.sync_turmas("agendado")),
        ]
        while True:
            for nome, etapa in etapas:
                try:
                    result = await etapa()
                    log.info("Sincronização agendada de %s concluída: %s", nome, result)
                except SyncAlreadyRunningError:
                    log.info(
                        "Sincronização agendada de %s pulada: outra execução em andamento",
                        nome,
                    )
                except Exception:
                    log.exception("Sincronização agendada de %s falhou", nome)
            await asyncio.sleep(interval)
