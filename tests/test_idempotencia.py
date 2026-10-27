import pytest

from app.interfaces import MoodleError
from app.sync.idempotencia import ConflitoError, enviar_em_lotes, localizar

pytestmark = pytest.mark.anyio


def test_localizar_prefere_o_idnumber() -> None:
    por_idnumber = {"a": {"id": 1, "idnumber": "a"}}
    por_nome = {"login": {"id": 2, "idnumber": ""}}

    assert localizar(por_idnumber, por_nome, "a", "login") == {"id": 1, "idnumber": "a"}
    assert localizar({}, por_nome, "a", "login") == {"id": 2, "idnumber": ""}
    assert localizar({}, por_nome, "a", "outro") is None


def test_localizar_recusa_nome_de_outro_registro() -> None:
    with pytest.raises(ConflitoError):
        localizar({}, {"login": {"id": 2, "idnumber": "b"}}, "a", "login")


async def test_lote_recusado_e_reenviado_um_a_um() -> None:
    enviados: list[list[int]] = []
    falhas: list[int] = []

    async def enviar(lote: list[int]) -> dict[str, int]:
        enviados.append(lote)
        if 3 in lote:
            raise MoodleError("teste", "invalidparameter", "")
        return {str(i): i * 10 for i in lote}

    aceitos, ids = await enviar_em_lotes(
        [1, 2, 3, 4], enviar, lambda item, exc: falhas.append(item)
    )

    assert enviados == [[1, 2, 3, 4], [1], [2], [3], [4]]
    assert aceitos == [1, 2, 4]
    assert falhas == [3]
    assert ids == {"1": 10, "2": 20, "4": 40}
