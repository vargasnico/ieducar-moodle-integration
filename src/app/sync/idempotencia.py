"""Escrita no Moodle sem duplicados e com falhas isoladas.

- `localizar`: antes de criar, procura o registro que já existe no Moodle pelo
  idnumber gravado pela integração ou pelo nome. Assim um mapeamento local perdido
  não gera duplicados.
- `enviar_em_lotes`: o Moodle processa cada chamada numa transação; se um item
  inválido derruba o lote, reenvia um a um para que só ele falhe.
"""

from collections.abc import Awaitable, Callable, Sequence
from itertools import batched
from typing import Any

import httpx

from app.clients.moodle import MoodleError

# Itens por chamada ao Moodle: valores demais passam do max_input_vars do PHP (1000)
# e o excedente é descartado sem erro.
LOTE = 50


class ConflitoError(Exception):
    """O login ou nome curto já pertence a outro registro do Moodle."""


def localizar(
    por_idnumber: dict[str, Any], por_nome: dict[str, Any], idnumber: str, nome: str
) -> dict[str, Any] | None:
    """Registro a adotar: o de mesmo idnumber ou, se não houver, o de mesmo nome
    (login ou nome curto) sem idnumber, como uma conta criada à mão."""
    if idnumber in por_idnumber:
        return por_idnumber[idnumber]
    existente = por_nome.get(nome)
    if existente and existente.get("idnumber"):
        raise ConflitoError(
            f"{nome} já pertence ao registro {existente['id']} do Moodle"
            f" (idnumber {existente['idnumber']})"
        )
    return existente


async def enviar_em_lotes[T](
    itens: Sequence[T],
    enviar: Callable[[list[T]], Awaitable[dict[str, int] | None]],
    ao_falhar: Callable[[T, Exception], None],
) -> tuple[list[T], dict[str, int]]:
    """Envia os itens em lotes. Devolve os aceitos e os ids que o Moodle devolveu
    para os criados (login ou nome curto → id)."""
    aceitos: list[T] = []
    ids: dict[str, int] = {}

    async def tentar(lote: list[T]) -> bool:
        try:
            ids.update(await enviar(lote) or {})
        except (MoodleError, httpx.HTTPError) as exc:
            if len(lote) == 1:
                ao_falhar(lote[0], exc)
            return False
        aceitos.extend(lote)
        return True

    for lote in batched(itens, LOTE):
        if not await tentar(list(lote)) and len(lote) > 1:
            for item in lote:
                await tentar([item])
    return aceitos, ids
