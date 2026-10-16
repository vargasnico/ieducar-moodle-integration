import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ieducar_url: str = "http://localhost"
    ieducar_access_key: str = ""
    ieducar_instituicao_id: int = 1

    moodle_url: str = "http://localhost:8080"
    # Opcional: sobrescreve o token gerado pelo serviço moodle-setup.
    moodle_token: str = ""
    moodle_setup_file: Path = Path("/shared/moodle.json")

    database_path: Path = Path("data/integration.db")
    # Intervalo da sincronização periódica; 0 desativa o agendamento.
    sync_interval_seconds: int = 300
    # Domínio usado quando o servidor não tem e-mail no i-Educar.
    placeholder_email_domain: str = "sem-email.invalid"


@dataclass(frozen=True)
class MoodleSetup:
    """Dados gerados por moodle/setup_webservice.php."""

    token: str
    teacher_role_id: int
    category_context_id: int


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
        teacher_role_id=int(data["teacher_role_id"]),
        category_context_id=int(data["category_context_id"]),
    )
