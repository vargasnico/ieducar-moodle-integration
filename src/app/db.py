import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Colunas declaradas BOOLEAN voltam como bool.
sqlite3.register_converter("BOOLEAN", lambda valor: valor == b"1")

SCHEMA = """
-- Usuário do Moodle de cada servidor (professor) ou aluno do i-Educar.
CREATE TABLE IF NOT EXISTS usuario (
    tipo               TEXT    NOT NULL,  -- professor | aluno
    origem_id          INTEGER NOT NULL,  -- servidor_id ou aluno_id no i-Educar
    moodle_user_id     INTEGER NOT NULL UNIQUE,
    moodle_username    TEXT    NOT NULL,
    ativo              BOOLEAN NOT NULL,
    ieducar_updated_at TEXT    NOT NULL,
    synced_at          TEXT    NOT NULL,
    PRIMARY KEY (tipo, origem_id)
);

-- Curso do Moodle de cada turma × componente curricular do i-Educar.
CREATE TABLE IF NOT EXISTS curso (
    turma_id         INTEGER NOT NULL,
    componente_id    INTEGER NOT NULL,
    ano              INTEGER NOT NULL,
    moodle_course_id INTEGER NOT NULL UNIQUE,
    fullname         TEXT    NOT NULL,
    visible          BOOLEAN NOT NULL,
    synced_at        TEXT    NOT NULL,
    PRIMARY KEY (turma_id, componente_id)
);

-- Inscrições feitas pela integração. Só estas são desfeitas: inscrições criadas
-- à mão no Moodle não são tocadas.
CREATE TABLE IF NOT EXISTS inscricao (
    papel            TEXT    NOT NULL,  -- editingteacher | student
    moodle_course_id INTEGER NOT NULL,
    moodle_user_id   INTEGER NOT NULL,
    synced_at        TEXT    NOT NULL,
    PRIMARY KEY (papel, moodle_course_id, moodle_user_id)
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

# Inscrição: (moodle_course_id, moodle_user_id).
Par = tuple[int, int]


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class UsuarioMapping:
    tipo: str
    origem_id: int
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
    """Persistência local da integração: mapeamento i-Educar ↔ Moodle e histórico das execuções."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _query(self, sql: str, *params: Any) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(sql, params).fetchall()

    def _execute(self, sql: str, *params: Any) -> int:
        """Executa um comando e devolve o id da linha inserida, se houver."""
        with self._connect() as conn:
            return conn.execute(sql, params).lastrowid or 0

    def _upsert(self, tabela: str, chave: tuple[str, ...], **valores: Any) -> None:
        """Insere a linha ou, se a chave já existe, atualiza as demais colunas."""
        valores["synced_at"] = now()
        atualizar = ", ".join(f"{c} = excluded.{c}" for c in valores if c not in chave)
        self._execute(
            f"INSERT INTO {tabela} ({', '.join(valores)})"
            f" VALUES ({', '.join('?' * len(valores))})"
            f" ON CONFLICT ({', '.join(chave)}) DO UPDATE SET {atualizar}",
            *valores.values(),
        )

    def list_usuarios(self, tipo: str) -> list[UsuarioMapping]:
        rows = self._query(
            "SELECT * FROM usuario WHERE tipo = ? ORDER BY origem_id", tipo
        )
        return [UsuarioMapping(**r) for r in rows]

    def save_usuario(
        self,
        tipo: str,
        origem_id: int,
        moodle_user_id: int,
        moodle_username: str,
        ativo: bool,
        ieducar_updated_at: str,
    ) -> None:
        self._upsert(
            "usuario",
            ("tipo", "origem_id"),
            tipo=tipo,
            origem_id=origem_id,
            moodle_user_id=moodle_user_id,
            moodle_username=moodle_username,
            ativo=ativo,
            ieducar_updated_at=ieducar_updated_at,
        )

    def delete_usuario(self, tipo: str, origem_id: int) -> None:
        self._execute(
            "DELETE FROM usuario WHERE tipo = ? AND origem_id = ?", tipo, origem_id
        )

    def list_cursos(self, ano: int | None = None) -> list[CursoMapping]:
        rows = self._query(
            "SELECT * FROM curso WHERE ? IS NULL OR ano = ?"
            " ORDER BY turma_id, componente_id",
            ano,
            ano,
        )
        return [CursoMapping(**r) for r in rows]

    def save_curso(
        self,
        turma_id: int,
        componente_id: int,
        ano: int,
        moodle_course_id: int,
        fullname: str,
        visible: bool,
    ) -> None:
        self._upsert(
            "curso",
            ("turma_id", "componente_id"),
            turma_id=turma_id,
            componente_id=componente_id,
            ano=ano,
            moodle_course_id=moodle_course_id,
            fullname=fullname,
            visible=visible,
        )

    def delete_curso(self, curso: CursoMapping) -> None:
        """Esquece um curso que não existe mais no Moodle, com as suas inscrições."""
        with self._connect() as conn:
            for tabela in ("curso", "inscricao"):
                conn.execute(
                    f"DELETE FROM {tabela} WHERE moodle_course_id = ?",
                    (curso.moodle_course_id,),
                )

    def list_inscricoes(self, papel: str) -> set[Par]:
        rows = self._query(
            "SELECT moodle_course_id, moodle_user_id FROM inscricao WHERE papel = ?",
            papel,
        )
        return {(r["moodle_course_id"], r["moodle_user_id"]) for r in rows}

    def save_inscricoes(self, papel: str, pares: Iterable[Par]) -> None:
        agora = now()
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO inscricao VALUES (?, ?, ?, ?)",
                ((papel, course_id, user_id, agora) for course_id, user_id in pares),
            )

    def delete_inscricoes(self, papel: str, pares: Iterable[Par]) -> None:
        with self._connect() as conn:
            conn.executemany(
                "DELETE FROM inscricao"
                " WHERE papel = ? AND moodle_course_id = ? AND moodle_user_id = ?",
                ((papel, course_id, user_id) for course_id, user_id in pares),
            )

    def start_run(self, entity: str, trigger: str) -> int:
        return self._execute(
            "INSERT INTO sync_run (entity, trigger, started_at, status)"
            " VALUES (?, ?, ?, 'running')",
            entity,
            trigger,
            now(),
        )

    def finish_run(self, run_id: int, status: str, summary: str) -> None:
        self._execute(
            "UPDATE sync_run SET finished_at = ?, status = ?, summary = ? WHERE id = ?",
            now(),
            status,
            summary,
            run_id,
        )

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._query("SELECT * FROM sync_run ORDER BY id DESC LIMIT ?", limit)
        return [dict(r) for r in rows]
