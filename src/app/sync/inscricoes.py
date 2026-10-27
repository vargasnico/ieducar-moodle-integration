"""Inscrições nos cursos do Moodle feitas pela integração (professores e alunos).

Compara as inscrições desejadas com as que a integração já fez e só envia a
diferença. Só contam os cursos visíveis do ano: as inscrições de cursos ocultos
ficam, para preservar o histórico, e as criadas à mão no Moodle nunca são desfeitas.
"""

from collections.abc import Iterable

from app.db import CursoMapping, Par
from app.sync.base import Sincronizacao
from app.sync.idempotencia import enviar_em_lotes


async def reconciliar_inscricoes(
    sync: Sincronizacao, desejadas: set[Par], cursos: Iterable[CursoMapping]
) -> None:
    """`desejadas` são pares (moodle_course_id, moodle_user_id) no papel do caso."""
    papel, resultado = sync.papel, sync.resultado
    role_id = sync.setup.role_ids[papel]
    visiveis = {c.moodle_course_id for c in cursos if c.visible}
    atuais = {par for par in sync.repo.list_inscricoes(papel) if par[0] in visiveis}

    feitas, _ = await enviar_em_lotes(
        sorted(desejadas if sync.completo else desejadas - atuais),
        lambda lote: sync.moodle.enrol_users(role_id, lote),
        lambda par, exc: resultado.erro(
            f"inscrever usuário {par[1]} no curso {par[0]}", exc
        ),
    )
    sync.repo.save_inscricoes(papel, feitas)
    resultado.conta("inscricoes_criadas", len(set(feitas) - atuais))

    removidas, _ = await enviar_em_lotes(
        sorted(atuais - desejadas),
        sync.moodle.unenrol_users,
        lambda par, exc: resultado.erro(
            f"desinscrever usuário {par[1]} do curso {par[0]}", exc
        ),
    )
    sync.repo.delete_inscricoes(papel, removidas)
    resultado.conta("inscricoes_removidas", len(removidas))
