import asyncio
import json
import logging
from dataclasses import asdict

import httpx

from app.clients.ieducar import IEducarClient, IEducarError
from app.clients.moodle import MoodleClient, MoodleError
from app.config import MoodleNotConfiguredError, Settings, load_moodle_setup
from app.db import Repository
from app.sync.professores import ProfessorSync, SyncResult

log = logging.getLogger(__name__)


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

    def moodle(self) -> MoodleClient:
        return MoodleClient(self._moodle_http, load_moodle_setup(self._settings).token)

    async def sync_professores(
        self, trigger: str, completo: bool = False
    ) -> SyncResult:
        if self._lock.locked():
            raise SyncAlreadyRunningError("Já existe uma sincronização em andamento.")
        async with self._lock:
            run_id = self._repo.start_run("professores", trigger)
            try:
                setup = load_moodle_setup(self._settings)
                sync = ProfessorSync(
                    self.ieducar(),
                    MoodleClient(self._moodle_http, setup.token),
                    self._repo,
                    setup,
                    self._settings.placeholder_email_domain,
                )
                result = await sync.run(completo=completo)
            except (
                MoodleNotConfiguredError,
                IEducarError,
                MoodleError,
                httpx.HTTPError,
            ) as exc:
                self._repo.finish_run(run_id, "failed", str(exc))
                raise
            status = "partial" if result.erros else "success"
            self._repo.finish_run(
                run_id, status, json.dumps(asdict(result), ensure_ascii=False)
            )
            return result

    async def run_periodically(self, interval: int) -> None:
        while True:
            try:
                result = await self.sync_professores("agendado")
                log.info("Sincronização agendada concluída: %s", result)
            except SyncAlreadyRunningError:
                log.info("Sincronização agendada pulada: outra execução em andamento")
            except Exception:
                log.exception("Sincronização agendada falhou")
            await asyncio.sleep(interval)
