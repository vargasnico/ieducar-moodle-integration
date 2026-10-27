import logging
from dataclasses import dataclass

from app.clients.ieducar import IEducarClient

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Contexto:
    """Escolas e ano letivo de uma execução; `erro` explica quando não há o que sincronizar."""

    escolas: list[int]
    ano: int | None
    erro: str | None = None


async def resolver_contexto(
    ieducar: IEducarClient, escola_id: int | None, ano_letivo: int | None
) -> Contexto:
    """Usa a escola e o ano configurados ou descobre pela API: as escolas ativas e o
    ano letivo em andamento da primeira delas."""
    if escola_id is not None:
        escolas = [escola_id]
    else:
        escolas = [e.escola_id for e in await ieducar.listar_escolas() if e.ativo]
    if not escolas:
        return Contexto(escolas, None, "Nenhuma escola ativa no i-Educar.")
    if len(escolas) > 1 and ano_letivo is None:
        log.warning(
            "Mais de uma escola ativa (%s); usando o ano letivo da primeira. "
            "Defina IEDUCAR_ANO_LETIVO para escolher.",
            escolas,
        )
    ano = (
        ano_letivo
        if ano_letivo is not None
        else await ieducar.ano_letivo_em_andamento(escolas[0])
    )
    if ano is None:
        return Contexto(escolas, None, "Nenhum ano letivo em andamento na escola.")
    return Contexto(escolas, ano)
