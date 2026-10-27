import logging
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import ClassVar

from app.config import MoodleNotConfiguredError, MoodleSetup, Settings
from app.db import Repository
from app.domain import ComponenteCurricular
from app.interfaces import IEducar, Moodle

log = logging.getLogger(__name__)


@dataclass
class Resultado:
    """Resumo de uma execução: contagem de cada evento e erros por registro."""

    escolas: list[int] = field(default_factory=list)
    ano: int | None = None
    contagens: dict[str, int] = field(default_factory=dict)
    erros: list[str] = field(default_factory=list)

    def conta(self, evento: str, n: int = 1) -> None:
        self.contagens[evento] = self.contagens.get(evento, 0) + n

    def erro(self, onde: str, exc: Exception) -> None:
        log.warning("Falha em %s: %s", onde, exc)
        self.erros.append(f"{onde}: {exc}")


class Sincronizacao(ABC):
    """Uma sincronização i-Educar → Moodle. `run` descobre a escola e o ano letivo
    e delega a regra de cada caso a `executar` (template method)."""

    # Papel do Moodle nas inscrições do caso; vazio se o caso não inscreve.
    papel: ClassVar[str] = ""

    def __init__(
        self,
        ieducar: IEducar,
        moodle: Moodle,
        repo: Repository,
        setup: MoodleSetup,
        settings: Settings,
        completo: bool = False,
    ) -> None:
        self.ieducar = ieducar
        self.moodle = moodle
        self.repo = repo
        self.setup = setup
        self.settings = settings
        # Reenvia ao Moodle mesmo o que não mudou (corrige alterações feitas lá).
        self.completo = completo
        self.resultado = Resultado()
        self._componentes: dict[tuple[int, int], list[ComponenteCurricular]] = {}

    async def run(self) -> Resultado:
        # Conferido antes de qualquer alteração no Moodle.
        if self.papel and self.papel not in self.setup.role_ids:
            raise MoodleNotConfiguredError(
                f"Sem o id do papel {self.papel} no arquivo do moodle-setup;"
                " rode o serviço moodle-setup de novo."
            )
        ano = await self._ano_letivo()
        if ano is not None:
            await self.executar(ano, self.resultado.escolas)
        return self.resultado

    @abstractmethod
    async def executar(self, ano: int, escolas: list[int]) -> None: ...

    async def _ano_letivo(self) -> int | None:
        """Usa a escola e o ano configurados ou descobre pela API: as escolas ativas
        e o ano letivo em andamento da primeira delas."""
        escola_id = self.settings.ieducar_escola_id
        ano = self.settings.ieducar_ano_letivo
        if escola_id is not None:
            escolas = [escola_id]
        else:
            escolas = [
                e.escola_id for e in await self.ieducar.listar_escolas() if e.ativo
            ]
        self.resultado.escolas = escolas
        if not escolas:
            self.resultado.erros.append("Nenhuma escola ativa no i-Educar.")
            return None
        if ano is None:
            if len(escolas) > 1:
                log.warning(
                    "Mais de uma escola ativa (%s); usando o ano letivo da primeira. "
                    "Defina IEDUCAR_ANO_LETIVO para escolher.",
                    escolas,
                )
            ano = await self.ieducar.ano_letivo_em_andamento(escolas[0])
        if ano is None:
            self.resultado.erros.append("Nenhum ano letivo em andamento na escola.")
        self.resultado.ano = ano
        return ano

    async def componentes(
        self, escola_id: int, series: Iterable[int], ano: int
    ) -> dict[int, ComponenteCurricular]:
        """Componentes que a escola oferece nas séries, por id (uma turma multisseriada
        tem várias). Consulta cada série uma vez por execução."""
        por_id: dict[int, ComponenteCurricular] = {}
        for serie_id in series:
            key = (escola_id, serie_id)
            if key not in self._componentes:
                self._componentes[key] = await self.ieducar.listar_componentes(
                    escola_id, serie_id, ano
                )
            for componente in self._componentes[key]:
                por_id.setdefault(componente.componente_id, componente)
        return por_id

    def email_provisorio(self, idnumber: str) -> str:
        """E-mail de quem não tem um no i-Educar (o Moodle exige e-mail)."""
        return f"{idnumber}@{self.settings.placeholder_email_domain}"
