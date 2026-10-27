"""i-Educar e Moodle em memória, com o comportamento da API real que a integração usa."""

from dataclasses import replace
from itertools import count
from typing import Any

import httpx

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
from app.interfaces import MoodleError


class FakeIEducar:
    def __init__(self) -> None:
        self.ano = 2026
        self.escolas = [Escola(1, "Escola Exemplo", True)]
        self.servidores: dict[int, Servidor] = {}
        self.vinculos: list[Vinculo] = []
        self.turmas: list[Turma] = []
        self.componentes: dict[int, list[ComponenteCurricular]] = {}  # por série
        self.alunos: list[Aluno] = []
        self.matriculas: list[Matricula] = []
        self.enturmacoes: list[Enturmacao] = []
        self.dispensas: list[Dispensa] = []

    async def listar_escolas(self) -> list[Escola]:
        return self.escolas

    async def ano_letivo_em_andamento(self, escola_id: int) -> int | None:
        return self.ano

    async def listar_servidores(self) -> list[Servidor]:
        # Como na API real, a listagem não traz o e-mail.
        return [replace(s, email=None) for s in self.servidores.values()]

    async def obter_servidor(self, servidor_id: int) -> Servidor | None:
        return self.servidores.get(servidor_id)

    async def listar_vinculos(self, ano: int, escolas: list[int]) -> list[Vinculo]:
        return self.vinculos

    async def listar_turmas(self, ano: int, escolas: list[int]) -> list[Turma]:
        return self.turmas

    async def listar_componentes(
        self, escola_id: int, serie_id: int, ano: int
    ) -> list[ComponenteCurricular]:
        return self.componentes.get(serie_id, [])

    async def listar_alunos(self, ano: int, escolas: list[int]) -> list[Aluno]:
        return self.alunos

    async def listar_matriculas(self, ano: int, escolas: list[int]) -> list[Matricula]:
        return self.matriculas

    async def listar_enturmacoes(
        self, ano: int, escolas: list[int]
    ) -> list[Enturmacao]:
        return self.enturmacoes

    async def listar_dispensas(self, escolas: list[int]) -> list[Dispensa]:
        return self.dispensas


class FakeMoodle:
    def __init__(self) -> None:
        self._ids = count(100)
        self.users: dict[int, dict[str, Any]] = {}
        self.categories: dict[int, dict[str, Any]] = {}
        self.courses: dict[int, dict[str, Any]] = {}
        self.enrolments: set[tuple[int, int, int]] = set()  # (curso, usuário, papel)
        self.chamadas: list[str] = []
        # Funções que respondem com erro de rede, como num Moodle fora do ar.
        self.fora_do_ar: set[str] = set()
        # Logins que o Moodle recusa (dado inválido): derrubam o lote inteiro.
        self.recusar: set[str] = set()

    def _chamar(self, funcao: str) -> None:
        self.chamadas.append(funcao)
        if funcao in self.fora_do_ar:
            raise httpx.ConnectError(f"{funcao}: Moodle fora do ar")

    async def get_site_info(self) -> dict[str, Any]:
        return {"sitename": "Moodle de teste", "release": "5.0"}

    async def get_users_by_field(
        self, field: str, values: list[str]
    ) -> list[dict[str, Any]]:
        self._chamar("get_users_by_field")
        return [u for u in self.users.values() if u.get(field) in values]

    async def create_users(self, users: list[dict[str, Any]]) -> dict[str, int]:
        self._chamar("create_users")
        logins = {u["username"] for u in self.users.values()}
        for user in users:
            if user["username"] in logins | self.recusar:
                raise MoodleError("core_user_create_users", "invalidparameter", "")
        criados = {}
        for user in users:
            user_id = next(self._ids)
            self.users[user_id] = {**user, "id": user_id, "suspended": False}
            criados[user["username"]] = user_id
        return criados

    async def update_user(self, user: dict[str, Any]) -> None:
        self._chamar("update_user")
        if user["id"] not in self.users:
            raise MoodleError("core_user_update_users", "invaliduserid", "")
        self.users[user["id"]].update(user)

    async def get_category_by_idnumber(self, idnumber: str) -> dict[str, Any] | None:
        return next(
            (c for c in self.categories.values() if c["idnumber"] == idnumber), None
        )

    async def create_category(
        self, name: str, parent: int, idnumber: str, description: str = ""
    ) -> int:
        category_id = next(self._ids)
        self.categories[category_id] = {"id": category_id, "idnumber": idnumber}
        return category_id

    async def get_courses_by_field(
        self, field: str, value: str
    ) -> list[dict[str, Any]]:
        campo = "categoryid" if field == "category" else field
        return [c for c in self.courses.values() if str(c.get(campo)) == value]

    async def create_courses(self, courses: list[dict[str, Any]]) -> dict[str, int]:
        self._chamar("create_courses")
        nomes = {c["shortname"] for c in self.courses.values()}
        if any(c["shortname"] in nomes for c in courses):
            raise MoodleError("core_course_create_courses", "shortnametaken", "")
        criados = {}
        for course in courses:
            course_id = next(self._ids)
            self.courses[course_id] = {**course, "id": course_id}
            criados[course["shortname"]] = course_id
        return criados

    async def update_course(self, course: dict[str, Any]) -> None:
        self._chamar("update_course")
        if course["id"] not in self.courses:
            raise MoodleError("core_course_update_courses", "invalidcourseid", "")
        self.courses[course["id"]].update(course)

    async def enrol_users(self, role_id: int, pares: list[tuple[int, int]]) -> None:
        self._chamar("enrol_users")
        self.enrolments |= {(course, user, role_id) for course, user in pares}

    async def unenrol_users(self, pares: list[tuple[int, int]]) -> None:
        self._chamar("unenrol_users")
        self.enrolments = {e for e in self.enrolments if e[:2] not in set(pares)}

    # Consultas usadas nos testes.

    def usuario(self, idnumber: str) -> dict[str, Any]:
        return next(u for u in self.users.values() if u.get("idnumber") == idnumber)

    def curso(self, idnumber: str) -> dict[str, Any]:
        return next(c for c in self.courses.values() if c.get("idnumber") == idnumber)

    def inscritos(self, idnumber_curso: str) -> set[str]:
        course_id = self.curso(idnumber_curso)["id"]
        return {
            self.users[user]["username"]
            for course, user, _ in self.enrolments
            if course == course_id
        }
