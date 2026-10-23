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
from app.db import Repository

log = logging.getLogger(__name__)

SERVIDORES_CURSOR = "servidores.cursor"
PENDENTES_KEY = "professores.pendentes"


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
        for v in vinculos:
            self._repo.save_vinculo(
                v.vinculo_id,
                v.servidor_id,
                v.turma_id,
                ano,
                v.componentes,
                v.ativo,
                v.updated_at,
            )
        result.vinculos_lidos = len(vinculos)

        servidores = await self._ieducar.listar_servidores(
            modified=None if completo else self._since(SERVIDORES_CURSOR)
        )

        # Servidores afetados: os de vínculos alterados, os de cadastro alterado e
        # os que falharam na execução anterior.
        pendentes = set(json.loads(self._repo.get_state(PENDENTES_KEY) or "[]"))
        candidatos = (
            {v.servidor_id for v in vinculos}
            | {s.servidor_id for s in servidores}
            | pendentes
        )
        falhas: set[int] = set()
        for servidor_id in sorted(candidatos):
            result.avaliados += 1
            try:
                await self._sync_one(servidor_id, ano, result)
            except (IEducarError, MoodleError, httpx.HTTPError) as exc:
                log.warning("Falha ao sincronizar servidor %s: %s", servidor_id, exc)
                result.erros.append(f"servidor {servidor_id}: {exc}")
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

    async def _sync_one(self, servidor_id: int, ano: int, result: SyncResult) -> None:
        servidor = await self._ieducar.obter_servidor(servidor_id)
        if servidor is None:
            result.ignorados += 1
            return
        professor = (
            servidor.ativo and self._repo.count_turmas_ativas(servidor_id, ano) > 0
        )

        mapping = self._repo.get_professor(servidor_id)
        if (
            mapping
            and mapping.ieducar_updated_at == servidor.updated_at
            and mapping.ativo == professor
        ):
            result.inalterados += 1
            return

        # "Já existe no Moodle?" — primeiro pelo mapeamento local; se ele foi perdido,
        # pelo idnumber gravado no usuário do Moodle, evitando criar duplicados.
        moodle_user_id = mapping.moodle_user_id if mapping else None
        username = mapping.moodle_username if mapping else None
        if moodle_user_id is None:
            existing = await self._moodle.get_user_by_field(
                "idnumber", idnumber_for(servidor_id)
            )
            if existing:
                moodle_user_id = int(existing["id"])
                username = str(existing["username"])

        if moodle_user_id is None:
            if not professor:
                result.ignorados += 1
                return
            moodle_user_id, username = await self._create(servidor)
            result.criados += 1
        else:
            await self._update(servidor, moodle_user_id, suspenso=not professor)
            if professor:
                result.atualizados += 1
            else:
                result.suspensos += 1

        self._repo.save_professor(
            servidor_id, moodle_user_id, username or "", professor, servidor.updated_at
        )

    def _email(self, servidor: Servidor) -> str:
        return (
            servidor.email
            or f"{idnumber_for(servidor.servidor_id)}@{self._email_domain}"
        )

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
                "email": self._email(servidor),
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
                "email": self._email(servidor),
                "suspended": suspenso,
            }
        )
        log.info(
            "Servidor %s atualizado no Moodle (id=%s, suspenso=%s)",
            servidor.servidor_id,
            user_id,
            suspenso,
        )
