"""Interfaces dos sistemas integrados.

As sincronizações dependem destes protocolos, não dos clientes HTTP: os clientes de
`app.clients` os implementam e os testes usam versões em memória.
"""

from typing import Any, Protocol

from app.domain import (
    Aluno,
    ComponenteCurricular,
    Dispensa,
    Enturmacao,
    Escola,
    Matricula,
    Servidor,
    Turma,
    Vinculo,
)


class IEducarError(Exception):
    """A API do i-Educar recusou a consulta."""


class MoodleError(Exception):
    """O Moodle recusou a chamada; `errorcode` identifica o motivo."""

    def __init__(self, function: str, errorcode: str, message: str) -> None:
        super().__init__(f"{function}: {message} ({errorcode})")
        self.errorcode = errorcode


class IEducar(Protocol):
    """Fonte da verdade: leitura dos cadastros escolares."""

    async def listar_escolas(self) -> list[Escola]: ...

    async def ano_letivo_em_andamento(self, escola_id: int) -> int | None: ...

    async def listar_servidores(self) -> list[Servidor]:
        """Servidores da instituição, sem o e-mail."""
        ...

    async def obter_servidor(self, servidor_id: int) -> Servidor | None:
        """Servidor com o e-mail; None se não existe."""
        ...

    async def listar_vinculos(self, ano: int, escolas: list[int]) -> list[Vinculo]:
        """Vínculos professor ↔ turma do ano, incluindo os removidos."""
        ...

    async def listar_turmas(self, ano: int, escolas: list[int]) -> list[Turma]:
        """Turmas do ano, incluindo as desativadas."""
        ...

    async def listar_componentes(
        self, escola_id: int, serie_id: int, ano: int
    ) -> list[ComponenteCurricular]:
        """Componentes que a escola oferece na série; vazio se não houver."""
        ...

    async def listar_alunos(self, ano: int, escolas: list[int]) -> list[Aluno]: ...

    async def listar_matriculas(
        self, ano: int, escolas: list[int]
    ) -> list[Matricula]: ...

    async def listar_enturmacoes(
        self, ano: int, escolas: list[int]
    ) -> list[Enturmacao]: ...

    async def listar_dispensas(self, escolas: list[int]) -> list[Dispensa]:
        """Dispensas de componente por matrícula, de todos os anos."""
        ...


class Moodle(Protocol):
    """Destino: Web Services do Moodle. Os dicionários seguem o formato deles e as
    chamadas de criação e inscrição são transações (um item inválido desfaz todas)."""

    async def get_site_info(self) -> dict[str, Any]: ...

    async def get_users_by_field(
        self, field: str, values: list[str]
    ) -> list[dict[str, Any]]: ...

    async def create_users(self, users: list[dict[str, Any]]) -> dict[str, int]:
        """Devolve login → id."""
        ...

    async def update_user(self, user: dict[str, Any]) -> None: ...

    async def get_category_by_idnumber(
        self, idnumber: str
    ) -> dict[str, Any] | None: ...

    async def create_category(
        self, name: str, parent: int, idnumber: str, description: str = ""
    ) -> int: ...

    async def get_courses_by_field(
        self, field: str, value: str
    ) -> list[dict[str, Any]]: ...

    async def create_courses(self, courses: list[dict[str, Any]]) -> dict[str, int]:
        """Devolve nome curto → id."""
        ...

    async def update_course(self, course: dict[str, Any]) -> None: ...

    async def enrol_users(self, role_id: int, pares: list[tuple[int, int]]) -> None:
        """Inscreve pares (course_id, user_id); reinscrever não tem efeito."""
        ...

    async def unenrol_users(self, pares: list[tuple[int, int]]) -> None: ...
