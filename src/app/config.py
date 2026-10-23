import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Variáveis vazias (repassadas pelo compose sem valor) contam como não definidas.
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", env_parse_none_str=""
    )

    ieducar_url: str = "http://localhost"
    ieducar_access_key: str = ""
    ieducar_instituicao_id: int = 1
    # Opcionais: por padrão a integração usa a única escola ativa e o ano
    # letivo em andamento dela, ambos descobertos pela API do i-Educar.
    ieducar_escola_id: int | None = None
    ieducar_ano_letivo: int | None = None

    moodle_url: str = "http://localhost:8080"
    # Opcional: sobrescreve o token gerado pelo serviço moodle-setup.
    moodle_token: str | None = None
    moodle_setup_file: Path = Path("/shared/moodle.json")

    database_path: Path = Path("data/integration.db")
    # Intervalo da sincronização periódica; 0 desativa o agendamento.
    sync_interval_seconds: int = 300
    # O i-Educar grava updated_at ora em UTC (código legado, CURRENT_TIMESTAMP do
    # Postgres), ora no fuso da aplicação (models Laravel). As leituras
    # incrementais recuam o cursor por esta margem para não perder registros;
    # reprocessar o que não mudou é inofensivo.
    sync_cursor_overlap_hours: int = 24
    # Domínio usado quando o servidor não tem e-mail no i-Educar.
    placeholder_email_domain: str = "sem-email.invalid"


@dataclass(frozen=True)
class MoodleSetup:
    """Dados gerados por moodle/setup_webservice.php."""

    token: str
    category_id: int
    # shortname do papel → id (editingteacher, teacher, student).
    role_ids: dict[str, int] = field(default_factory=dict)


class MoodleNotConfiguredError(RuntimeError):
    pass


@lru_cache
def get_settings() -> Settings:
    return Settings()


def load_moodle_setup(settings: Settings) -> MoodleSetup:
    """Lê o arquivo do moodle-setup a cada uso, pois ele pode surgir depois da API subir."""
    try:
        data = json.loads(settings.moodle_setup_file.read_text())
    except FileNotFoundError as exc:
        raise MoodleNotConfiguredError(
            f"Arquivo {settings.moodle_setup_file} não encontrado; o serviço moodle-setup já rodou?"
        ) from exc
    return MoodleSetup(
        token=settings.moodle_token or data["token"],
        category_id=int(data["category_id"]),
        role_ids={k: int(v) for k, v in (data.get("role_ids") or {}).items()},
    )
