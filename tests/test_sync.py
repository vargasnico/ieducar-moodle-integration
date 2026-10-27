"""Cenários de sincronização ponta a ponta contra o i-Educar e o Moodle em memória."""

from dataclasses import replace
from pathlib import Path

import pytest

from app.db import Repository
from app.domain import Vinculo
from app.sync.alunos import AlunoSync
from app.sync.professores import ProfessorSync
from app.sync.turmas import TurmaSync
from tests.conftest import MATEMATICA, PORTUGUES, TURMA, Rodar
from tests.fakes import FakeIEducar, FakeMoodle

pytestmark = pytest.mark.anyio

ANA = "ieducar-servidor-7"
CURSO_PORTUGUES = f"ieducar-turma-{TURMA}-componente-{PORTUGUES}"
CURSO_MATEMATICA = f"ieducar-turma-{TURMA}-componente-{MATEMATICA}"


async def rodar_tudo(rodar: Rodar, **kwargs: object) -> list[dict[str, int]]:
    return [
        (await rodar(classe, **kwargs)).contagens
        for classe in (ProfessorSync, TurmaSync, AlunoSync)
    ]


async def test_so_professor_com_turma_vira_usuario(
    rodar: Rodar, moodle: FakeMoodle
) -> None:
    resultado = await rodar(ProfessorSync)

    assert resultado.erros == []
    assert resultado.contagens["usuarios_criados"] == 1
    ana = moodle.usuario(ANA)
    assert (ana["username"], ana["firstname"], ana["lastname"], ana["email"]) == (
        "12345678900",
        "Ana",
        "Souza",
        "ana@x.br",
    )
    assert len(moodle.users) == 1  # Beto, da secretaria, não entra


async def test_turmas_cria_um_curso_por_componente_e_inscreve_o_professor(
    rodar: Rodar, moodle: FakeMoodle
) -> None:
    await rodar(ProfessorSync)
    resultado = await rodar(TurmaSync)

    assert resultado.erros == []
    assert moodle.curso(CURSO_PORTUGUES)["fullname"] == "1º ano A – Português (2026)"
    assert moodle.curso(CURSO_MATEMATICA)["shortname"] == "2026-T10-C2"
    assert moodle.inscritos(CURSO_PORTUGUES) == {"12345678900"}
    assert moodle.inscritos(CURSO_MATEMATICA) == set()


async def test_aluno_entra_nos_cursos_da_turma_menos_os_dispensados(
    rodar: Rodar, moodle: FakeMoodle
) -> None:
    await rodar_tudo(rodar)

    assert moodle.inscritos(CURSO_MATEMATICA) == {"aluno20"}
    assert moodle.inscritos(CURSO_PORTUGUES) == {"12345678900", "aluno20", "aluno21"}
    assert moodle.usuario("ieducar-aluno-21")["firstname"] == "Dani"  # nome social


async def test_segunda_execucao_nao_envia_nada_ao_moodle(
    rodar: Rodar, moodle: FakeMoodle
) -> None:
    await rodar_tudo(rodar)
    estado = (dict(moodle.users), dict(moodle.courses), set(moodle.enrolments))
    moodle.chamadas.clear()

    professores, turmas, alunos = await rodar_tudo(rodar)

    assert moodle.chamadas == []
    assert (moodle.users, moodle.courses, moodle.enrolments) == estado
    assert professores["usuarios_inalterados"] == 1
    assert turmas["cursos_inalterados"] == 2
    assert alunos["usuarios_inalterados"] == 2


async def test_mapeamento_perdido_adota_em_vez_de_duplicar(
    rodar: Rodar, moodle: FakeMoodle, tmp_path: Path
) -> None:
    await rodar_tudo(rodar)
    usuarios, cursos = len(moodle.users), len(moodle.courses)

    vazio = Repository(tmp_path / "outro.db")
    resultados = await rodar_tudo(rodar, repo=vazio)

    assert (len(moodle.users), len(moodle.courses)) == (usuarios, cursos)
    assert "usuarios_criados" not in resultados[0]
    assert resultados[1]["cursos_atualizados"] == 2


