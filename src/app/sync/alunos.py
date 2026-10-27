"""Caso de integração 3: alunos do i-Educar viram usuários e alunos dos cursos no Moodle.

Aluno no Moodle é o aluno ativo com enturmação ativa no ano letivo: matrícula ativa,
enturmação sem exclusão e sem data de saída. Transferência, remanejamento, abandono e
falecimento preenchem a data de saída e tiram o aluno da turma.

Cada aluno é inscrito como student nos cursos (turma × componente, criados no caso 2)
da sua turma, mas só nos componentes da sua série (numa turma multisseriada cada série
tem os seus) e sem os componentes de que tem dispensa ativa — qualquer dispensa,
mesmo que só de algumas etapas, tira o aluno do curso do componente.

Como no caso 2, cada execução recalcula o estado desejado a partir de todos os
alunos, matrículas, enturmações e dispensas do ano, e só envia ao Moodle o que difere
do que a integração já fez:
  1. Usuários: cria (login aluno<código>) quem passou a ser enturmado, atualiza nome
     e reativa quem mudou, suspende quem ficou sem turma no ano.
  2. Inscrições: inscreve e desinscreve nos cursos visíveis do ano. Cursos ocultos
     não são alterados, para preservar o histórico.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import batched
from typing import Any

import httpx

from app.clients.ieducar import Aluno, IEducarClient, IEducarError
from app.clients.moodle import MoodleClient, MoodleError
from app.config import MoodleNotConfiguredError, MoodleSetup
from app.db import AlunoMapping, Repository
from app.sync.contexto import resolver_contexto
from app.sync.lotes import enviar_em_lotes
from app.sync.professores import USUARIO_REMOVIDO, split_name
from app.sync.turmas import SERIE_SEM_COMPONENTES

log = logging.getLogger(__name__)

PAPEL_ALUNO = "student"
LOTE_USUARIOS = 50

Par = tuple[int, int]


def aluno_idnumber(aluno_id: int) -> str:
    """Identificador estável do aluno no Moodle: o código do aluno no i-Educar."""
    return f"ieducar-aluno-{aluno_id}"


def aluno_username(aluno_id: int) -> str:
    return f"aluno{aluno_id}"


def aluno_nome(aluno: Aluno) -> tuple[str, str]:
    """Nome social, quando houver; senão o nome civil."""
    nome = aluno.nome_social or aluno.nome
    if not nome.strip():
        return "Aluno", str(aluno.aluno_id)
    return split_name(nome)


@dataclass
class AlunoSyncResult:
    escolas: list[int] = field(default_factory=list)
    ano: int | None = None
    alunos_lidos: int = 0
    enturmacoes_ativas: int = 0
    usuarios_criados: int = 0
    usuarios_atualizados: int = 0
    usuarios_suspensos: int = 0
    usuarios_inalterados: int = 0
    inscricoes_criadas: int = 0
    inscricoes_removidas: int = 0
    # Pares aluno × componente fora dos cursos por dispensa ativa.
    dispensas_aplicadas: int = 0
    erros: list[str] = field(default_factory=list)


class AlunoSync:
    def __init__(
        self,
        ieducar: IEducarClient,
        moodle: MoodleClient,
        repo: Repository,
        setup: MoodleSetup,
        placeholder_email_domain: str,
        escola_id: int | None = None,
        ano_letivo: int | None = None,
    ) -> None:
        self._ieducar = ieducar
        self._moodle = moodle
        self._repo = repo
        self._setup = setup
        self._email_domain = placeholder_email_domain
        self._escola_id = escola_id
        self._ano_letivo = ano_letivo
        self._componentes: dict[Par, list[int]] = {}

    async def run(self, completo: bool = False) -> AlunoSyncResult:
        """`completo` reenvia ao Moodle todos os usuários e inscrições desejados, mesmo
        os que não mudaram (corrige alterações feitas direto no Moodle)."""
        role_id = self._setup.role_ids.get(PAPEL_ALUNO)
        if role_id is None:
            raise MoodleNotConfiguredError(
                f"Sem o id do papel {PAPEL_ALUNO} no arquivo do moodle-setup;"
                " rode o serviço moodle-setup de novo."
            )

        result = AlunoSyncResult()
        contexto = await resolver_contexto(
            self._ieducar, self._escola_id, self._ano_letivo
        )
        result.escolas, result.ano = contexto.escolas, contexto.ano
        if contexto.erro or contexto.ano is None:
            result.erros.append(contexto.erro or "Ano letivo não definido.")
            return result
        ano = contexto.ano

        alunos = {
            a.aluno_id: a
            for a in await self._ieducar.listar_alunos(ano, contexto.escolas)
        }
        matriculas = {
            m.matricula_id: m
            for m in await self._ieducar.listar_matriculas(ano, contexto.escolas)
        }
        enturmacoes = await self._ieducar.listar_enturmacoes(ano, contexto.escolas)
        dispensas = {
            (d.matricula_id, d.componente_id)
            for d in await self._ieducar.listar_dispensas(contexto.escolas)
            if d.ativa and d.matricula_id in matriculas
        }
        result.alunos_lidos = len(alunos)

        cursos = {(c.turma_id, c.componente_id): c for c in self._repo.list_cursos(ano)}
        cursos_por_aluno: dict[int, set[int]] = defaultdict(set)
        enturmados: set[int] = set()
        for enturmacao in enturmacoes:
            matricula = matriculas.get(enturmacao.matricula_id)
            aluno = alunos.get(matricula.aluno_id) if matricula else None
            if not (enturmacao.ativa and matricula and matricula.ativo and aluno):
                continue
            if not aluno.ativo:
                continue
            result.enturmacoes_ativas += 1
            enturmados.add(aluno.aluno_id)
            for componente_id in await self._componentes_da_serie(
                matricula.escola_id, enturmacao.serie_id, ano
            ):
                if (matricula.matricula_id, componente_id) in dispensas:
                    result.dispensas_aplicadas += 1
                    continue
                curso = cursos.get((enturmacao.turma_id, componente_id))
                if curso and curso.visible:
                    cursos_por_aluno[aluno.aluno_id].add(curso.moodle_course_id)

        usuarios = await self._sincronizar_usuarios(
            alunos, enturmados, completo, result
        )

        desejadas = {
            (course_id, usuarios[aluno_id])
            for aluno_id, course_ids in cursos_por_aluno.items()
            if aluno_id in usuarios
            for course_id in course_ids
        }
        visiveis = {c.moodle_course_id for c in cursos.values() if c.visible}
        await self._inscrever(desejadas, visiveis, role_id, completo, result)
        return result

    @staticmethod
    def _erro(result: AlunoSyncResult, onde: str, exc: Exception) -> None:
        log.warning("Falha em %s: %s", onde, exc)
        result.erros.append(f"{onde}: {exc}")

    async def _componentes_da_serie(
        self, escola_id: int, serie_id: int, ano: int
    ) -> list[int]:
        key = (escola_id, serie_id)
        if key not in self._componentes:
            try:
                componentes = await self._ieducar.listar_componentes(
                    escola_id, serie_id, ano
                )
            except IEducarError as exc:
                if SERIE_SEM_COMPONENTES not in str(exc):
                    raise
                componentes = []
            self._componentes[key] = [c.componente_id for c in componentes]
        return self._componentes[key]

    def _dados(self, aluno: Aluno, suspenso: bool) -> dict[str, Any]:
        firstname, lastname = aluno_nome(aluno)
        return {
            "firstname": firstname,
            "lastname": lastname,
            # A API do i-Educar não expõe o e-mail do aluno.
            "email": f"{aluno_idnumber(aluno.aluno_id)}@{self._email_domain}",
            "idnumber": aluno_idnumber(aluno.aluno_id),
            "suspended": suspenso,
        }

    async def _sincronizar_usuarios(
        self,
        alunos: dict[int, Aluno],
        enturmados: set[int],
        completo: bool,
        result: AlunoSyncResult,
    ) -> dict[int, int]:
        """Cria, atualiza e suspende usuários; devolve aluno_id → moodle_user_id dos
        alunos enturmados que têm usuário ativo."""
        ativos: dict[int, int] = {}
        a_criar: set[int] = set(enturmados)

        for mapping in self._repo.list_alunos():
            a_criar.discard(mapping.aluno_id)
            aluno = alunos.get(mapping.aluno_id)
            deve_ativo = mapping.aluno_id in enturmados
            try:
                if await self._atualizar(mapping, aluno, deve_ativo, completo, result):
                    if deve_ativo:
                        ativos[mapping.aluno_id] = mapping.moodle_user_id
                elif deve_ativo:
                    a_criar.add(mapping.aluno_id)
            except (MoodleError, httpx.HTTPError) as exc:
                self._erro(result, f"aluno {mapping.aluno_id}", exc)
                # O usuário segue existindo: sem ele em `ativos` o aluno seria
                # desinscrito de todos os cursos por causa de uma falha momentânea.
                if deve_ativo:
                    ativos[mapping.aluno_id] = mapping.moodle_user_id

        if a_criar:
            ativos.update(
                await self._criar([alunos[i] for i in sorted(a_criar)], result)
            )
        return ativos

    async def _atualizar(
        self,
        mapping: AlunoMapping,
        aluno: Aluno | None,
        deve_ativo: bool,
        completo: bool,
        result: AlunoSyncResult,
    ) -> bool:
        """Mantém o usuário de um aluno já mapeado; False se ele não existe mais no Moodle."""
        if aluno is None:
            # Sem matrícula no ano: só suspende, se ainda estiver ativo. Com
            # `completo` reenvia mesmo assim, para descobrir usuário excluído no Moodle.
            if not mapping.ativo and not completo:
                result.usuarios_inalterados += 1
                return True
            dados: dict[str, Any] = {"suspended": True}
            updated_at = mapping.ieducar_updated_at
        else:
            if (
                not completo
                and mapping.ativo == deve_ativo
                and mapping.ieducar_updated_at == aluno.updated_at
            ):
                result.usuarios_inalterados += 1
                return True
            dados = self._dados(aluno, suspenso=not deve_ativo)
            updated_at = aluno.updated_at

        try:
            await self._moodle.update_user({"id": mapping.moodle_user_id, **dados})
        except MoodleError as exc:
            if exc.errorcode not in USUARIO_REMOVIDO:
                raise
            log.warning(
                "Usuário %s do Moodle (aluno %s) não existe mais; mapeamento descartado",
                mapping.moodle_user_id,
                mapping.aluno_id,
            )
            self._repo.delete_aluno(mapping.aluno_id)
            return False

        self._repo.save_aluno(
            mapping.aluno_id,
            mapping.moodle_user_id,
            mapping.moodle_username,
            deve_ativo,
            updated_at,
        )
        if deve_ativo:
            result.usuarios_atualizados += 1
        else:
            result.usuarios_suspensos += 1
        return True

    async def _criar(
        self, alunos: list[Aluno], result: AlunoSyncResult
    ) -> dict[int, int]:
        """Cria os usuários que faltam, adotando antes os que já existem no Moodle
        (pelo idnumber ou, sem idnumber, pelo login). Devolve aluno_id → user_id."""
        criados: dict[int, int] = {}
        pendentes = {a.aluno_id: a for a in alunos}

        # Em lotes: valores demais numa chamada passam do max_input_vars do PHP e o
        # excedente é descartado sem erro.
        por_idnumber: dict[str, dict[str, Any]] = {}
        por_username: dict[str, dict[str, Any]] = {}
        for lote in batched(sorted(pendentes), LOTE_USUARIOS):
            for u in await self._moodle.get_users_by_field(
                "idnumber", [aluno_idnumber(i) for i in lote]
            ):
                por_idnumber[str(u.get("idnumber"))] = u
            for u in await self._moodle.get_users_by_field(
                "username", [aluno_username(i) for i in lote]
            ):
                por_username[str(u.get("username"))] = u
        for aluno_id, aluno in list(pendentes.items()):
            existente = por_idnumber.get(aluno_idnumber(aluno_id))
            if existente is None:
                existente = por_username.get(aluno_username(aluno_id))
                if existente and existente.get("idnumber"):
                    self._erro(
                        result,
                        f"aluno {aluno_id}",
                        MoodleError(
                            "core_user_create_users",
                            "usernametaken",
                            f"login {aluno_username(aluno_id)} já pertence ao usuário"
                            f" {existente['id']} (idnumber {existente['idnumber']})",
                        ),
                    )
                    del pendentes[aluno_id]
                    continue
            if existente is None:
                continue
            del pendentes[aluno_id]
            user_id = int(existente["id"])
            try:
                await self._moodle.update_user(
                    {"id": user_id, **self._dados(aluno, suspenso=False)}
                )
            except (MoodleError, httpx.HTTPError) as exc:
                self._erro(result, f"aluno {aluno_id}", exc)
                continue
            self._repo.save_aluno(
                aluno_id, user_id, str(existente["username"]), True, aluno.updated_at
            )
            criados[aluno_id] = user_id
            result.usuarios_atualizados += 1

        def novo(aluno: Aluno) -> dict[str, Any]:
            dados = self._dados(aluno, suspenso=False)
            del dados["suspended"]
            return {
                "username": aluno_username(aluno.aluno_id),
                "auth": "manual",
                "createpassword": True,
                **dados,
            }

        for lote in batched(
            sorted(pendentes.values(), key=lambda a: a.aluno_id), LOTE_USUARIOS
        ):
            try:
                ids = await self._moodle.create_users([novo(a) for a in lote])
            except (MoodleError, httpx.HTTPError) as exc:
                # A criação é uma transação: um usuário inválido desfaz o lote inteiro.
                log.warning(
                    "Lote de %s alunos recusado (%s); criando um a um", len(lote), exc
                )
                ids = {}
                for aluno in lote:
                    try:
                        ids.update(await self._moodle.create_users([novo(aluno)]))
                    except (MoodleError, httpx.HTTPError) as exc:
                        self._erro(result, f"aluno {aluno.aluno_id}", exc)
            for aluno in lote:
                user_id = ids.get(aluno_username(aluno.aluno_id))
                if user_id is None:
                    continue
                self._repo.save_aluno(
                    aluno.aluno_id,
                    user_id,
                    aluno_username(aluno.aluno_id),
                    True,
                    aluno.updated_at,
                )
                criados[aluno.aluno_id] = user_id
                result.usuarios_criados += 1
        if result.usuarios_criados:
            log.info("%s alunos criados no Moodle", result.usuarios_criados)
        return criados

    async def _inscrever(
        self,
        desejadas: set[Par],
        visiveis: set[int],
        role_id: int,
        completo: bool,
        result: AlunoSyncResult,
    ) -> None:
        # Só as inscrições dos cursos visíveis deste ano: as de cursos ocultos ficam.
        atuais = {
            (course_id, user_id)
            for course_id, user_id in self._repo.list_aluno_inscricoes()
            if course_id in visiveis
        }

        novas = sorted(desejadas if completo else desejadas - atuais)
        feitas = await enviar_em_lotes(
            novas,
            lambda lote: self._moodle.enrol_users(role_id, lote),
            lambda par, exc: self._erro(
                result, f"inscrever usuário {par[1]} no curso {par[0]}", exc
            ),
        )
        self._repo.save_aluno_inscricoes(feitas)
        result.inscricoes_criadas += len(set(feitas) - atuais)

        removidas = await enviar_em_lotes(
            sorted(atuais - desejadas),
            self._moodle.unenrol_users,
            lambda par, exc: self._erro(
                result, f"desinscrever usuário {par[1]} do curso {par[0]}", exc
            ),
        )
        self._repo.delete_aluno_inscricoes(removidas)
        result.inscricoes_removidas += len(removidas)
