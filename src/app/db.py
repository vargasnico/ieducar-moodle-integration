import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS professor_mapping (
    servidor_id        INTEGER PRIMARY KEY,
    moodle_user_id     INTEGER NOT NULL UNIQUE,
    moodle_username    TEXT    NOT NULL,
    ativo              INTEGER NOT NULL,
    ieducar_updated_at TEXT    NOT NULL,
    synced_at          TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity      TEXT    NOT NULL,
    trigger     TEXT    NOT NULL,
    started_at  TEXT    NOT NULL,
    finished_at TEXT,
    status      TEXT    NOT NULL,
    summary     TEXT
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ProfessorMapping:
    servidor_id: int
    moodle_user_id: int
    moodle_username: str
    ativo: bool
    ieducar_updated_at: str
    synced_at: str


class Repository:
    """Persistência local da integração: mapeamento i-Educar ↔ Moodle e estado das sincronizações."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def get_state(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM sync_state WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sync_state (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_professor(self, servidor_id: int) -> ProfessorMapping | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM professor_mapping WHERE servidor_id = ?", (servidor_id,)
            ).fetchone()
        return _to_mapping(row) if row else None

    def list_professores(self) -> list[ProfessorMapping]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM professor_mapping ORDER BY servidor_id"
            ).fetchall()
        return [_to_mapping(r) for r in rows]

    def save_professor(
        self,
        servidor_id: int,
        moodle_user_id: int,
        username: str,
        ativo: bool,
        updated_at: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO professor_mapping
                    (servidor_id, moodle_user_id, moodle_username, ativo, ieducar_updated_at, synced_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(servidor_id) DO UPDATE SET
                    moodle_user_id = excluded.moodle_user_id,
                    moodle_username = excluded.moodle_username,
                    ativo = excluded.ativo,
                    ieducar_updated_at = excluded.ieducar_updated_at,
                    synced_at = excluded.synced_at
                """,
                (servidor_id, moodle_user_id, username, int(ativo), updated_at, now()),
            )

    def start_run(self, entity: str, trigger: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO sync_run (entity, trigger, started_at, status) VALUES (?, ?, ?, 'running')",
                (entity, trigger, now()),
            )
        return int(cur.lastrowid or 0)

    def finish_run(self, run_id: int, status: str, summary: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sync_run SET finished_at = ?, status = ?, summary = ? WHERE id = ?",
                (now(), status, summary, run_id),
            )

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sync_run ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def _to_mapping(row: sqlite3.Row) -> ProfessorMapping:
    return ProfessorMapping(
        servidor_id=row["servidor_id"],
        moodle_user_id=row["moodle_user_id"],
        moodle_username=row["moodle_username"],
        ativo=bool(row["ativo"]),
        ieducar_updated_at=row["ieducar_updated_at"],
        synced_at=row["synced_at"],
    )
