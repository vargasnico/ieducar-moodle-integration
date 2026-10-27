"""Usuários do Moodle mantidos pela integração (professores e alunos).

Cada pessoa do i-Educar tem um usuário identificado pelo idnumber. Quem deve estar
ativo é criado ou atualizado; quem deixou de estar é suspenso, nunca excluído. Só
vai ao Moodle quem mudou desde a última execução.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from itertools import batched
from typing import Any

import httpx

from app.db import UsuarioMapping
from app.interfaces import IEducarError, MoodleError
from app.sync.base import Sincronizacao
from app.sync.idempotencia import LOTE, ConflitoError, enviar_em_lotes, localizar

log = logging.getLogger(__name__)

PROFESSOR = "professor"
ALUNO = "aluno"
# Avisos de core_user_update_users para um usuário que não existe mais no Moodle.
USUARIO_REMOVIDO = {"invaliduserid", "usernotupdateddeleted"}


@dataclass(frozen=True)
class Pessoa:
    """Servidor ou aluno do i-Educar como usuário do Moodle."""

    origem_id: int
    idnumber: str
    username: str
    nome: str
    email: str
    updated_at: str

    def campos(self) -> dict[str, Any]:
        partes = self.nome.split() or [self.username]
        return {
            "firstname": partes[0],
            "lastname": " ".join(partes[1:]) or partes[0],
            "email": self.email,
            "idnumber": self.idnumber,
        }

    def novo(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "auth": "manual",
            "createpassword": True,
            **self.campos(),
        }


async def sincronizar_usuarios(
    sync: Sincronizacao,
    tipo: str,
    presentes: dict[int, str],
    ativos: set[int],
    carregar: Callable[[int], Awaitable[Pessoa | None]],
) -> dict[int, int]:
    """Cria, atualiza e suspende os usuários de um tipo; devolve origem_id →
    moodle_user_id de quem está ativo.

    `presentes` traz o updated_at de cada registro que existe no i-Educar, `ativos`
    quem deve ter usuário ativo, e `carregar` monta a Pessoa de um registro (só é
    chamado para quem vai ao Moodle)."""
    mapeados = {m.origem_id: m for m in sync.repo.list_usuarios(tipo)}
    usuarios: dict[int, int] = {}
    novos: list[Pessoa] = []
    for origem_id in sorted(ativos | mapeados.keys()):
        mapping = mapeados.get(origem_id)
        ativo = origem_id in ativos
        try:
            if mapping and await _manter(
                sync, mapping, presentes.get(origem_id), ativo, carregar
            ):
                if ativo:
                    usuarios[origem_id] = mapping.moodle_user_id
            elif ativo and (pessoa := await carregar(origem_id)):
                novos.append(pessoa)
        except (IEducarError, MoodleError, httpx.HTTPError) as exc:
            sync.resultado.erro(f"{tipo} {origem_id}", exc)
            # O usuário segue existindo: uma falha momentânea não o tira dos cursos.
            if mapping and ativo:
                usuarios[origem_id] = mapping.moodle_user_id
    usuarios.update(await _criar(sync, tipo, novos))
    return usuarios


async def _manter(
    sync: Sincronizacao,
    mapping: UsuarioMapping,
    updated_at: str | None,
    ativo: bool,
    carregar: Callable[[int], Awaitable[Pessoa | None]],
) -> bool:
    """Atualiza o usuário mapeado, se mudou; False se ele não existe mais no Moodle.
    `updated_at` é None quando o registro saiu do i-Educar: aí só suspende."""
    if (
        not sync.completo
        and mapping.ativo == ativo
        and mapping.ieducar_updated_at == (updated_at or mapping.ieducar_updated_at)
    ):
        sync.resultado.conta("usuarios_inalterados")
        return True
    pessoa = await carregar(mapping.origem_id) if updated_at else None
    campos = pessoa.campos() if pessoa else {}
    try:
        await sync.moodle.update_user(
            {"id": mapping.moodle_user_id, **campos, "suspended": not ativo}
        )
    except MoodleError as exc:
        if exc.errorcode not in USUARIO_REMOVIDO:
            raise
        log.warning(
            "Usuário %s do Moodle (%s %s) não existe mais; mapeamento descartado",
            mapping.moodle_user_id,
            mapping.tipo,
            mapping.origem_id,
        )
        sync.repo.delete_usuario(mapping.tipo, mapping.origem_id)
        return False
    sync.repo.save_usuario(
        mapping.tipo,
        mapping.origem_id,
        mapping.moodle_user_id,
        mapping.moodle_username,
        ativo,
        pessoa.updated_at if pessoa else mapping.ieducar_updated_at,
    )
    sync.resultado.conta("usuarios_atualizados" if ativo else "usuarios_suspensos")
    return True


async def _criar(
    sync: Sincronizacao, tipo: str, pessoas: list[Pessoa]
) -> dict[int, int]:
    """Adota os usuários que já existem no Moodle e cria os demais em lotes."""
    por_idnumber: dict[str, Any] = {}
    por_username: dict[str, Any] = {}
    for lote in batched(pessoas, LOTE):
        for u in await sync.moodle.get_users_by_field(
            "idnumber", [p.idnumber for p in lote]
        ):
            por_idnumber[u["idnumber"]] = u
        for u in await sync.moodle.get_users_by_field(
            "username", [p.username for p in lote]
        ):
            por_username[u["username"]] = u

    usuarios: dict[int, int] = {}

    def salvar(pessoa: Pessoa, user_id: int, username: str, evento: str) -> None:
        sync.repo.save_usuario(
            tipo, pessoa.origem_id, user_id, username, True, pessoa.updated_at
        )
        sync.resultado.conta(evento)
        usuarios[pessoa.origem_id] = user_id

    novos: list[Pessoa] = []
    for pessoa in pessoas:
        try:
            existente = localizar(
                por_idnumber, por_username, pessoa.idnumber, pessoa.username
            )
            if existente is None:
                novos.append(pessoa)
                continue
            user_id = int(existente["id"])
            await sync.moodle.update_user(
                {"id": user_id, **pessoa.campos(), "suspended": False}
            )
        except (ConflitoError, MoodleError, httpx.HTTPError) as exc:
            sync.resultado.erro(f"{tipo} {pessoa.origem_id}", exc)
            continue
        salvar(pessoa, user_id, str(existente["username"]), "usuarios_atualizados")

    criados, ids = await enviar_em_lotes(
        novos,
        lambda lote: sync.moodle.create_users([p.novo() for p in lote]),
        lambda pessoa, exc: sync.resultado.erro(f"{tipo} {pessoa.origem_id}", exc),
    )
    for pessoa in criados:
        salvar(pessoa, ids[pessoa.username], pessoa.username, "usuarios_criados")
    return usuarios
