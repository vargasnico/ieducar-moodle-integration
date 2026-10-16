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
        result = response.json()
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
        await self.call("core_user_update_users", users=[user])

    async def assign_role(self, role_id: int, user_id: int, context_id: int) -> None:
        await self.call(
            "core_role_assign_roles",
            assignments=[
                {"roleid": role_id, "userid": user_id, "contextid": context_id}
            ],
        )
