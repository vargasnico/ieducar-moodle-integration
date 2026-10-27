import asyncio
import json
import logging
from dataclasses import asdict
from typing import Any, Literal

import httpx

from app.clients.ieducar import IEducarClient, IEducarError
from app.clients.moodle import MoodleClient, MoodleError
from app.config import (
    MoodleNotConfiguredError,
    MoodleSetup,
    Settings,
    load_moodle_setup,
)
from app.db import Repository
from app.sync.alunos import AlunoSync
from app.sync.base import Resultado, Sincronizacao
from app.sync.professores import ProfessorSync
from app.sync.turmas import TurmaSync

log = logging.getLogger(__name__)

Entidade = Literal["professores", "turmas", "alunos"]

# Em ordem de dependência: as inscrições de professores usam os usuários do caso 1,
# e as de alunos usam os cursos do caso 2.
SINCRONIZACOES: dict[Entidade, type[Sincronizacao]] = {
    "professores": ProfessorSync,
    "turmas": TurmaSync,
    "alunos": AlunoSync,
}


class SyncAlreadyRunningError(RuntimeError):
    pass


class SyncService:
    """Ponto único de entrada das sincronizações, usado pelo agendador e pelos
    endpoints: uma por vez, cada uma registrada no histórico de execuções."""

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

    async def sincronizar(
        self, entidade: Entidade, trigger: str, completo: bool = False
    ) -> Resultado:
        if self._lock.locked():
            raise SyncAlreadyRunningError("Já existe uma sincronização em andamento.")
        async with self._lock:
            run_id = self._repo.start_run(entidade, trigger)
            try:
                # Lido a cada execução: o arquivo pode surgir depois da API subir.
                setup = load_moodle_setup(self._settings)
                sync = SINCRONIZACOES[entidade](
                    self.ieducar(),
                    self.moodle(setup),
                    self._repo,
                    setup,
                    self._settings,
                    completo,
                )
                resultado = await sync.run()
            except asyncio.CancelledError:
                self._repo.finish_run(run_id, "cancelled", "Execução interrompida.")
                raise
            except Exception as exc:
                # Qualquer erro fecha a execução; senão ela fica "running" para sempre.
                self._repo.finish_run(run_id, "failed", str(exc) or repr(exc))
                raise
            status = "partial" if resultado.erros else "success"
            resumo = json.dumps(asdict(resultado), ensure_ascii=False)
            self._repo.finish_run(run_id, status, resumo)
            return resultado

    async def run_periodically(self, interval: int) -> None:
        while True:
            for entidade in SINCRONIZACOES:
                try:
                    resultado = await self.sincronizar(entidade, "agendado")
                    log.info("Sincronização de %s concluída: %s", entidade, resultado)
                except SyncAlreadyRunningError:
                    log.info("Sincronização de %s pulada: outra em andamento", entidade)
                except Exception:
                    log.exception("Sincronização agendada de %s falhou", entidade)
            await asyncio.sleep(interval)

    async def verificar_dependencias(self) -> dict[str, Any]:
        """Confere se a integração consegue autenticar no i-Educar e no Moodle."""
        checks: dict[str, Any] = {}
        try:
            ieducar = self.ieducar()
            anos: dict[str, Any] = {}
            for escola in await ieducar.listar_escolas():
                if not escola.ativo:
                    continue
                # Uma escola sem ano letivo não indica que a API esteja fora do ar.
                try:
                    anos[escola.nome] = await ieducar.ano_letivo_em_andamento(
                        escola.escola_id
                    )
                except (IEducarError, httpx.HTTPStatusError) as exc:
                    anos[escola.nome] = f"erro: {exc}"
            checks["ieducar"] = {
                "ok": True,
                "detalhe": "API acessível",
                "escolas_ativas": anos,
            }
        except (IEducarError, httpx.HTTPError, ValueError) as exc:
            checks["ieducar"] = {"ok": False, "detalhe": str(exc)}
        try:
            info = await self.moodle().get_site_info()
            detalhe = f"{info['sitename']} (Moodle {info['release']})"
            checks["moodle"] = {"ok": True, "detalhe": detalhe}
        except (
            MoodleNotConfiguredError,
            MoodleError,
            httpx.HTTPError,
            ValueError,
        ) as exc:
            checks["moodle"] = {"ok": False, "detalhe": str(exc)}
        return checks
