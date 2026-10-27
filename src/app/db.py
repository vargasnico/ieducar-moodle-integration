import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.clients.ieducar import Vinculo

SCHEMA = """
CREATE TABLE IF NOT EXISTS professor_mapping (
    servidor_id        INTEGER PRIMARY KEY,
    moodle_user_id     INTEGER NOT NULL UNIQUE,
    moodle_username    TEXT    NOT NULL,
    ativo              INTEGER NOT NULL,
    ieducar_updated_at TEXT    NOT NULL,
    synced_at          TEXT    NOT NULL
);

-- Cópia local dos vínculos professor ↔ turma do i-Educar. A API só devolve os
-- vínculos alterados desde o cursor; guardá-los permite saber se um servidor
-- ainda tem alguma turma depois de perder uma.
CREATE TABLE IF NOT EXISTS professor_vinculo (
    vinculo_id  INTEGER PRIMARY KEY,
    servidor_id INTEGER NOT NULL,
    turma_id    INTEGER NOT NULL,
    ano         INTEGER NOT NULL,
    componentes TEXT    NOT NULL,
    ativo       INTEGER NOT NULL,
    updated_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS professor_vinculo_servidor
    ON professor_vinculo (servidor_id, ano);

-- Curso do Moodle de cada turma × componente curricular do i-Educar.
CREATE TABLE IF NOT EXISTS curso_mapping (
    turma_id         INTEGER NOT NULL,
    componente_id    INTEGER NOT NULL,
    ano              INTEGER NOT NULL,
    moodle_course_id INTEGER NOT NULL UNIQUE,
    fullname         TEXT    NOT NULL,
    visible          INTEGER NOT NULL,
    synced_at        TEXT    NOT NULL,
    PRIMARY KEY (turma_id, componente_id)
);

-- Inscrições de professores feitas pela integração. Só estas são desfeitas:
-- inscrições criadas à mão no Moodle não são tocadas.
CREATE TABLE IF NOT EXISTS professor_inscricao (
    moodle_course_id INTEGER NOT NULL,
    moodle_user_id   INTEGER NOT NULL,
    synced_at        TEXT    NOT NULL,
    PRIMARY KEY (moodle_course_id, moodle_user_id)
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


@dataclass(frozen=True)
class CursoMapping:
    turma_id: int
    componente_id: int
    ano: int
    moodle_course_id: int
    fullname: str
    visible: bool
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

    def delete_professor(self, servidor_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM professor_mapping WHERE servidor_id = ?", (servidor_id,)
            )

    def list_professores_sem_turma(self, ano: int) -> list[int]:
        """Servidores ativos no Moodle sem nenhum vínculo ativo no ano (devem ser suspensos)."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT m.servidor_id FROM professor_mapping m
                WHERE m.ativo = 1 AND NOT EXISTS (
                    SELECT 1 FROM professor_vinculo v
                    WHERE v.servidor_id = m.servidor_id AND v.ano = ? AND v.ativo = 1
                )
                """,
                (ano,),
            ).fetchall()
        return [int(r["servidor_id"]) for r in rows]

    def save_vinculos(self, ano: int, vinculos: Iterable[Vinculo]) -> None:
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO professor_vinculo
                    (vinculo_id, servidor_id, turma_id, ano, componentes, ativo, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(vinculo_id) DO UPDATE SET
                    servidor_id = excluded.servidor_id,
                    turma_id = excluded.turma_id,
                    ano = excluded.ano,
                    componentes = excluded.componentes,
                    ativo = excluded.ativo,
                    updated_at = excluded.updated_at
                """,
                (
                    (
                        v.vinculo_id,
                        v.servidor_id,
                        v.turma_id,
                        ano,
                        ",".join(str(c) for c in v.componentes),
                        int(v.ativo),
                        v.updated_at,
                    )
                    for v in vinculos
                ),
            )

    def count_turmas_ativas(self, servidor_id: int, ano: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT turma_id) AS n FROM professor_vinculo"
                " WHERE servidor_id = ? AND ano = ? AND ativo = 1",
                (servidor_id, ano),
            ).fetchone()
        return int(row["n"])

    def list_vinculos_ativos(self, ano: int) -> list[tuple[int, int, tuple[int, ...]]]:
        """(servidor_id, turma_id, componentes) dos vínculos ativos do ano."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT servidor_id, turma_id, componentes FROM professor_vinculo"
                " WHERE ano = ? AND ativo = 1",
                (ano,),
            ).fetchall()
        return [
            (
                int(r["servidor_id"]),
                int(r["turma_id"]),
                tuple(int(c) for c in r["componentes"].split(",") if c),
            )
            for r in rows
        ]

    def list_cursos(self, ano: int | None = None) -> list[CursoMapping]:
        with self._connect() as conn:
            if ano is None:
                rows = conn.execute(
                    "SELECT * FROM curso_mapping ORDER BY turma_id, componente_id"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM curso_mapping WHERE ano = ?"
                    " ORDER BY turma_id, componente_id",
                    (ano,),
                ).fetchall()
        return [_to_curso(r) for r in rows]

    def save_curso(
        self,
        turma_id: int,
        componente_id: int,
        ano: int,
        moodle_course_id: int,
        fullname: str,
        visible: bool,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO curso_mapping
                    (turma_id, componente_id, ano, moodle_course_id, fullname, visible, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(turma_id, componente_id) DO UPDATE SET
                    ano = excluded.ano,
                    moodle_course_id = excluded.moodle_course_id,
                    fullname = excluded.fullname,
                    visible = excluded.visible,
                    synced_at = excluded.synced_at
                """,
                (
                    turma_id,
                    componente_id,
                    ano,
                    moodle_course_id,
                    fullname,
                    int(visible),
                    now(),
                ),
            )

    def delete_curso(self, turma_id: int, componente_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM curso_mapping WHERE turma_id = ? AND componente_id = ?",
                (turma_id, componente_id),
            )

    def list_professor_inscricoes(self) -> set[tuple[int, int]]:
        """(moodle_course_id, moodle_user_id) das inscrições feitas pela integração."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT moodle_course_id, moodle_user_id FROM professor_inscricao"
            ).fetchall()
        return {(int(r["moodle_course_id"]), int(r["moodle_user_id"])) for r in rows}

    def save_professor_inscricao(self, course_id: int, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO professor_inscricao"
                " (moodle_course_id, moodle_user_id, synced_at) VALUES (?, ?, ?)",
                (course_id, user_id, now()),
            )

    def delete_professor_inscricao(self, course_id: int, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM professor_inscricao"
                " WHERE moodle_course_id = ? AND moodle_user_id = ?",
                (course_id, user_id),
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


def _to_curso(row: sqlite3.Row) -> CursoMapping:
    return CursoMapping(
        turma_id=row["turma_id"],
        componente_id=row["componente_id"],
        ano=row["ano"],
        moodle_course_id=row["moodle_course_id"],
        fullname=row["fullname"],
        visible=bool(row["visible"]),
        synced_at=row["synced_at"],
    )


def _to_mapping(row: sqlite3.Row) -> ProfessorMapping:
    return ProfessorMapping(
        servidor_id=row["servidor_id"],
        moodle_user_id=row["moodle_user_id"],
        moodle_username=row["moodle_username"],
        ativo=bool(row["ativo"]),
        ieducar_updated_at=row["ieducar_updated_at"],
        synced_at=row["synced_at"],
    )
