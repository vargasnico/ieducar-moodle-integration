"""Caso de integração 3: alunos do i-Educar viram usuários e alunos dos cursos no Moodle.

Aluno no Moodle é o aluno ativo com enturmação ativa no ano letivo: matrícula ativa,
enturmação sem exclusão e sem data de saída. Transferência, remanejamento, abandono e
falecimento preenchem a data de saída e tiram o aluno da turma.

Cada aluno é inscrito como student nos cursos (turma × componente, criados no caso 2)
da sua turma, mas só nos componentes da sua série (numa turma multisseriada cada série
tem os seus) e sem os componentes de que tem dispensa ativa — qualquer dispensa,
mesmo que só de algumas etapas, tira o aluno do curso do componente.
"""

from collections import defaultdict

from app.sync.base import Sincronizacao
from app.sync.inscricoes import reconciliar_inscricoes
from app.sync.usuarios import ALUNO, Pessoa, sincronizar_usuarios


class AlunoSync(Sincronizacao):
    papel = "student"

    async def executar(self, ano: int, escolas: list[int]) -> None:
        alunos = {
            a.aluno_id: a
            for a in await self.ieducar.listar_alunos(ano, escolas)
            if a.ativo
        }
        matriculas = {
            m.matricula_id: m
            for m in await self.ieducar.listar_matriculas(ano, escolas)
            if m.ativo and m.aluno_id in alunos
        }
        enturmacoes = [
            e
            for e in await self.ieducar.listar_enturmacoes(ano, escolas)
            if e.ativa and e.matricula_id in matriculas
        ]
        dispensas = {
            (d.matricula_id, d.componente_id)
            for d in await self.ieducar.listar_dispensas(escolas)
            if d.ativa
        }
        self.resultado.conta("alunos_lidos", len(alunos))
        self.resultado.conta("enturmacoes_ativas", len(enturmacoes))

        cursos = self.repo.list_cursos(ano)
        por_par = {(c.turma_id, c.componente_id): c for c in cursos if c.visible}
        cursos_do_aluno: dict[int, set[int]] = defaultdict(set)
        for enturmacao in enturmacoes:
            matricula = matriculas[enturmacao.matricula_id]
            for componente_id in await self.componentes(
                matricula.escola_id, [enturmacao.serie_id], ano
            ):
                curso = por_par.get((enturmacao.turma_id, componente_id))
                if (matricula.matricula_id, componente_id) in dispensas:
                    self.resultado.conta("dispensas_aplicadas")
                elif curso:
                    cursos_do_aluno[matricula.aluno_id].add(curso.moodle_course_id)

        async def pessoa(aluno_id: int) -> Pessoa:
            aluno = alunos[aluno_id]
            idnumber = f"ieducar-aluno-{aluno_id}"
            return Pessoa(
                origem_id=aluno_id,
                idnumber=idnumber,
                # O código do aluno nunca muda; a matrícula é renovada a cada ano.
                username=f"aluno{aluno_id}",
                nome=aluno.nome_social or aluno.nome,
                # A API do i-Educar não expõe o e-mail do aluno.
                email=self.email_provisorio(idnumber),
                updated_at=aluno.updated_at,
            )

        enturmados = {matriculas[e.matricula_id].aluno_id for e in enturmacoes}
        usuarios = await sincronizar_usuarios(
            self,
            ALUNO,
            {a.aluno_id: a.updated_at for a in alunos.values()},
            enturmados,
            pessoa,
        )
        desejadas = {
            (course_id, usuarios[aluno_id])
            for aluno_id, course_ids in cursos_do_aluno.items()
            if aluno_id in usuarios
            for course_id in course_ids
        }
        await reconciliar_inscricoes(self, desejadas, cursos)
