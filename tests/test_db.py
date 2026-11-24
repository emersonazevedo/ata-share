import sqlite3

from ata import db as dbmod
from ata.db import Database


def _db(tmp_path):
    return Database(tmp_path / "t.db")


def test_meeting_lifecycle_and_segments(tmp_path):
    d = _db(tmp_path)
    d.create_meeting("m1", "Reunião 1", "pt", "2026-09-02T12:00:00Z", str(tmp_path / "m1"))
    d.add_segment("m1", "me", 1.0, 2.0, "primeira fala")
    d.add_segment("m1", "system", 2.0, 3.0, "resposta")
    d.finish_recording("m1", "2026-09-02T12:30:00Z", 1800, dbmod.STATUS_DONE)

    m = d.get_meeting("m1")
    assert m["status"] == "done" and m["duration_seconds"] == 1800
    segs = d.get_segments("m1")
    assert [s["text"] for s in segs] == ["primeira fala", "resposta"]


def test_fts_search_across_meetings(tmp_path):
    d = _db(tmp_path)
    d.create_meeting("m1", "Projeto A", "pt", "2026-09-01T10:00:00Z", str(tmp_path / "m1"))
    d.create_meeting("m2", "Projeto B", "pt", "2026-09-02T10:00:00Z", str(tmp_path / "m2"))
    d.add_segment("m1", "me", 0, 1, "precisamos revisar o orçamento do cluster")
    d.add_segment("m2", "system", 0, 1, "o orçamento aprovado foi menor")

    hits = d.search("orçamento")
    assert {h["meeting_id"] for h in hits} == {"m1", "m2"}
    # diacrítico-insensível
    assert d.search("orcamento")


def test_delete_cascades_segments_and_fts(tmp_path):
    d = _db(tmp_path)
    d.create_meeting("m1", "X", "pt", "2026-09-02T12:00:00Z", str(tmp_path / "m1"))
    d.add_segment("m1", "me", 0, 1, "palavra única zzz")
    assert d.delete_meeting("m1") == str(tmp_path / "m1")
    assert d.get_segments("m1") == []
    assert d.search("zzz") == []


def test_summary_and_notes_roundtrip(tmp_path):
    d = _db(tmp_path)
    d.create_meeting("m1", "X", "pt", "2026-09-02T12:00:00Z", str(tmp_path / "m1"))
    d.save_summary("m1", {"summary": "ok", "risks": ["r1"]}, {"status": "ok"})
    d.save_notes("m1", "minha nota")
    s = d.get_summary("m1")
    assert s["summary"] == "ok" and s["_diagnostics"]["status"] == "ok"
    assert d.get_notes("m1") == "minha nota"


def test_recover_lists_stuck(tmp_path):
    d = _db(tmp_path)
    d.create_meeting("m1", "X", "pt", "2026-09-02T12:00:00Z", str(tmp_path / "m1"))  # recording
    d.create_meeting("m2", "Y", "pt", "2026-09-02T12:00:00Z", str(tmp_path / "m2"))
    d.finish_recording("m2", "2026-09-02T12:10:00Z", 600, dbmod.STATUS_DONE)
    stuck = d.stuck_meetings()
    assert [m["id"] for m in stuck] == ["m1"]
    assert d.bump_retries("m1") == 1


def test_create_meeting_standalone_has_empty_project_fields(tmp_path):
    d = _db(tmp_path)
    d.create_meeting("m1", "X", "pt", "2026-09-02T12:00:00Z", str(tmp_path / "m1"))
    m = d.get_meeting("m1")
    assert m["ticket"] is None and m["project_name"] is None
    assert m["client"] is None and m["cloud"] is None


def test_create_meeting_with_project_metadata_roundtrip(tmp_path):
    d = _db(tmp_path)
    d.create_meeting("m1", "X", "pt", "2026-09-02T12:00:00Z", str(tmp_path / "m1"),
                      ticket="12345", project_name="Projeto X", client="Cliente Y", cloud="AWS")
    m = d.get_meeting("m1")
    assert m["ticket"] == "12345" and m["project_name"] == "Projeto X"
    assert m["client"] == "Cliente Y" and m["cloud"] == "AWS"


def test_update_project_metadata(tmp_path):
    d = _db(tmp_path)
    d.create_meeting("m1", "X", "pt", "2026-09-02T12:00:00Z", str(tmp_path / "m1"))
    d.update_project_metadata("m1", ticket="999", project_name="Nova Demanda")
    m = d.get_meeting("m1")
    assert m["ticket"] == "999" and m["project_name"] == "Nova Demanda"
    assert m["client"] is None  # não informado agora -> volta a None, não preserva valor antigo


def test_save_obsidian_export_roundtrip(tmp_path):
    d = _db(tmp_path)
    d.create_meeting("m1", "X", "pt", "2026-09-02T12:00:00Z", str(tmp_path / "m1"))
    d.save_obsidian_export("m1", status="ok", path="C:/vault/Reunioes/x.md", error=None,
                            exported_at="2026-09-02T12:35:00Z")
    m = d.get_meeting("m1")
    assert m["obsidian_export_status"] == "ok"
    assert m["obsidian_export_path"] == "C:/vault/Reunioes/x.md"
    assert m["obsidian_exported_at"] == "2026-09-02T12:35:00Z"
    assert m["obsidian_export_error"] is None


def test_migration_adds_columns_to_preexisting_database(tmp_path):
    """Simula um banco criado antes das colunas de projeto/Obsidian existirem."""
    path = tmp_path / "old.db"
    old_schema = """
    CREATE TABLE meetings (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, language TEXT NOT NULL DEFAULT 'pt',
        started_at TEXT NOT NULL, ended_at TEXT, duration_seconds INTEGER,
        status TEXT NOT NULL, folder TEXT NOT NULL, created_at TEXT NOT NULL,
        retries INTEGER NOT NULL DEFAULT 0
    );
    """
    conn = sqlite3.connect(path)
    conn.executescript(old_schema)
    conn.execute(
        "INSERT INTO meetings(id,title,language,started_at,status,folder,created_at) "
        "VALUES('m1','Antiga','pt','2026-01-01T00:00:00Z','done','/x','2026-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    d = Database(path)  # deve migrar sem quebrar
    m = d.get_meeting("m1")
    assert m["title"] == "Antiga"
    assert m["ticket"] is None
    assert m["obsidian_export_status"] is None
    d.update_project_metadata("m1", ticket="1")
    assert d.get_meeting("m1")["ticket"] == "1"
