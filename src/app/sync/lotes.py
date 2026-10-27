from collections.abc import Awaitable, Callable, Sequence
from itertools import batched

import httpx

from app.clients.moodle import MoodleError

# Itens por chamada ao Moodle, longe do max_input_vars padrão do PHP (1000).
LOTE_INSCRICOES = 100


async def enviar_em_lotes[T](
    itens: Sequence[T],
    enviar: Callable[[list[T]], Awaitable[None]],
    ao_falhar: Callable[[T, Exception], None],
    tamanho: int = LOTE_INSCRICOES,
) -> list[T]:
    """Envia itens em lotes e devolve os aceitos. O Moodle processa cada lote numa
    transação; se um item inválido derruba o lote, reenvia um a um."""
    aceitos: list[T] = []
    for lote in batched(itens, tamanho):
        try:
            await enviar(list(lote))
        except MoodleError, httpx.HTTPError:
            for item in lote:
                try:
                    await enviar([item])
                except (MoodleError, httpx.HTTPError) as exc:
                    ao_falhar(item, exc)
                    continue
                aceitos.append(item)
        else:
            aceitos.extend(lote)
    return aceitos
