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
  3. Turma desativada, ou componente que saiu da série: o curso é ocultado. As
     inscrições de cursos ocultos não são alteradas, para preservar o histórico.
  4. Professores: inscritos nos cursos dos componentes dos seus vínculos ativos e
     desinscritos quando o vínculo sai. Os vínculos e os usuários vêm do caso 1.
"""

import logging
from dataclasses import dataclass, field

import httpx

from app.clients.ieducar import ComponenteCurricular, IEducarClient, IEducarError, Turma
from app.clients.moodle import MoodleClient, MoodleError
from app.config import MoodleSetup
from app.db import CursoMapping, Repository
from app.sync.contexto import resolver_contexto

log = logging.getLogger(__name__)

PAPEL_PROFESSOR = "editingteacher"
# Erro do Moodle ao atualizar um curso que foi excluído.
CURSO_REMOVIDO = {"invalidrecord", "invalidcourseid"}


def categoria_idnumber(ano: int) -> str:
    return f"ieducar-{ano}"


def curso_idnumber(turma_id: int, componente_id: int) -> str:
    """Identificador estável do curso no Moodle: o par turma × componente."""
    return f"ieducar-turma-{turma_id}-componente-{componente_id}"


def curso_shortname(ano: int, turma_id: int, componente_id: int) -> str:
    return f"{ano}-T{turma_id}-C{componente_id}"


def curso_fullname(turma: Turma, componente: ComponenteCurricular) -> str:
    return f"{turma.nome} – {componente.nome} ({turma.ano})"


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

    async def run(self, completo: bool = False) -> TurmaSyncResult:
        """`completo` reenvia ao Moodle todos os cursos e inscrições desejados, mesmo
        os que não mudaram (corrige alterações feitas direto no Moodle)."""
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

        desejados: dict[tuple[int, int], str] = {}
        for turma in turmas:
            if not turma.ativo:
                continue
            for componente in await self._componentes_da_turma(turma):
                key = (turma.turma_id, componente.componente_id)
                desejados[key] = curso_fullname(turma, componente)

        existentes = {
            (c.turma_id, c.componente_id): c for c in self._repo.list_cursos(ano)
        }
        for key, fullname in sorted(desejados.items()):
            try:
                await self._garantir_curso(
                    key,
                    fullname,
                    ano,
                    categoria_id,
                    existentes.get(key),
                    completo,
                    result,
                )
            except (MoodleError, httpx.HTTPError) as exc:
                self._erro(result, f"curso turma {key[0]} × componente {key[1]}", exc)

        for key, curso in sorted(existentes.items()):
            if key in desejados or not curso.visible:
                continue
            try:
                await self._ocultar(curso, result)
            except (MoodleError, httpx.HTTPError) as exc:
                self._erro(result, f"ocultar curso {curso.moodle_course_id}", exc)

        await self._inscrever_professores(ano, completo, result)
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
                    # Série sem componentes na escola: a API responde com erro.
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
        key: tuple[int, int],
        fullname: str,
        ano: int,
        categoria_id: int,
        mapping: CursoMapping | None,
        completo: bool,
        result: TurmaSyncResult,
    ) -> None:
        turma_id, componente_id = key
        if (
            mapping
            and not completo
            and mapping.fullname == fullname
            and mapping.visible
        ):
            result.cursos_inalterados += 1
            return

        course_id = mapping.moodle_course_id if mapping else None
        if course_id is not None:
            try:
                await self._moodle.update_course(
                    {"id": course_id, "fullname": fullname, "visible": True}
                )
            except MoodleError as exc:
                if exc.errorcode not in CURSO_REMOVIDO:
                    raise
                log.warning("Curso %s não existe mais no Moodle; recriando", course_id)
                course_id = None
            else:
                result.cursos_atualizados += 1

        if course_id is None:
            # Sem mapeamento válido, procura pelo idnumber antes de criar.
            idnumber = curso_idnumber(turma_id, componente_id)
            existente = await self._moodle.get_course_by_field("idnumber", idnumber)
            if existente:
                course_id = int(existente["id"])
                await self._moodle.update_course(
                    {"id": course_id, "fullname": fullname, "visible": True}
                )
                result.cursos_atualizados += 1
            else:
                course_id = await self._moodle.create_course(
                    {
                        "fullname": fullname,
                        "shortname": curso_shortname(ano, turma_id, componente_id),
                        "categoryid": categoria_id,
                        "idnumber": idnumber,
                        "visible": True,
                    }
                )
                result.cursos_criados += 1
                log.info("Curso %s criado no Moodle (id=%s)", fullname, course_id)

        self._repo.save_curso(turma_id, componente_id, ano, course_id, fullname, True)

    async def _ocultar(self, curso: CursoMapping, result: TurmaSyncResult) -> None:
        try:
            await self._moodle.update_course(
                {"id": curso.moodle_course_id, "visible": False}
            )
        except MoodleError as exc:
            if exc.errorcode not in CURSO_REMOVIDO:
                raise
            self._repo.delete_curso(curso.turma_id, curso.componente_id)
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
        self, ano: int, completo: bool, result: TurmaSyncResult
    ) -> None:
        cursos = {(c.turma_id, c.componente_id): c for c in self._repo.list_cursos(ano)}
        visiveis = {c.moodle_course_id for c in cursos.values() if c.visible}
        usuarios = {
            p.servidor_id: p.moodle_user_id
            for p in self._repo.list_professores()
            if p.ativo
        }

        desejadas: set[tuple[int, int]] = set()
        for servidor_id, turma_id, componentes in self._repo.list_vinculos_ativos(ano):
            user_id = usuarios.get(servidor_id)
            if user_id is None:
                continue
            for componente_id in componentes:
                curso = cursos.get((turma_id, componente_id))
                if curso and curso.visible:
                    desejadas.add((curso.moodle_course_id, user_id))

        # Só as inscrições dos cursos visíveis deste ano: as de cursos ocultos ficam.
        atuais = {
            (course_id, user_id)
            for course_id, user_id in self._repo.list_professor_inscricoes()
            if course_id in visiveis
        }
        role_id = self._setup.role_ids[PAPEL_PROFESSOR]

        novas = desejadas if completo else desejadas - atuais
        for course_id, user_id in sorted(novas):
            try:
                await self._moodle.enrol_user(role_id, user_id, course_id)
            except (MoodleError, httpx.HTTPError) as exc:
                self._erro(
                    result, f"inscrever usuário {user_id} no curso {course_id}", exc
                )
                continue
            self._repo.save_professor_inscricao(course_id, user_id)
            if (course_id, user_id) not in atuais:
                result.inscricoes_criadas += 1

        for course_id, user_id in sorted(atuais - desejadas):
            try:
                await self._moodle.unenrol_user(user_id, course_id)
            except (MoodleError, httpx.HTTPError) as exc:
                self._erro(
                    result, f"desinscrever usuário {user_id} do curso {course_id}", exc
                )
                continue
            self._repo.delete_professor_inscricao(course_id, user_id)
            result.inscricoes_removidas += 1
