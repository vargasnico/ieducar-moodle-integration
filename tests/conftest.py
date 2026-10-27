from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from app.config import MoodleSetup, Settings
from app.db import Repository
from app.domain import (
    Aluno,
    ComponenteCurricular,
    Dispensa,
    Enturmacao,
    Matricula,
    Servidor,
    Turma,
    Vinculo,
)
from app.sync.base import Resultado, Sincronizacao
from tests.fakes import FakeIEducar, FakeMoodle

PORTUGUES, MATEMATICA = 1, 2
TURMA, SERIE = 10, 5
SETUP = MoodleSetup(
    token="t", category_id=1, role_ids={"editingteacher": 3, "student": 5}
)

Rodar = Callable[..., Awaitable[Resultado]]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def ieducar() -> FakeIEducar:
    """Uma turma com Português e Matemática; a professora Ana leciona Português, o
    servidor Beto é da secretaria (sem turma). Carla e Davi estão enturmados, e Davi
    tem dispensa de Matemática."""
    fake = FakeIEducar()
    fake.servidores = {
        7: Servidor(7, "Ana Souza", "123.456.789-00", True, "2026-01-01", "ana@x.br"),
        8: Servidor(8, "Beto Lima", None, True, "2026-01-01"),
    }
    fake.vinculos = [Vinculo(1, 7, TURMA, (PORTUGUES,), True, "2026-01-01")]
    fake.turmas = [Turma(TURMA, "1º ano A", 2026, 1, (SERIE,), True, "2026-01-01")]
    fake.componentes = {
        SERIE: [
            ComponenteCurricular(PORTUGUES, "Português", "POR"),
            ComponenteCurricular(MATEMATICA, "Matemática", "MAT"),
        ]
    }
    fake.alunos = [
        Aluno(20, "Carla Dias", None, True, "2026-01-01"),
        Aluno(21, "Davi Reis", "Dani Reis", True, "2026-01-01"),
    ]
    fake.matriculas = [Matricula(200, 20, 1, True), Matricula(201, 21, 1, True)]
    fake.enturmacoes = [
        Enturmacao(1, 200, TURMA, SERIE, True),
        Enturmacao(2, 201, TURMA, SERIE, True),
    ]
    fake.dispensas = [Dispensa(201, MATEMATICA, True)]
    return fake


@pytest.fixture
def moodle() -> FakeMoodle:
    return FakeMoodle()


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    return Repository(tmp_path / "integration.db")


@pytest.fixture
def rodar(ieducar: FakeIEducar, moodle: FakeMoodle, repo: Repository) -> Rodar:
    """Executa uma sincronização contra os fakes."""

    async def rodar(
        classe: type[Sincronizacao], completo: bool = False, repo: Repository = repo
    ) -> Resultado:
        settings = Settings(ieducar_escola_id=None, ieducar_ano_letivo=None)
        return await classe(ieducar, moodle, repo, SETUP, settings, completo).run()

    return rodar
