"""Caso de integração 2: turmas × componentes curriculares viram cursos no Moodle.

No i-Educar uma turma reúne alunos, professores e vários componentes curriculares.
No Moodle, cada par turma × componente vira um curso, com o professor do componente
inscrito como editingteacher. O curso é criado mesmo sem professor vinculado.

Cada execução recalcula o estado desejado a partir de todas as turmas do ano e só
envia ao Moodle o que difere do que a integração já criou:
  1. Garante a categoria do ano letivo (iEducar › <ano>).
  2. Turma ativa: um curso por componente que a escola oferece na série da turma
     (Séries da escola), criado ou com o nome atualizado e visível.
  3. Turma desativada, componente que saiu da série ou curso de outro ano letivo: o
     curso é ocultado. As inscrições de cursos ocultos não são alteradas, para
     preservar o histórico.
  4. Professores: inscritos nos cursos dos componentes dos seus vínculos ativos e
     desinscritos quando o vínculo sai. Os usuários vêm do caso 1.
"""

import logging
from typing import Any

import httpx

from app.db import CursoMapping
from app.interfaces import MoodleError
from app.sync.base import Sincronizacao
from app.sync.idempotencia import ConflitoError, enviar_em_lotes, localizar
from app.sync.inscricoes import reconciliar_inscricoes
from app.sync.usuarios import PROFESSOR

log = logging.getLogger(__name__)

# Erros do Moodle ao atualizar um curso que foi excluído.
CURSO_REMOVIDO = {"invalidrecord", "invalidcourseid"}

# (turma_id, componente_id)
Chave = tuple[int, int]


def curso_idnumber(turma_id: int, componente_id: int) -> str:
    """Identificador estável do curso no Moodle: o par turma × componente."""
    return f"ieducar-turma-{turma_id}-componente-{componente_id}"


def curso_shortname(ano: int, turma_id: int, componente_id: int) -> str:
    return f"{ano}-T{turma_id}-C{componente_id}"


def onde(chave: Chave) -> str:
    return f"curso turma {chave[0]} × componente {chave[1]}"


