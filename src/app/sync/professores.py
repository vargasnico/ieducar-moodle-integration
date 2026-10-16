"""Caso de integração 1: professores do i-Educar viram usuários do Moodle com papel de professor.

Fluxo:
  1. Consulta a API do i-Educar pelos servidores alterados desde a última execução.
  2. Verifica se o servidor já existe no Moodle (mapeamento local ou idnumber no Moodle).
  3. Se não existe: core_user_create_users + core_role_assign_roles.
  4. Se existe: atualiza nome/e-mail e suspende ou reativa conforme o status no i-Educar.
  5. Registra o mapeamento i-Educar ↔ Moodle.
"""

import logging
import re
from dataclasses import dataclass, field

import httpx

from app.clients.ieducar import IEducarClient, IEducarError, Servidor
from app.clients.moodle import MoodleClient, MoodleError
from app.config import MoodleSetup
from app.db import Repository

log = logging.getLogger(__name__)

CURSOR_KEY = "professores.cursor"


def idnumber_for(servidor_id: int) -> str:
    """Identificador estável do servidor no Moodle; não depende de CPF ou e-mail."""
    return f"ieducar-servidor-{servidor_id}"


def username_for(servidor: Servidor) -> str:
    cpf = re.sub(r"\D", "", servidor.cpf or "")
    return cpf if cpf else f"servidor{servidor.servidor_id}"


def split_name(nome: str) -> tuple[str, str]:
    parts = nome.split()
    if not parts:
        return "Servidor", "Sem nome"
    if len(parts) == 1:
        return parts[0], parts[0]
    return parts[0], " ".join(parts[1:])


@dataclass
class SyncResult:
    total: int = 0
    criados: int = 0
    atualizados: int = 0
    suspensos: int = 0
    inalterados: int = 0
    ignorados: int = 0
    erros: list[str] = field(default_factory=list)


class ProfessorSync:
    def __init__(
        self,
        ieducar: IEducarClient,
        moodle: MoodleClient,
        repo: Repository,
        setup: MoodleSetup,
        placeholder_email_domain: str,
    ) -> None:
        self._ieducar = ieducar
        self._moodle = moodle
        self._repo = repo
        self._setup = setup
        self._email_domain = placeholder_email_domain

    async def run(self, completo: bool = False) -> SyncResult:
        cursor = None if completo else self._repo.get_state(CURSOR_KEY)
        servidores = await self._ieducar.listar_servidores(modified=cursor)
        result = SyncResult(total=len(servidores))
        log.info("Sincronizando %d servidor(es) (cursor=%s)", len(servidores), cursor)

        # A API devolve os registros ordenados por updated_at. O cursor só avança até
        # antes do primeiro erro, para que o registro com falha volte na próxima execução.
        novo_cursor = cursor
        falhou = False
        for servidor in servidores:
            try:
                await self._sync_one(servidor, result)
            except (IEducarError, MoodleError, httpx.HTTPError) as exc:
                log.warning(
                    "Falha ao sincronizar servidor %s: %s", servidor.servidor_id, exc
                )
                result.erros.append(f"servidor {servidor.servidor_id}: {exc}")
                falhou = True
            if not falhou:
                novo_cursor = servidor.updated_at

        if novo_cursor and novo_cursor != cursor:
            self._repo.set_state(CURSOR_KEY, novo_cursor)
        return result

    async def _sync_one(self, servidor: Servidor, result: SyncResult) -> None:
        mapping = self._repo.get_professor(servidor.servidor_id)
        if mapping and mapping.ieducar_updated_at == servidor.updated_at:
            result.inalterados += 1
            return

        # "Já existe no Moodle?" — primeiro pelo mapeamento local; se ele foi perdido,
        # pelo idnumber gravado no usuário do Moodle, evitando criar duplicados.
        moodle_user_id = mapping.moodle_user_id if mapping else None
        username = mapping.moodle_username if mapping else None
        if moodle_user_id is None:
            existing = await self._moodle.get_user_by_field(
                "idnumber", idnumber_for(servidor.servidor_id)
            )
            if existing:
                moodle_user_id, username = (
                    int(existing["id"]),
                    str(existing["username"]),
                )

        if moodle_user_id is None:
            if not servidor.ativo:
                result.ignorados += 1
                return
            moodle_user_id, username = await self._create(servidor)
            result.criados += 1
        else:
            await self._update(servidor, moodle_user_id)
            if servidor.ativo:
                result.atualizados += 1
            else:
                result.suspensos += 1

        self._repo.save_professor(
            servidor.servidor_id,
            moodle_user_id,
            username or "",
            servidor.ativo,
            servidor.updated_at,
        )

    async def _email(self, servidor: Servidor) -> str:
        email = await self._ieducar.obter_email_servidor(servidor.servidor_id)
        return email or f"{idnumber_for(servidor.servidor_id)}@{self._email_domain}"

    async def _create(self, servidor: Servidor) -> tuple[int, str]:
        firstname, lastname = split_name(servidor.nome)
        username = username_for(servidor)
        user_id = await self._moodle.create_user(
            {
                "username": username,
                "auth": "manual",
                "createpassword": True,
                "firstname": firstname,
                "lastname": lastname,
                "email": await self._email(servidor),
                "idnumber": idnumber_for(servidor.servidor_id),
            }
        )
        await self._assign_teacher_role(user_id)
        log.info("Servidor %s criado no Moodle (id=%s)", servidor.servidor_id, user_id)
        return user_id, username

    async def _update(self, servidor: Servidor, user_id: int) -> None:
        firstname, lastname = split_name(servidor.nome)
        await self._moodle.update_user(
            {
                "id": user_id,
                "firstname": firstname,
                "lastname": lastname,
                "email": await self._email(servidor),
                "suspended": not servidor.ativo,
            }
        )
        if servidor.ativo:
            await self._assign_teacher_role(user_id)
        log.info(
            "Servidor %s atualizado no Moodle (id=%s)", servidor.servidor_id, user_id
        )

    async def _assign_teacher_role(self, user_id: int) -> None:
        # Atribuir de novo um papel já atribuído não tem efeito no Moodle.
        await self._moodle.assign_role(
            self._setup.teacher_role_id, user_id, self._setup.category_context_id
        )