async def test_professor_que_perde_a_turma_e_suspenso_e_desinscrito(
    rodar: Rodar, ieducar: FakeIEducar, moodle: FakeMoodle
) -> None:
    await rodar_tudo(rodar)
    ieducar.vinculos = [replace(v, ativo=False) for v in ieducar.vinculos]

    professores, turmas, _ = await rodar_tudo(rodar)

    assert professores["usuarios_suspensos"] == 1
    assert moodle.usuario(ANA)["suspended"] is True
    assert turmas["inscricoes_removidas"] == 1
    assert "12345678900" not in moodle.inscritos(CURSO_PORTUGUES)

    # Um novo vínculo reativa o mesmo usuário.
    ieducar.vinculos.append(Vinculo(2, 7, TURMA, (MATEMATICA,), True, "2026-02-01"))
    await rodar_tudo(rodar)
    assert moodle.usuario(ANA)["suspended"] is False
    assert moodle.inscritos(CURSO_MATEMATICA) >= {"12345678900"}


async def test_aluno_que_sai_da_turma_e_desinscrito_e_suspenso(
    rodar: Rodar, ieducar: FakeIEducar, moodle: FakeMoodle
) -> None:
    await rodar_tudo(rodar)
    # Transferência: a enturmação continua no diário, mas ganha data de saída.
    ieducar.enturmacoes = [
        replace(e, ativa=e.matricula_id != 200) for e in ieducar.enturmacoes
    ]

    *_, alunos = await rodar_tudo(rodar)

    assert alunos["usuarios_suspensos"] == 1
    assert moodle.usuario("ieducar-aluno-20")["suspended"] is True
    assert "aluno20" not in moodle.inscritos(CURSO_PORTUGUES)


async def test_turma_desativada_oculta_os_cursos_e_mantem_as_inscricoes(
    rodar: Rodar, ieducar: FakeIEducar, moodle: FakeMoodle
) -> None:
    await rodar_tudo(rodar)
    ieducar.turmas = [replace(t, ativo=False) for t in ieducar.turmas]

    _, turmas, _ = await rodar_tudo(rodar)

    assert turmas["cursos_ocultados"] == 2
    assert moodle.curso(CURSO_PORTUGUES)["visible"] is False
    assert moodle.inscritos(CURSO_PORTUGUES) == {"12345678900", "aluno20", "aluno21"}


async def test_usuario_excluido_no_moodle_e_recriado(
    rodar: Rodar, moodle: FakeMoodle
) -> None:
    await rodar(ProfessorSync)
    del moodle.users[moodle.usuario(ANA)["id"]]

    resultado = await rodar(ProfessorSync, completo=True)

    assert resultado.erros == []
    assert resultado.contagens["usuarios_criados"] == 1
    assert moodle.usuario(ANA)["username"] == "12345678900"


async def test_login_de_outro_usuario_vira_erro_e_nao_e_sobrescrito(
    rodar: Rodar, moodle: FakeMoodle
) -> None:
    moodle.users[1] = {"id": 1, "username": "12345678900", "idnumber": "outro"}

    resultado = await rodar(ProfessorSync)

    assert resultado.erros and "já pertence ao registro 1" in resultado.erros[0]
    assert moodle.users[1]["idnumber"] == "outro"


async def test_conta_criada_a_mao_com_o_login_e_adotada(
    rodar: Rodar, moodle: FakeMoodle
) -> None:
    moodle.users[1] = {"id": 1, "username": "12345678900", "idnumber": ""}

    resultado = await rodar(ProfessorSync)

    assert resultado.erros == []
    assert moodle.users[1]["idnumber"] == ANA
    assert len(moodle.users) == 1


async def test_falha_momentanea_nao_tira_ninguem_dos_cursos(
    rodar: Rodar, ieducar: FakeIEducar, moodle: FakeMoodle
) -> None:
    await rodar_tudo(rodar)
    ieducar.alunos = [replace(a, updated_at="2026-03-01") for a in ieducar.alunos]
    moodle.fora_do_ar.add("update_user")

    resultado = await rodar(AlunoSync)

    assert len(resultado.erros) == 2
    assert resultado.contagens["inscricoes_removidas"] == 0
    assert moodle.inscritos(CURSO_PORTUGUES) == {"12345678900", "aluno20", "aluno21"}


async def test_aluno_invalido_nao_impede_a_criacao_dos_outros(
    rodar: Rodar, moodle: FakeMoodle
) -> None:
    await rodar(ProfessorSync)
    await rodar(TurmaSync)
    moodle.recusar.add("aluno21")

    resultado = await rodar(AlunoSync)

    assert resultado.contagens["usuarios_criados"] == 1
    assert len(resultado.erros) == 1 and resultado.erros[0].startswith("aluno 21")
    assert moodle.inscritos(CURSO_PORTUGUES) == {"12345678900", "aluno20"}
