from typing import Any

import httpx


class MoodleError(Exception):
    def __init__(self, function: str, errorcode: str, message: str) -> None:
        super().__init__(f"{function}: {message} ({errorcode})")
        self.errorcode = errorcode


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Converte {"users": [{"username": "x"}]} em {"users[0][username]": "x"} (formato REST do Moodle)."""
    if isinstance(value, dict):
        items = value.items()
    elif isinstance(value, list):
        items = enumerate(value)
    else:
        return {prefix: int(value) if isinstance(value, bool) else value}
    flat: dict[str, Any] = {}
    for key, item in items:
        flat.update(_flatten(item, f"{prefix}[{key}]" if prefix else str(key)))
    return flat


class MoodleClient:
    """Cliente dos Web Services REST do Moodle."""

    def __init__(self, http: httpx.AsyncClient, token: str) -> None:
        self._http = http
        self._token = token

    async def call(self, function: str, **params: Any) -> Any:
        data = {
            "wstoken": self._token,
            "wsfunction": function,
            "moodlewsrestformat": "json",
        }
        data.update(_flatten(params))
        response = await self._http.post("/webservice/rest/server.php", data=data)
        response.raise_for_status()
        try:
            result = response.json()
        except ValueError as exc:
            raise MoodleError(
                function, "invalidresponse", "resposta não é JSON"
            ) from exc
        if isinstance(result, dict) and "exception" in result:
            raise MoodleError(
                function, result.get("errorcode", ""), result.get("message", "")
            )
        return result

    async def get_site_info(self) -> dict[str, Any]:
        return await self.call("core_webservice_get_site_info")

    async def get_user_by_field(self, field: str, value: str) -> dict[str, Any] | None:
        users = await self.call(
            "core_user_get_users_by_field", field=field, values=[value]
        )
        return users[0] if users else None

    async def create_user(self, user: dict[str, Any]) -> int:
        created = await self.call("core_user_create_users", users=[user])
        return int(created[0]["id"])

    async def update_user(self, user: dict[str, Any]) -> None:
        # Erros por usuário (id inválido, usuário excluído, e-mail duplicado...) não
        # viram exceção no Moodle: voltam como warnings e o usuário fica sem alteração.
        result = await self.call("core_user_update_users", users=[user])
        warnings = result.get("warnings") if isinstance(result, dict) else None
        if warnings:
            raise MoodleError(
                "core_user_update_users",
                str(warnings[0].get("warningcode", "")),
                str(warnings[0].get("message", "")),
            )

    async def assign_role(self, role_id: int, user_id: int, context_id: int) -> None:
        await self.call(
            "core_role_assign_roles",
            assignments=[
                {"roleid": role_id, "userid": user_id, "contextid": context_id}
            ],
        )

    async def get_category_by_idnumber(self, idnumber: str) -> dict[str, Any] | None:
        categories = await self.call(
            "core_course_get_categories",
            criteria=[{"key": "idnumber", "value": idnumber}],
            addsubcategories=False,
        )
        return categories[0] if categories else None

    async def create_category(
        self, name: str, parent: int, idnumber: str, description: str = ""
    ) -> int:
        created = await self.call(
            "core_course_create_categories",
            categories=[
                {
                    "name": name,
                    "parent": parent,
                    "idnumber": idnumber,
                    "description": description,
                }
            ],
        )
        return int(created[0]["id"])

    async def get_courses_by_field(
        self, field: str, value: str
    ) -> list[dict[str, Any]]:
        result = await self.call(
            "core_course_get_courses_by_field", field=field, value=value
        )
        courses = result.get("courses") if isinstance(result, dict) else None
        return courses or []

    async def get_course_by_field(
        self, field: str, value: str
    ) -> dict[str, Any] | None:
        courses = await self.get_courses_by_field(field, value)
        return courses[0] if courses else None

    async def create_courses(self, courses: list[dict[str, Any]]) -> dict[str, int]:
        """Cria em uma transação (um curso inválido desfaz o lote); devolve shortname → id."""
        created = await self.call("core_course_create_courses", courses=courses)
        return {str(c["shortname"]): int(c["id"]) for c in created}

    async def update_course(self, course: dict[str, Any]) -> None:
        # Como em core_user_update_users, erros por curso voltam como warnings.
        result = await self.call("core_course_update_courses", courses=[course])
        warnings = result.get("warnings") if isinstance(result, dict) else None
        if warnings:
            raise MoodleError(
                "core_course_update_courses",
                str(warnings[0].get("warningcode", "")),
                str(warnings[0].get("message", "")),
            )

    async def enrol_users(self, role_id: int, pares: list[tuple[int, int]]) -> None:
        """Inscrição manual de pares (course_id, user_id), em uma transação; reinscrever
        quem já está inscrito não tem efeito."""
        await self.call(
            "enrol_manual_enrol_users",
            enrolments=[
                {"roleid": role_id, "userid": user_id, "courseid": course_id}
                for course_id, user_id in pares
            ],
        )

    async def unenrol_users(self, pares: list[tuple[int, int]]) -> None:
        """Remove inscrições manuais de pares (course_id, user_id), em uma transação."""
        await self.call(
            "enrol_manual_unenrol_users",
            enrolments=[
                {"userid": user_id, "courseid": course_id}
                for course_id, user_id in pares
            ],
        )
