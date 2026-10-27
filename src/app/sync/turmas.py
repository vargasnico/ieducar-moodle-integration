"""Caso de integração 2: turmas × componentes curriculares viram cursos no Moodle.

No i-Educar uma turma reúne alunos, professores e vários componentes curriculares.
No Moodle, cada par turma × componente vira um curso, com o professor do componente
inscrito como editingteacher. O curso é criado mesmo sem professor vinculado.

Cada execução recalcula o estado desejado a partir de todas as turmas do ano (são
poucas chamadas ao i-Educar: uma de turmas e uma por série) e só envia ao Moodle o
que difere do que a integração já criou:
  1. Garante a categoria do ano letivo (iEducar › <ano>).
  2. Turma ativa: um curso por componente que a escola oferece na série da turma
     (Séries da escola), criado ou com o nome atualizado e visível.
  3. Turma desativada, componente que saiu da série ou curso de outro ano letivo: o
     curso é ocultado. As inscrições de cursos ocultos não são alteradas, para
     preservar o histórico.
  4. Professores: inscritos nos cursos dos componentes dos seus vínculos ativos e
     desinscritos quando o vínculo sai. Os vínculos e os usuários vêm do caso 1.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from itertools import batched
from typing import Any

import httpx

from app.clients.ieducar import ComponenteCurricular, IEducarClient, IEducarError, Turma
from app.clients.moodle import MoodleClient, MoodleError
from app.config import MoodleNotConfiguredError, MoodleSetup
from app.db import CursoMapping, Repository
from app.sync.contexto import resolver_contexto

log = logging.getLogger(__name__)

PAPEL_PROFESSOR = "editingteacher"
# Erro do Moodle ao atualizar um curso que foi excluído.
CURSO_REMOVIDO = {"invalidrecord", "invalidcourseid"}
# Trecho da resposta do i-Educar para uma série sem componentes na escola.
SERIE_SEM_COMPONENTES = "não está vinculado ao ano letivo"
# Itens por chamada ao Moodle, longe do max_input_vars padrão do PHP (1000).
LOTE_CURSOS = 50
LOTE_INSCRICOES = 100

Par = tuple[int, int]


def categoria_idnumber(ano: int) -> str:
    return f"ieducar-{ano}"


def curso_idnumber(turma_id: int, componente_id: int) -> str:
    """Identificador estável do curso no Moodle: o par turma × componente."""
    return f"ieducar-turma-{turma_id}-componente-{componente_id}"


def curso_shortname(ano: int, turma_id: int, componente_id: int) -> str:
    return f"{ano}-T{turma_id}-C{componente_id}"


def curso_fullname(turma: Turma, componente: ComponenteCurricular) -> str:
    return f"{turma.nome} – {componente.nome} ({turma.ano})"


def curso_do_par(
    cursos: list[dict[str, Any]], idnumber: str, shortname: str
) -> dict[str, Any] | None:
    """Curso com o idnumber do par ou, sem idnumber, com o nome curto dele (idnumber
    apagado à mão, por exemplo). Nome curto usado por outro curso é conflito."""
    for curso in cursos:
        if curso.get("idnumber") == idnumber:
            return curso
    for curso in cursos:
        if curso.get("shortname") == shortname:
            if curso.get("idnumber"):
                raise MoodleError(
                    "core_course_create_courses",
                    "shortnametaken",
                    f"nome curto {shortname} já usado pelo curso {curso['id']}"
                    f" (idnumber {curso['idnumber']})",
                )
            return curso
    return None


@dataclass
class TurmaSyncResult:
    escolas: list[int] = field(default_factory=list)
    ano: int | None = None
    turmas_lidas: int = 0
    cursos_criados: int = 0
    cursos_atualizados: int = 0
    cursos_ocultados: int = 0
    cursos_inalterados: int = 0
    inscricoes_criadas: int = 0
    inscricoes_removidas: int = 0
    # Pares turma × componente de vínculos ativos sem curso no Moodle (componente
    # diferenciado da turma, ou que a escola não oferece na série).
    componentes_sem_curso: int = 0
    erros: list[str] = field(default_factory=list)


class TurmaSync:
    def __init__(
        self,
        ieducar: IEducarClient,
        moodle: MoodleClient,
        repo: Repository,
        setup: MoodleSetup,
        escola_id: int | None = None,
        ano_letivo: int | None = None,
    ) -> None:
        self._ieducar = ieducar
        self._moodle = moodle
        self._repo = repo
        self._setup = setup
        self._escola_id = escola_id
        self._ano_letivo = ano_letivo
        self._componentes: dict[tuple[int, int], list[ComponenteCurricular]] = {}
        self._na_categoria: list[dict[str, Any]] | None = None

    async def run(self, completo: bool = False) -> TurmaSyncResult:
        """`completo` reenvia ao Moodle todos os cursos e inscrições desejados, mesmo
        os que não mudaram (corrige alterações feitas direto no Moodle)."""
        # Conferido antes de qualquer alteração no Moodle.
        role_id = self._setup.role_ids.get(PAPEL_PROFESSOR)
        if role_id is None:
            raise MoodleNotConfiguredError(
                f"Sem o id do papel {PAPEL_PROFESSOR} no arquivo do moodle-setup;"
                " rode o serviço moodle-setup de novo."
            )

        result = TurmaSyncResult()
        contexto = await resolver_contexto(
            self._ieducar, self._escola_id, self._ano_letivo
        )
        result.escolas, result.ano = contexto.escolas, contexto.ano
        if contexto.erro or contexto.ano is None:
            result.erros.append(contexto.erro or "Ano letivo não definido.")
            return result
        ano = contexto.ano

        categoria_id = await self._categoria(ano)
        turmas = await self._ieducar.listar_turmas(ano, contexto.escolas)
        result.turmas_lidas = len(turmas)

        desejados: dict[Par, str] = {}
        for turma in turmas:
            if not turma.ativo:
                continue
            for componente in await self._componentes_da_turma(turma):
                key = (turma.turma_id, componente.componente_id)
                desejados[key] = curso_fullname(turma, componente)

        # Cursos de todos os anos: na troca de ano letivo os do ano anterior são ocultados.
        existentes = {
            (c.turma_id, c.componente_id): c for c in self._repo.list_cursos()
        }
        a_criar: list[tuple[Par, str]] = []
        for key, fullname in sorted(desejados.items()):
            try:
                if not await self._garantir_curso(
                    key,
                    fullname,
                    ano,
                    categoria_id,
                    existentes.get(key),
                    completo,
                    result,
                ):
                    a_criar.append((key, fullname))
            except (MoodleError, httpx.HTTPError) as exc:
                self._erro(result, f"curso turma {key[0]} × componente {key[1]}", exc)
        await self._criar_cursos(a_criar, ano, categoria_id, result)

        for key, curso in sorted(existentes.items()):
            if key in desejados or not curso.visible:
                continue
            try:
                await self._ocultar(curso, result)
            except (MoodleError, httpx.HTTPError) as exc:
                self._erro(result, f"ocultar curso {curso.moodle_course_id}", exc)

        await self._inscrever_professores(ano, role_id, completo, result)
        return result

    @staticmethod
    def _erro(result: TurmaSyncResult, onde: str, exc: Exception) -> None:
        log.warning("Falha em %s: %s", onde, exc)
        result.erros.append(f"{onde}: {exc}")

    async def _categoria(self, ano: int) -> int:
        idnumber = categoria_idnumber(ano)
        existente = await self._moodle.get_category_by_idnumber(idnumber)
        if existente:
            return int(existente["id"])
        categoria_id = await self._moodle.create_category(
            str(ano),
            self._setup.category_id,
            idnumber,
            f"Turmas do ano letivo {ano} sincronizadas a partir do i-Educar.",
        )
        log.info("Categoria do ano %s criada no Moodle (id=%s)", ano, categoria_id)
        return categoria_id

    async def _componentes_da_turma(self, turma: Turma) -> list[ComponenteCurricular]:
        """Componentes de todas as séries da turma (várias, se multisseriada)."""
        por_id: dict[int, ComponenteCurricular] = {}
        for serie_id in turma.series:
            key = (turma.escola_id, serie_id)
            if key not in self._componentes:
                try:
                    self._componentes[key] = await self._ieducar.listar_componentes(
                        turma.escola_id, serie_id, turma.ano
                    )
                except IEducarError as exc:
                    # Outros erros interrompem a execução: tratá-los como "sem
                    # componentes" ocultaria todos os cursos da série.
                    if SERIE_SEM_COMPONENTES not in str(exc):
                        raise
                    log.warning(
                        "Sem componentes para escola %s, série %s: %s",
                        turma.escola_id,
                        serie_id,
                        exc,
                    )
                    self._componentes[key] = []
            for componente in self._componentes[key]:
                por_id.setdefault(componente.componente_id, componente)
        return list(por_id.values())

    async def _garantir_curso(
        self,
        key: Par,
        fullname: str,
        ano: int,
        categoria_id: int,
        mapping: CursoMapping | None,
        completo: bool,
        result: TurmaSyncResult,
    ) -> bool:
        """Atualiza o curso do par se ele já existe no Moodle; False se precisa ser criado."""
        if (
            mapping
            and not completo
            and mapping.fullname == fullname
            and mapping.visible
        ):
            result.cursos_inalterados += 1
            return True

        if mapping:
            try:
                await self._moodle.update_course(
                    {
                        "id": mapping.moodle_course_id,
                        "fullname": fullname,
                        "visible": True,
                    }
                )
            except MoodleError as exc:
                if exc.errorcode not in CURSO_REMOVIDO:
                    raise
                log.warning(
                    "Curso %s não existe mais no Moodle; recriando",
                    mapping.moodle_course_id,
                )
                self._repo.delete_inscricoes_do_curso(mapping.moodle_course_id)
            else:
                result.cursos_atualizados += 1
                self._repo.save_curso(
                    *key, ano, mapping.moodle_course_id, fullname, True
                )
                return True

        # Sem mapeamento válido, procura o curso na categoria do ano antes de criar.
        if self._na_categoria is None:
            self._na_categoria = await self._moodle.get_courses_by_field(
                "category", str(categoria_id)
            )
        existente = curso_do_par(
            self._na_categoria, curso_idnumber(*key), curso_shortname(ano, *key)
        )
        if not existente:
            return False
        await self._adotar(existente, key, fullname, ano, result)
        return True

    async def _adotar(
        self,
        curso: dict[str, Any],
        key: Par,
        fullname: str,
        ano: int,
        result: TurmaSyncResult,
    ) -> None:
        """Passa a manter um curso que já existe no Moodle (grava o idnumber do par)."""
        course_id = int(curso["id"])
        await self._moodle.update_course(
            {
                "id": course_id,
                "fullname": fullname,
                "idnumber": curso_idnumber(*key),
                "visible": True,
            }
        )
        result.cursos_atualizados += 1
        self._repo.save_curso(*key, ano, course_id, fullname, True)

    async def _criar_cursos(
        self,
        pendentes: list[tuple[Par, str]],
        ano: int,
        categoria_id: int,
        result: TurmaSyncResult,
    ) -> None:
        def dados(key: Par, fullname: str) -> dict[str, Any]:
            return {
                "fullname": fullname,
                "shortname": curso_shortname(ano, *key),
                "categoryid": categoria_id,
                "idnumber": curso_idnumber(*key),
                "visible": True,
            }

        for lote in batched(pendentes, LOTE_CURSOS):
            try:
                ids = await self._moodle.create_courses([dados(*p) for p in lote])
            except (MoodleError, httpx.HTTPError) as exc:
                # A criação é uma transação: um curso inválido desfaz o lote inteiro.
                log.warning(
                    "Lote de %s cursos recusado (%s); criando um a um", len(lote), exc
                )
                for key, fullname in lote:
                    try:
                        await self._criar_um(
                            dados(key, fullname), key, fullname, ano, result
                        )
                    except (MoodleError, httpx.HTTPError) as exc:
                        self._erro(
                            result, f"curso turma {key[0]} × componente {key[1]}", exc
                        )
                continue
            for key, fullname in lote:
                self._salvar_criado(
                    key, fullname, ano, ids[curso_shortname(ano, *key)], result
                )

    async def _criar_um(
        self,
        curso: dict[str, Any],
        key: Par,
        fullname: str,
        ano: int,
        result: TurmaSyncResult,
    ) -> None:
        # O curso pode ter sido movido para fora da categoria do ano: procura no site.
        no_site = await self._moodle.get_courses_by_field("idnumber", curso["idnumber"])
        no_site += await self._moodle.get_courses_by_field(
            "shortname", curso["shortname"]
        )
        existente = curso_do_par(no_site, curso["idnumber"], curso["shortname"])
        if existente:
            await self._adotar(existente, key, fullname, ano, result)
            return
        ids = await self._moodle.create_courses([curso])
        self._salvar_criado(key, fullname, ano, ids[curso["shortname"]], result)

    def _salvar_criado(
        self, key: Par, fullname: str, ano: int, course_id: int, result: TurmaSyncResult
    ) -> None:
        self._repo.save_curso(*key, ano, course_id, fullname, True)
        result.cursos_criados += 1
        log.info("Curso %s criado no Moodle (id=%s)", fullname, course_id)

    async def _ocultar(self, curso: CursoMapping, result: TurmaSyncResult) -> None:
        try:
            await self._moodle.update_course(
                {"id": curso.moodle_course_id, "visible": False}
            )
        except MoodleError as exc:
            if exc.errorcode not in CURSO_REMOVIDO:
                raise
            self._repo.delete_curso(curso.turma_id, curso.componente_id)
            self._repo.delete_inscricoes_do_curso(curso.moodle_course_id)
            return
        self._repo.save_curso(
            curso.turma_id,
            curso.componente_id,
            curso.ano,
            curso.moodle_course_id,
            curso.fullname,
            False,
        )
        result.cursos_ocultados += 1
        log.info("Curso %s ocultado no Moodle", curso.moodle_course_id)

    async def _inscrever_professores(
        self, ano: int, role_id: int, completo: bool, result: TurmaSyncResult
    ) -> None:
        cursos = {(c.turma_id, c.componente_id): c for c in self._repo.list_cursos(ano)}
        visiveis = {c.moodle_course_id for c in cursos.values() if c.visible}
        usuarios = {
            p.servidor_id: p.moodle_user_id
            for p in self._repo.list_professores()
            if p.ativo
        }

        desejadas: set[Par] = set()
        sem_curso: set[Par] = set()
        for servidor_id, turma_id, componentes in self._repo.list_vinculos_ativos(ano):
            user_id = usuarios.get(servidor_id)
            if user_id is None:
                continue
            for componente_id in componentes:
                curso = cursos.get((turma_id, componente_id))
                if curso is None:
                    sem_curso.add((turma_id, componente_id))
                elif curso.visible:
                    desejadas.add((curso.moodle_course_id, user_id))
        if sem_curso:
            result.componentes_sem_curso = len(sem_curso)
            log.warning(
                "Vínculos com componentes sem curso no Moodle (turma, componente): %s",
                sorted(sem_curso),
            )

        # Só as inscrições dos cursos visíveis deste ano: as de cursos ocultos ficam.
        atuais = {
            (course_id, user_id)
            for course_id, user_id in self._repo.list_professor_inscricoes()
            if course_id in visiveis
        }

        novas = sorted(desejadas if completo else desejadas - atuais)
        feitas = await self._em_lotes(
            novas,
            lambda lote: self._moodle.enrol_users(role_id, lote),
            "inscrever usuário {1} no curso {0}",
            result,
        )
        self._repo.save_professor_inscricoes(feitas)
        result.inscricoes_criadas += len(set(feitas) - atuais)

        removidas = await self._em_lotes(
            sorted(atuais - desejadas),
            self._moodle.unenrol_users,
            "desinscrever usuário {1} do curso {0}",
            result,
        )
        self._repo.delete_professor_inscricoes(removidas)
        result.inscricoes_removidas += len(removidas)

    async def _em_lotes(
        self,
        pares: list[Par],
        enviar: Callable[[list[Par]], Awaitable[None]],
        descricao: str,
        result: TurmaSyncResult,
    ) -> list[Par]:
        """Envia pares (curso, usuário) em lotes e devolve os aceitos. O Moodle processa
        cada lote numa transação; se um par inválido derruba o lote, reenvia um a um."""
        aceitos: list[Par] = []
        for lote in batched(pares, LOTE_INSCRICOES):
            try:
                await enviar(list(lote))
            except MoodleError, httpx.HTTPError:
                for par in lote:
                    try:
                        await enviar([par])
                    except (MoodleError, httpx.HTTPError) as exc:
                        self._erro(result, descricao.format(*par), exc)
                        continue
                    aceitos.append(par)
            else:
                aceitos.extend(lote)
        return aceitos