class TurmaSync(Sincronizacao):
    papel = "editingteacher"

    async def executar(self, ano: int, escolas: list[int]) -> None:
        categoria_id = await self._categoria(ano)
        turmas = await self.ieducar.listar_turmas(ano, escolas)
        self.resultado.conta("turmas_lidas", len(turmas))
        desejados: dict[Chave, str] = {}
        for turma in turmas:
            if not turma.ativo:
                continue
            componentes = await self.componentes(turma.escola_id, turma.series, ano)
            for c in componentes.values():
                desejados[(turma.turma_id, c.componente_id)] = (
                    f"{turma.nome} – {c.nome} ({ano})"
                )

        # Cursos de todos os anos: na troca de ano letivo os do ano anterior são ocultados.
        mapeados = {(c.turma_id, c.componente_id): c for c in self.repo.list_cursos()}
        a_criar: list[Chave] = []
        for chave, fullname in sorted(desejados.items()):
            curso = mapeados.get(chave)
            try:
                if not (curso and await self._atualizar(curso, fullname, True)):
                    a_criar.append(chave)
            except (MoodleError, httpx.HTTPError) as exc:
                self.resultado.erro(onde(chave), exc)
        await self._criar(a_criar, desejados, ano, categoria_id)

        for chave, curso in sorted(mapeados.items()):
            if chave in desejados or not curso.visible:
                continue
            try:
                await self._atualizar(curso, curso.fullname, False)
            except (MoodleError, httpx.HTTPError) as exc:
                self.resultado.erro(f"ocultar curso {curso.moodle_course_id}", exc)

        await self._inscrever_professores(ano, escolas)

    async def _categoria(self, ano: int) -> int:
        idnumber = f"ieducar-{ano}"
        existente = await self.moodle.get_category_by_idnumber(idnumber)
        if existente:
            return int(existente["id"])
        log.info("Criando no Moodle a categoria do ano %s", ano)
        return await self.moodle.create_category(
            str(ano),
            self.setup.category_id,
            idnumber,
            f"Turmas do ano letivo {ano} sincronizadas a partir do i-Educar.",
        )

    async def _atualizar(
        self, curso: CursoMapping, fullname: str, visible: bool
    ) -> bool:
        """Atualiza nome e visibilidade de um curso mapeado, se mudaram; False se ele
        não existe mais no Moodle."""
        if not self.completo and (curso.fullname, curso.visible) == (fullname, visible):
            self.resultado.conta("cursos_inalterados")
            return True
        try:
            await self.moodle.update_course(
                {"id": curso.moodle_course_id, "fullname": fullname, "visible": visible}
            )
        except MoodleError as exc:
            if exc.errorcode not in CURSO_REMOVIDO:
                raise
            log.warning("Curso %s não existe mais no Moodle", curso.moodle_course_id)
            self.repo.delete_curso(curso)
            return False
        self.repo.save_curso(
            curso.turma_id,
            curso.componente_id,
            curso.ano,
            curso.moodle_course_id,
            fullname,
            visible,
        )
        self.resultado.conta("cursos_atualizados" if visible else "cursos_ocultados")
        return True

    async def _criar(
        self, chaves: list[Chave], nomes: dict[Chave, str], ano: int, categoria_id: int
    ) -> None:
        """Adota os cursos que já existem na categoria do ano e cria os demais em lotes."""
        if not chaves:
            return
        na_categoria = await self.moodle.get_courses_by_field(
            "category", str(categoria_id)
        )
        por_idnumber = {c["idnumber"]: c for c in na_categoria if c.get("idnumber")}
        por_shortname = {c["shortname"]: c for c in na_categoria}

        def dados(chave: Chave) -> dict[str, Any]:
            return {
                "fullname": nomes[chave],
                "shortname": curso_shortname(ano, *chave),
                "idnumber": curso_idnumber(*chave),
                "categoryid": categoria_id,
                "visible": True,
            }

        def salvar(chave: Chave, course_id: int, evento: str) -> None:
            self.repo.save_curso(*chave, ano, course_id, nomes[chave], True)
            self.resultado.conta(evento)

        novos: list[Chave] = []
        for chave in chaves:
            curso = dados(chave)
            try:
                existente = localizar(
                    por_idnumber, por_shortname, curso["idnumber"], curso["shortname"]
                )
                if existente is None:
                    novos.append(chave)
                    continue
                course_id = int(existente["id"])
                await self.moodle.update_course(
                    {
                        "id": course_id,
                        "fullname": curso["fullname"],
                        "idnumber": curso["idnumber"],
                        "visible": True,
                    }
                )
            except (ConflitoError, MoodleError, httpx.HTTPError) as exc:
                self.resultado.erro(onde(chave), exc)
                continue
            salvar(chave, course_id, "cursos_atualizados")

        criados, ids = await enviar_em_lotes(
            novos,
            lambda lote: self.moodle.create_courses([dados(c) for c in lote]),
            lambda chave, exc: self.resultado.erro(onde(chave), exc),
        )
        for chave in criados:
            salvar(chave, ids[curso_shortname(ano, *chave)], "cursos_criados")

    async def _inscrever_professores(self, ano: int, escolas: list[int]) -> None:
        cursos = {(c.turma_id, c.componente_id): c for c in self.repo.list_cursos(ano)}
        usuarios = {
            u.origem_id: u.moodle_user_id
            for u in self.repo.list_usuarios(PROFESSOR)
            if u.ativo
        }
        desejadas: set[tuple[int, int]] = set()
        sem_curso: set[Chave] = set()
        for vinculo in await self.ieducar.listar_vinculos(ano, escolas):
            user_id = usuarios.get(vinculo.servidor_id)
            if not vinculo.ativo or user_id is None:
                continue
            for componente_id in vinculo.componentes:
                curso = cursos.get((vinculo.turma_id, componente_id))
                if curso is None:
                    sem_curso.add((vinculo.turma_id, componente_id))
                elif curso.visible:
                    desejadas.add((curso.moodle_course_id, user_id))
        if sem_curso:
            # Componente diferenciado da turma, ou que a escola não oferece na série.
            self.resultado.conta("componentes_sem_curso", len(sem_curso))
            log.warning(
                "Vínculos com componentes sem curso no Moodle (turma, componente): %s",
                sorted(sem_curso),
            )
        await reconciliar_inscricoes(self, desejadas, cursos.values())
