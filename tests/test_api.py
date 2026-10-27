from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "integration.db"))
    monkeypatch.setenv("SYNC_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("MOODLE_SETUP_FILE", str(tmp_path / "nao-existe.json"))
    get_settings.cache_clear()
    with TestClient(app) as client:
        yield client
    get_settings.cache_clear()


def test_entidade_desconhecida_e_recusada(client: TestClient) -> None:
    assert client.post("/sync/notas").status_code == 422


def test_moodle_nao_configurado_responde_503_e_fica_no_historico(
    client: TestClient,
) -> None:
    resposta = client.post("/sync/professores")

    assert resposta.status_code == 503
    assert "moodle-setup" in resposta.json()["detail"]
    execucao = client.get("/sync/execucoes").json()["execucoes"][0]
    assert (execucao["entity"], execucao["status"]) == ("professores", "failed")
