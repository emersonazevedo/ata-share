"""Persistência SQLite. Uma conexão compartilhada (WAL + busy_timeout) protegida por lock.

FTS5 para busca real entre reuniões — algo que o v1 prometia e não tinha.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

STATUS_RECORDING = "recording"
STATUS_TRANSCRIBING = "transcribing"
STATUS_SUMMARIZING = "summarizing"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id               TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    language         TEXT NOT NULL DEFAULT 'pt',
    started_at       TEXT NOT NULL,
    ended_at         TEXT,
    duration_seconds INTEGER,
    status           TEXT NOT NULL,
    folder           TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    retries          INTEGER NOT NULL DEFAULT 0,
    ticket                  TEXT,
    project_name            TEXT,
    client                  TEXT,
    cloud                   TEXT,
    obsidian_export_status  TEXT,
    obsidian_export_path    TEXT,
    obsidian_exported_at    TEXT,
    obsidian_export_error   TEXT
);

CREATE TABLE IF NOT EXISTS segments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    source     TEXT NOT NULL,            -- 'me' | 'system'
    start      REAL NOT NULL,
    end        REAL NOT NULL,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_segments_meeting ON segments(meeting_id, start, id);

CREATE TABLE IF NOT EXISTS summaries (
    meeting_id   TEXT PRIMARY KEY REFERENCES meetings(id) ON DELETE CASCADE,
    json         TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    diagnostics  TEXT
);

CREATE TABLE IF NOT EXISTS notes (
    meeting_id TEXT PRIMARY KEY REFERENCES meetings(id) ON DELETE CASCADE,
    text       TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
    text,
    meeting_id UNINDEXED,
    content='segments',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS segments_ai AFTER INSERT ON segments BEGIN
    INSERT INTO segments_fts(rowid, text, meeting_id) VALUES (new.id, new.text, new.meeting_id);
END;
CREATE TRIGGER IF NOT EXISTS segments_ad AFTER DELETE ON segments BEGIN
    INSERT INTO segments_fts(segments_fts, rowid, text, meeting_id) VALUES('delete', old.id, old.text, old.meeting_id);
END;
"""

