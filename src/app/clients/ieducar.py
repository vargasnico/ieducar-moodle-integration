from dataclasses import dataclass
from typing import Any

import httpx


class IEducarError(Exception):
    pass


@dataclass(frozen=True)
class Servidor:
    servidor_id: int
    nome: str
    cpf: str | None
    ativo: bool
    updated_at: str


class IEducarClient:
    """Cliente da API legada do i-Educar (/module/Api/*), autenticada por access_key."""

    def __init__(
        self, http: httpx.AsyncClient, access_key: str, instituicao_id: int
    ) -> None:
        self._http = http
        self._access_key = access_key
        self._instituicao_id = instituicao_id

    async def _get(
        self, controller: str, resource: str, **params: Any
    ) -> dict[str, Any]:
        query = {"oper": "get", "resource": resource, "access_key": self._access_key}
        query.update({k: v for k, v in params.items() if v is not None})
        response = await self._http.get(f"/module/Api/{controller}", params=query)
        response.raise_for_status()
        data = response.json()
        if data.get("any_error_msg"):
            msgs = "; ".join(m.get("msg", "") for m in data.get("msgs", []))
            raise IEducarError(f"{controller}/{resource}: {msgs}")
        return data

    async def listar_servidores(self, modified: str | None = None) -> list[Servidor]:
        """Servidores da instituição, opcionalmente só os alterados desde `modified`."""
        data = await self._get(
            "Servidor",
            "servidores",
            instituicao_id=self._instituicao_id,
            modified=modified,
        )
        return [
            Servidor(
                servidor_id=int(s["servidor_id"]),
                nome=str(s["nome"]).strip(),
                cpf=s.get("cpf") or None,
                ativo=str(s["ativo"]) == "1",
                updated_at=str(s["updated_at"]),
            )
            for s in data.get("servidores", [])
        ]

    async def obter_email_servidor(self, servidor_id: int) -> str | None:
        data = await self._get("Servidor", "dados-servidor", servidor_id=servidor_id)
        email = (data.get("result") or {}).get("email")
        return email.strip() if email else None
