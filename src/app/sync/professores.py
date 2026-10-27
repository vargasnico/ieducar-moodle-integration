"""Caso de integração 1: professores do i-Educar viram usuários do Moodle.

Professor é o servidor ativo com pelo menos um vínculo ativo com turma
(modules.professor_turma) no ano letivo da escola. Servidores sem turma
(secretaria, direção etc.) não entram no Moodle.

Este caso cria e mantém apenas o usuário. O papel de professor é atribuído na
inscrição em cada curso (turma × componente curricular), no caso de turmas.

Fluxo de uma execução:
  1. Descobre a escola e o ano letivo em andamento (ou usa os configurados).
  2. Lê os vínculos alterados desde o último cursor e atualiza a cópia local.
  3. Lê os servidores alterados desde o último cursor (nome, e-mail, situação).
  4. Para cada servidor afetado, decide se é professor e:
       - não existe no Moodle e é professor  → core_user_create_users
       - existe e é professor                → atualiza nome/e-mail e reativa
       - existe e deixou de ser professor    → suspende
     Afetados são os de vínculo ou cadastro alterado, os que falharam antes e os
     ativos no Moodle sem turma no ano (troca de ano letivo, vínculo transferido).
  5. Registra o mapeamento i-Educar ↔ Moodle.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from app.clients.ieducar import IEducarClient, IEducarError, Servidor
from app.clients.moodle import MoodleClient, MoodleError
from app.db import ProfessorMapping, Repository

log = logging.getLogger(__name__)

SERVIDORES_CURSOR = "servidores.cursor"
PENDENTES_KEY = "professores.pendentes"
# Avisos de core_user_update_users para um usuário que não existe mais no Moodle.
USUARIO_REMOVIDO = {"invaliduserid", "usernotupdateddeleted"}


class ConflitoError(Exception):
    """O login do servidor já pertence a outro usuário do Moodle."""


def vinculos_cursor_key(ano: int) -> str:
    return f"vinculos.{ano}.cursor"


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


def _inalterado(mapping: ProfessorMapping, servidor: Servidor, professor: bool) -> bool:
    return (
        mapping.ieducar_updated_at == servidor.updated_at and mapping.ativo == professor
    )


@dataclass
class SyncResult:
    escolas: list[int] = field(default_factory=list)
    ano: int | None = None
    vinculos_lidos: int = 0
    avaliados: int = 0
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
        placeholder_email_domain: str,
        escola_id: int | None = None,
        ano_letivo: int | None = None,
        cursor_overlap: timedelta = timedelta(hours=24),
    ) -> None:
        self._ieducar = ieducar
        self._moodle = moodle
        self._repo = repo
        self._email_domain = placeholder_email_domain
        self._escola_id = escola_id
        self._ano_letivo = ano_letivo
        self._cursor_overlap = cursor_overlap

    def _since(self, cursor_key: str) -> str | None:
        """Valor de `modified` para a API: o cursor salvo menos a margem de sobreposição."""
        cursor = self._repo.get_state(cursor_key)
        if not cursor:
            return None
        since = datetime.fromisoformat(cursor) - self._cursor_overlap
        return since.strftime("%Y-%m-%d %H:%M:%S")

    async def _escolas(self) -> list[int]:
        if self._escola_id is not None:
            return [self._escola_id]
        return [e.escola_id for e in await self._ieducar.listar_escolas() if e.ativo]

    async def _ano(self, escolas: list[int]) -> int | None:
        if self._ano_letivo is not None:
            return self._ano_letivo
        return await self._ieducar.ano_letivo_em_andamento(escolas[0])

    async def run(self, completo: bool = False) -> SyncResult:
        result = SyncResult()
        result.escolas = await self._escolas()
        if not result.escolas:
            result.erros.append("Nenhuma escola ativa no i-Educar.")
            return result
        if len(result.escolas) > 1 and self._ano_letivo is None:
            log.warning(
                "Mais de uma escola ativa (%s); usando o ano letivo da primeira. "
                "Defina IEDUCAR_ANO_LETIVO para escolher.",
                result.escolas,
            )
        result.ano = ano = await self._ano(result.escolas)
        if ano is None:
            result.erros.append("Nenhum ano letivo em andamento na escola.")
            return result

        # Vínculos: atualizam a cópia local antes de qualquer decisão.
        vinculos_key = vinculos_cursor_key(ano)
        vinculos = await self._ieducar.listar_vinculos(
            ano,
            result.escolas,
            modified=None if completo else self._since(vinculos_key),
        )
        self._repo.save_vinculos(ano, vinculos)
        result.vinculos_lidos = len(vinculos)

        servidores = await self._ieducar.listar_servidores(
            modified=None if completo else self._since(SERVIDORES_CURSOR)
        )
        listados = {s.servidor_id: s for s in servidores}

        # Servidores afetados: os de vínculos alterados, os de cadastro alterado, os
        # que falharam na execução anterior e os ativos no Moodle sem turma no ano
        # (troca de ano letivo, vínculo transferido para outro servidor...).
        pendentes = set(json.loads(self._repo.get_state(PENDENTES_KEY) or "[]"))
        candidatos = (
            {v.servidor_id for v in vinculos}
            | set(listados)
            | pendentes
            | set(self._repo.list_professores_sem_turma(ano))
        )
        if completo:
            # Inclui quem já foi mapeado e não aparece mais na listagem do i-Educar.
            candidatos |= {m.servidor_id for m in self._repo.list_professores()}
        falhas: set[int] = set()
        for servidor_id in sorted(candidatos):
            result.avaliados += 1
            try:
                await self._sync_one(
                    servidor_id, ano, result, listados.get(servidor_id), completo
                )
            except (IEducarError, MoodleError, ConflitoError, httpx.HTTPError) as exc:
                log.warning("Falha ao sincronizar servidor %s: %s", servidor_id, exc)
                result.erros.append(f"servidor {servidor_id}: {exc}")
                falhas.add(servidor_id)
            except Exception as exc:
                # Um registro inesperado não interrompe os demais nem descarta os pendentes.
                log.exception("Erro inesperado ao sincronizar servidor %s", servidor_id)
                result.erros.append(f"servidor {servidor_id}: {exc!r}")
                falhas.add(servidor_id)

        # Os cursores sempre avançam; quem falhou fica em "pendentes" e é
        # reavaliado na próxima execução, mesmo sem nova alteração no i-Educar.
        if vinculos:
            self._repo.set_state(vinculos_key, max(v.updated_at for v in vinculos))
        if servidores:
            self._repo.set_state(
                SERVIDORES_CURSOR, max(s.updated_at for s in servidores)
            )
        self._repo.set_state(PENDENTES_KEY, json.dumps(sorted(falhas)))
        return result

    async def _sync_one(
        self,
        servidor_id: int,
        ano: int,
        result: SyncResult,
        listado: Servidor | None,
        forcar: bool,
    ) -> None:
        """`listado` é o servidor vindo da listagem, se veio; `forcar` reenvia ao Moodle
        mesmo sem alteração no i-Educar (corrige mudanças feitas direto no Moodle)."""
        mapping = self._repo.get_professor(servidor_id)
        tem_turma = self._repo.count_turmas_ativas(servidor_id, ano) > 0
        # A listagem já traz situação e updated_at: sem mudança, nem busca o servidor.
        if (
            not forcar
            and mapping
            and listado
            and _inalterado(mapping, listado, listado.ativo and tem_turma)
        ):
            result.inalterados += 1
            return

        servidor = await self._ieducar.obter_servidor(servidor_id)
        if servidor is None:
            await self._suspender_removido(mapping, result)
            return
        professor = servidor.ativo and tem_turma
        if not forcar and mapping and _inalterado(mapping, servidor, professor):
            result.inalterados += 1
            return

        existente = await self._atualizar_existente(servidor, mapping, professor)
        if existente:
            moodle_user_id, username = existente
            if professor:
                result.atualizados += 1
            else:
                result.suspensos += 1
        elif professor:
            moodle_user_id, username = await self._create(servidor)
            result.criados += 1
        else:
            if mapping:
                self._repo.delete_professor(servidor_id)
            result.ignorados += 1
            return

        self._repo.save_professor(
            servidor_id, moodle_user_id, username, professor, servidor.updated_at
        )

    async def _suspender_removido(
        self, mapping: ProfessorMapping | None, result: SyncResult
    ) -> None:
        """Servidor que não existe mais no i-Educar: suspende o usuário, se ainda ativo."""
        if not (mapping and mapping.ativo):
            result.ignorados += 1
            return
        try:
            await self._moodle.update_user(
                {"id": mapping.moodle_user_id, "suspended": True}
            )
        except MoodleError as exc:
            if exc.errorcode not in USUARIO_REMOVIDO:
                raise
            self._repo.delete_professor(mapping.servidor_id)
            result.ignorados += 1
            return
        self._repo.save_professor(
            mapping.servidor_id,
            mapping.moodle_user_id,
            mapping.moodle_username,
            False,
            mapping.ieducar_updated_at,
        )
        result.suspensos += 1
        log.info(
            "Servidor %s não existe mais no i-Educar; usuário %s suspenso",
            mapping.servidor_id,
            mapping.moodle_user_id,
        )

    async def _atualizar_existente(
        self, servidor: Servidor, mapping: ProfessorMapping | None, professor: bool
    ) -> tuple[int, str] | None:
        """Atualiza o usuário do servidor no Moodle, se ele existir; devolve (id, username)."""
        if mapping:
            try:
                await self._update(
                    servidor, mapping.moodle_user_id, suspenso=not professor
                )
                return mapping.moodle_user_id, mapping.moodle_username
            except MoodleError as exc:
                if exc.errorcode not in USUARIO_REMOVIDO:
                    raise
                log.warning(
                    "Usuário %s do Moodle (servidor %s) não existe mais; mapeamento descartado",
                    mapping.moodle_user_id,
                    servidor.servidor_id,
                )
        # Sem mapeamento válido, procura pelo idnumber gravado no usuário do Moodle,
        # evitando criar duplicados. Ao excluir um usuário o Moodle limpa o idnumber.
        existing = await self._moodle.get_user_by_field(
            "idnumber", idnumber_for(servidor.servidor_id)
        )
        if not existing:
            return None
        user_id = int(existing["id"])
        await self._update(servidor, user_id, suspenso=not professor)
        return user_id, str(existing["username"])

    async def _email(self, servidor: Servidor, user_id: int | None) -> str:
        """E-mail do i-Educar; um provisório se não houver ou se já for de outro usuário."""
        provisorio = f"{idnumber_for(servidor.servidor_id)}@{self._email_domain}"
        if not servidor.email:
            return provisorio
        # Sem allowaccountssameemail o Moodle recusa e-mail repetido, e no update o
        # usuário inteiro deixaria de ser atualizado.
        dono = await self._moodle.get_user_by_field("email", servidor.email)
        if dono and int(dono["id"]) != user_id:
            log.warning(
                "E-mail %s do servidor %s já pertence ao usuário %s do Moodle; usando %s",
                servidor.email,
                servidor.servidor_id,
                dono["id"],
                provisorio,
            )
            return provisorio
        return servidor.email

    async def _create(self, servidor: Servidor) -> tuple[int, str]:
        username = username_for(servidor)
        existing = await self._moodle.get_user_by_field("username", username)
        if existing:
            if existing.get("idnumber"):
                raise ConflitoError(
                    f"login {username} já pertence ao usuário {existing['id']} do Moodle"
                    f" (idnumber {existing['idnumber']})"
                )
            # Conta criada à mão com o login do servidor: passa a ser mantida pela integração.
            user_id = int(existing["id"])
            await self._update(servidor, user_id, suspenso=False)
            log.info(
                "Servidor %s vinculado ao usuário %s já existente no Moodle",
                servidor.servidor_id,
                user_id,
            )
            return user_id, username

        firstname, lastname = split_name(servidor.nome)
        user_id = await self._moodle.create_user(
            {
                "username": username,
                "auth": "manual",
                "createpassword": True,
                "firstname": firstname,
                "lastname": lastname,
                "email": await self._email(servidor, None),
                "idnumber": idnumber_for(servidor.servidor_id),
            }
        )
        log.info("Servidor %s criado no Moodle (id=%s)", servidor.servidor_id, user_id)
        return user_id, username

    async def _update(self, servidor: Servidor, user_id: int, suspenso: bool) -> None:
        firstname, lastname = split_name(servidor.nome)
        await self._moodle.update_user(
            {
                "id": user_id,
                "firstname": firstname,
                "lastname": lastname,
                "email": await self._email(servidor, user_id),
                "idnumber": idnumber_for(servidor.servidor_id),
                "suspended": suspenso,
            }
        )
        log.info(
            "Servidor %s atualizado no Moodle (id=%s, suspenso=%s)",
            servidor.servidor_id,
            user_id,
            suspenso,
        )