# Colunas adicionadas depois do schema inicial: `CREATE TABLE IF NOT EXISTS` não
# altera uma tabela `meetings` já existente, então bancos antigos precisam de
# ALTER TABLE explícito (idempotente: só roda para a coluna que ainda não existe).
_MEETINGS_MIGRATIONS: dict[str, str] = {
    "ticket": "TEXT",
    "project_name": "TEXT",
    "client": "TEXT",
    "cloud": "TEXT",
    "obsidian_export_status": "TEXT",
    "obsidian_export_path": "TEXT",
    "obsidian_exported_at": "TEXT",
    "obsidian_export_error": "TEXT",
}


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate_meetings_columns()
            self._conn.commit()

    def _migrate_meetings_columns(self) -> None:
        """Roda sob `self._lock`, chamado só no __init__."""
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(meetings)").fetchall()}
        for column, sql_type in _MEETINGS_MIGRATIONS.items():
            if column not in existing:
                self._conn.execute(f"ALTER TABLE meetings ADD COLUMN {column} {sql_type}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- escrita ----------------------------------------------------------
    def create_meeting(
        self, meeting_id: str, title: str, language: str, started_at: str, folder: str,
        *, ticket: str | None = None, project_name: str | None = None,
        client: str | None = None, cloud: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meetings(id,title,language,started_at,status,folder,created_at,"
                "ticket,project_name,client,cloud) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (meeting_id, title, language, started_at, STATUS_RECORDING, folder, started_at,
                 ticket or None, project_name or None, client or None, cloud or None),
            )
            self._conn.commit()

    def update_project_metadata(
        self, meeting_id: str, *, ticket: str | None = None, project_name: str | None = None,
        client: str | None = None, cloud: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE meetings SET ticket=?, project_name=?, client=?, cloud=? WHERE id=?",
                (ticket or None, project_name or None, client or None, cloud or None, meeting_id),
            )
            self._conn.commit()

    def save_obsidian_export(
        self, meeting_id: str, *, status: str, path: str | None, error: str | None, exported_at: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE meetings SET obsidian_export_status=?, obsidian_export_path=?, "
                "obsidian_exported_at=?, obsidian_export_error=? WHERE id=?",
                (status, path, exported_at, error, meeting_id),
            )
            self._conn.commit()

    def set_status(self, meeting_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE meetings SET status=? WHERE id=?", (status, meeting_id))
            self._conn.commit()

    def finish_recording(self, meeting_id: str, ended_at: str, duration_seconds: int, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE meetings SET ended_at=?, duration_seconds=?, status=? WHERE id=?",
                (ended_at, duration_seconds, status, meeting_id),
            )
            self._conn.commit()

    def bump_retries(self, meeting_id: str) -> int:
        with self._lock:
            self._conn.execute("UPDATE meetings SET retries=retries+1 WHERE id=?", (meeting_id,))
            self._conn.commit()
            row = self._conn.execute("SELECT retries FROM meetings WHERE id=?", (meeting_id,)).fetchone()
            return int(row["retries"]) if row else 0

    def add_segment(self, meeting_id: str, source: str, start: float, end: float, text: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO segments(meeting_id,source,start,end,text,created_at) VALUES(?,?,?,?,?,?)",
                (meeting_id, source, round(start, 3), round(end, 3), text, _now()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def replace_segments(self, meeting_id: str, rows: Iterable[tuple[str, float, float, str]]) -> None:
        """Usado pelo re-processamento opcional (beam alto)."""
        with self._lock:
            self._conn.execute("DELETE FROM segments WHERE meeting_id=?", (meeting_id,))
            self._conn.executemany(
                "INSERT INTO segments(meeting_id,source,start,end,text,created_at) VALUES(?,?,?,?,?,?)",
                [(meeting_id, s, round(a, 3), round(b, 3), t, _now()) for (s, a, b, t) in rows],
            )
            self._conn.commit()

    def save_summary(self, meeting_id: str, payload: dict[str, Any], diagnostics: dict[str, Any] | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO summaries(meeting_id,json,generated_at,diagnostics) VALUES(?,?,?,?) "
                "ON CONFLICT(meeting_id) DO UPDATE SET json=excluded.json, generated_at=excluded.generated_at, "
                "diagnostics=excluded.diagnostics",
                (meeting_id, json.dumps(payload, ensure_ascii=False), _now(),
                 json.dumps(diagnostics, ensure_ascii=False) if diagnostics else None),
            )
            self._conn.commit()

    def save_notes(self, meeting_id: str, text: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO notes(meeting_id,text,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(meeting_id) DO UPDATE SET text=excluded.text, updated_at=excluded.updated_at",
                (meeting_id, text, _now()),
            )
            self._conn.commit()

    def delete_meeting(self, meeting_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT folder FROM meetings WHERE id=?", (meeting_id,)).fetchone()
            if row is None:
                return None
            self._conn.execute("DELETE FROM meetings WHERE id=?", (meeting_id,))
            self._conn.commit()
            return row["folder"]

    # --- leitura --------------------------------------------------------
    def list_meetings(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM meetings ORDER BY started_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_meeting(self, meeting_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
        return dict(row) if row else None

    def get_segments(self, meeting_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT source,start,end,text FROM segments WHERE meeting_id=? ORDER BY start,id",
                (meeting_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_summary(self, meeting_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT json,generated_at,diagnostics FROM summaries WHERE meeting_id=?", (meeting_id,)
            ).fetchone()
        if row is None:
            return None
        data = json.loads(row["json"])
        data["_generated_at"] = row["generated_at"]
        if row["diagnostics"]:
            data["_diagnostics"] = json.loads(row["diagnostics"])
        return data

    def get_notes(self, meeting_id: str) -> str:
        with self._lock:
            row = self._conn.execute("SELECT text FROM notes WHERE meeting_id=?", (meeting_id,)).fetchone()
        return row["text"] if row else ""

    def stuck_meetings(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM meetings WHERE status IN (?,?,?)",
                (STATUS_RECORDING, STATUS_TRANSCRIBING, STATUS_SUMMARIZING),
            ).fetchall()
        return [dict(r) for r in rows]

    def search(self, query: str, limit: int = 50) -> list[dict[str, Any]]:
        """Busca full-text entre TODAS as reuniões. Retorna trechos com snippet."""
        query = query.strip()
        if not query:
            return []
        match = " ".join(f'"{tok}"' for tok in query.split())
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT m.id AS meeting_id, m.title AS title, m.started_at AS started_at,
                       s.source AS source, s.start AS start,
                       snippet(segments_fts, 0, '[', ']', ' … ', 12) AS snippet
                FROM segments_fts
                JOIN segments s ON s.id = segments_fts.rowid
                JOIN meetings m ON m.id = s.meeting_id
                WHERE segments_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (match, limit),
            ).fetchall()
        return [dict(r) for r in rows]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
