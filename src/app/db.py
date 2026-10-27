import sqlite3
from collections.abc import Iterable, Iterator
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

CREATE TABLE IF NOT EXISTS aluno_mapping (
    aluno_id           INTEGER PRIMARY KEY,
    moodle_user_id     INTEGER NOT NULL UNIQUE,
    moodle_username    TEXT    NOT NULL,
    ativo              INTEGER NOT NULL,
    ieducar_updated_at TEXT    NOT NULL,
    synced_at          TEXT    NOT NULL
);

-- Inscrições de alunos feitas pela integração (mesma regra das de professores).
CREATE TABLE IF NOT EXISTS aluno_inscricao (
    moodle_course_id INTEGER NOT NULL,
    moodle_user_id   INTEGER NOT NULL,
    synced_at        TEXT    NOT NULL,
    PRIMARY KEY (moodle_course_id, moodle_user_id)
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
class AlunoMapping:
    aluno_id: int
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

    def save_professor_inscricoes(self, pares: Iterable[tuple[int, int]]) -> None:
        """Grava pares (moodle_course_id, moodle_user_id)."""
        agora = now()
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO professor_inscricao"
                " (moodle_course_id, moodle_user_id, synced_at) VALUES (?, ?, ?)",
                ((course_id, user_id, agora) for course_id, user_id in pares),
            )

    def delete_professor_inscricoes(self, pares: Iterable[tuple[int, int]]) -> None:
        """Remove pares (moodle_course_id, moodle_user_id)."""
        with self._connect() as conn:
            conn.executemany(
                "DELETE FROM professor_inscricao"
                " WHERE moodle_course_id = ? AND moodle_user_id = ?",
                pares,
            )

    def delete_inscricoes_do_curso(self, course_id: int) -> None:
        """Esquece as inscrições de um curso que não existe mais no Moodle."""
        with self._connect() as conn:
            for tabela in ("professor_inscricao", "aluno_inscricao"):
                conn.execute(
                    f"DELETE FROM {tabela} WHERE moodle_course_id = ?", (course_id,)
                )

    def list_alunos(self) -> list[AlunoMapping]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM aluno_mapping ORDER BY aluno_id"
            ).fetchall()
        return [
            AlunoMapping(
                aluno_id=r["aluno_id"],
                moodle_user_id=r["moodle_user_id"],
                moodle_username=r["moodle_username"],
                ativo=bool(r["ativo"]),
                ieducar_updated_at=r["ieducar_updated_at"],
                synced_at=r["synced_at"],
            )
            for r in rows
        ]

    def save_aluno(
        self,
        aluno_id: int,
        moodle_user_id: int,
        username: str,
        ativo: bool,
        updated_at: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO aluno_mapping
                    (aluno_id, moodle_user_id, moodle_username, ativo, ieducar_updated_at, synced_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(aluno_id) DO UPDATE SET
                    moodle_user_id = excluded.moodle_user_id,
                    moodle_username = excluded.moodle_username,
                    ativo = excluded.ativo,
                    ieducar_updated_at = excluded.ieducar_updated_at,
                    synced_at = excluded.synced_at
                """,
                (aluno_id, moodle_user_id, username, int(ativo), updated_at, now()),
            )

    def delete_aluno(self, aluno_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM aluno_mapping WHERE aluno_id = ?", (aluno_id,))

    def list_aluno_inscricoes(self) -> set[tuple[int, int]]:
        """(moodle_course_id, moodle_user_id) das inscrições de alunos da integração."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT moodle_course_id, moodle_user_id FROM aluno_inscricao"
            ).fetchall()
        return {(int(r["moodle_course_id"]), int(r["moodle_user_id"])) for r in rows}

    def save_aluno_inscricoes(self, pares: Iterable[tuple[int, int]]) -> None:
        agora = now()
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO aluno_inscricao"
                " (moodle_course_id, moodle_user_id, synced_at) VALUES (?, ?, ?)",
                ((course_id, user_id, agora) for course_id, user_id in pares),
            )

    def delete_aluno_inscricoes(self, pares: Iterable[tuple[int, int]]) -> None:
        with self._connect() as conn:
            conn.executemany(
                "DELETE FROM aluno_inscricao"
                " WHERE moodle_course_id = ? AND moodle_user_id = ?",
                pares,
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
