"""Caso de integração 1: professores do i-Educar viram usuários do Moodle.

Professor é o servidor ativo com pelo menos um vínculo ativo com turma
(modules.professor_turma) no ano letivo da escola. Servidores sem turma
(secretaria, direção etc.) não entram no Moodle.

Este caso cria e mantém apenas o usuário. O papel de professor é atribuído na
inscrição em cada curso (turma × componente curricular), no caso de turmas.

Fluxo de uma execução:
  1. Descobre a escola e o ano letivo em andamento (ou usa os configurados).
  2. Lê todos os vínculos do ano e todos os servidores da instituição.
  3. Para cada servidor com turma ou já mapeado, decide se é professor e:
       - não existe no Moodle e é professor  → core_user_create_users
       - existe e é professor                → atualiza nome/e-mail e reativa
       - existe e deixou de ser professor    → suspende
     Só envia ao Moodle quem mudou desde a última execução.
  4. Registra o mapeamento i-Educar ↔ Moodle.
"""

import logging
import re
from dataclasses import dataclass, field

import httpx

from app.clients.ieducar import IEducarClient, IEducarError, Servidor
from app.clients.moodle import MoodleClient, MoodleError
from app.db import ProfessorMapping, Repository
from app.sync.contexto import resolver_contexto

log = logging.getLogger(__name__)

# Avisos de core_user_update_users para um usuário que não existe mais no Moodle.
USUARIO_REMOVIDO = {"invaliduserid", "usernotupdateddeleted"}


class ConflitoError(Exception):
    """O login do servidor já pertence a outro usuário do Moodle."""


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
    ) -> None:
        self._ieducar = ieducar
        self._moodle = moodle
        self._repo = repo
        self._email_domain = placeholder_email_domain
        self._escola_id = escola_id
        self._ano_letivo = ano_letivo

    async def run(self, completo: bool = False) -> SyncResult:
        result = SyncResult()
        contexto = await resolver_contexto(
            self._ieducar, self._escola_id, self._ano_letivo
        )
        result.escolas, result.ano = contexto.escolas, contexto.ano
        if contexto.erro or contexto.ano is None:
            result.erros.append(contexto.erro or "Ano letivo não definido.")
            return result
        ano = contexto.ano

        vinculos = await self._ieducar.listar_vinculos(ano, result.escolas)
        result.vinculos_lidos = len(vinculos)
        com_turma = {v.servidor_id for v in vinculos if v.ativo}
        servidores = {s.servidor_id: s for s in await self._ieducar.listar_servidores()}

        # Quem tem turma e quem já tem usuário no Moodle (pode ter de ser suspenso).
        candidatos = com_turma | {m.servidor_id for m in self._repo.list_professores()}
        for servidor_id in sorted(candidatos):
            result.avaliados += 1
            try:
                await self._sync_one(
                    servidor_id,
                    servidor_id in com_turma,
                    result,
                    servidores.get(servidor_id),
                    completo,
                )
            except (IEducarError, MoodleError, ConflitoError, httpx.HTTPError) as exc:
                log.warning("Falha ao sincronizar servidor %s: %s", servidor_id, exc)
                result.erros.append(f"servidor {servidor_id}: {exc}")
            except Exception as exc:
                # Um registro inesperado não interrompe os demais.
                log.exception("Erro inesperado ao sincronizar servidor %s", servidor_id)
                result.erros.append(f"servidor {servidor_id}: {exc!r}")
        return result

    async def _sync_one(
        self,
        servidor_id: int,
        tem_turma: bool,
        result: SyncResult,
        listado: Servidor | None,
        forcar: bool,
    ) -> None:
        """`listado` é o servidor vindo da listagem (None se não existe mais); `forcar`
        reenvia ao Moodle mesmo sem alteração no i-Educar (corrige mudanças feitas direto
        no Moodle)."""
        mapping = self._repo.get_professor(servidor_id)
        # A listagem já traz situação e updated_at: sem mudança, nem busca o servidor.
        if (
            not forcar
            and mapping
            and listado
            and _inalterado(mapping, listado, listado.ativo and tem_turma)
        ):
            result.inalterados += 1
            return

        # A listagem não traz o e-mail: só busca o servidor quando vai enviar ao Moodle.
        servidor = await self._ieducar.obter_servidor(servidor_id) if listado else None
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
