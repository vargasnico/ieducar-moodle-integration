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
    email: str | None = None


@dataclass(frozen=True)
class Escola:
    escola_id: int
    nome: str
    ativo: bool


@dataclass(frozen=True)
class Vinculo:
    """Alocação de um servidor como professor numa turma (modules.professor_turma)."""

    vinculo_id: int
    servidor_id: int
    turma_id: int
    componentes: tuple[int, ...]
    ativo: bool
    updated_at: str


@dataclass(frozen=True)
class Turma:
    turma_id: int
    nome: str
    ano: int
    escola_id: int
    # Uma turma multisseriada tem mais de uma série.
    series: tuple[int, ...]
    ativo: bool
    updated_at: str


@dataclass(frozen=True)
class ComponenteCurricular:
    componente_id: int
    nome: str
    abreviatura: str


def _ativo(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "t"}


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
        try:
            data = response.json()
        except ValueError as exc:
            # Chave de acesso inválida, por exemplo, devolve a página HTML de login.
            raise IEducarError(f"{controller}/{resource}: resposta não é JSON") from exc
        if data.get("any_error_msg"):
            msgs = "; ".join(m.get("msg", "") for m in data.get("msgs", []))
            raise IEducarError(f"{controller}/{resource}: {msgs}")
        return data

    async def listar_escolas(self) -> list[Escola]:
        try:
            data = await self._get("Escola", "info-escolas")
        except IEducarError as exc:
            # Sem escolas cadastradas a API responde com erro e "escolas": 0.
            if "não existem escolas" in str(exc):
                return []
            raise
        return [
            Escola(
                escola_id=int(e["cod_escola"]),
                nome=str(e.get("nome") or "").strip(),
                ativo=_ativo(e.get("ativo")),
            )
            for e in data.get("escolas") or []
        ]

    async def ano_letivo_em_andamento(self, escola_id: int) -> int | None:
        data = await self._get("Escola", "escola-ano-letivo", escola_id=escola_id)
        ano = data.get("ano")
        return int(ano) if ano else None

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
                ativo=_ativo(s["ativo"]),
                updated_at=str(s["updated_at"]),
            )
            for s in data.get("servidores") or []
        ]

    async def obter_servidor(self, servidor_id: int) -> Servidor | None:
        data = await self._get("Servidor", "dados-servidor", servidor_id=servidor_id)
        s = data.get("result")
        if not s:
            return None
        email = (s.get("email") or "").strip()
        return Servidor(
            servidor_id=int(s["servidor_id"]),
            nome=str(s["nome"]).strip(),
            cpf=s.get("cpf") or None,
            ativo=_ativo(s["ativo"]),
            updated_at=str(s["updated_at"]),
            email=email or None,
        )

    async def listar_vinculos(
        self, ano: int, escolas: list[int], modified: str | None = None
    ) -> list[Vinculo]:
        """Vínculos professor ↔ turma do ano, incluindo os removidos (deleted_at)."""
        data = await self._get(
            "Servidor",
            "servidores-disciplinas-turmas",
            instituicao_id=self._instituicao_id,
            ano=ano,
            escola=",".join(str(e) for e in escolas),
            modified=modified,
        )
        return [
            Vinculo(
                vinculo_id=int(v["id"]),
                servidor_id=int(v["servidor_id"]),
                turma_id=int(v["turma_id"]),
                componentes=tuple(int(d["id"]) for d in v.get("disciplinas") or []),
                ativo=not v.get("deleted_at"),
                updated_at=str(v["updated_at"]),
            )
            for v in data.get("vinculos") or []
        ]

    async def listar_turmas(self, ano: int, escolas: list[int]) -> list[Turma]:
        """Turmas do ano nas escolas, incluindo as desativadas (deleted_at)."""
        data = await self._get(
            "Turma",
            "turmas-por-escola",
            instituicao_id=self._instituicao_id,
            ano=ano,
            escola=",".join(str(e) for e in escolas),
        )
        return [
            Turma(
                turma_id=int(t["id"]),
                nome=str(t["nome"]).strip(),
                ano=int(t["ano"]),
                escola_id=int(t["escola_id"]),
                series=tuple(
                    sorted({int(s["serie_id"]) for s in t.get("series_regras") or []})
                ),
                ativo=not t.get("deleted_at"),
                updated_at=str(t["updated_at"]),
            )
            for t in data.get("turmas") or []
        ]

    async def listar_componentes(
        self, escola_id: int, serie_id: int, ano: int
    ) -> list[ComponenteCurricular]:
        """Componentes curriculares que a escola oferece na série (Séries da escola)."""
        data = await self._get(
            "ComponenteCurricular",
            "componentes-curriculares-escola-serie-ano",
            escola_id=escola_id,
            serie_id=serie_id,
            ano=ano,
        )
        return [
            ComponenteCurricular(
                componente_id=int(c["id"]),
                nome=str(c["nome"]).strip(),
                abreviatura=str(c.get("abreviatura") or "").strip(),
            )
            for c in data.get("componentes_curriculares") or []
        ]
