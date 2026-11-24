"""Integração do MeetingPipeline sem áudio nem modelo reais:
monkeypatch em transcribe_file e summarize_meeting.

Cobre o que o report do Codex apontou: idioma da reunião chega ao ASR,
reprocesso não roda com reunião ativa, exclusão durante reprocesso é barrada,
e a recuperação no boot NÃO bloqueia.
"""

import dataclasses
import wave

import pytest

from ata import db as dbmod
from ata import pipeline as pipemod
from ata.config import ObsidianConfig, load_config
from ata.db import Database
from ata.events import EventHub
from ata.obsidian import ObsidianExportResult


@pytest.fixture()
def env(tmp_path, monkeypatch):
    # `_reveal()` chama os.startfile/subprocess.Popen para abrir o Explorer/Finder de
    # verdade (comportamento real e desejado da UI). `_finalize`/`_reprocess_job` disparam
    # isso sozinhos quando `open_folder_on_done=True` (default) e a reunião termina em
    # `done` — o que este arquivo faz o tempo todo via `_reprocess_job` direto. Sem este
    # patch, cada `pytest` real abria uma janela do Explorer por teste. Patch no nível do
    # módulo (não numa instância) para cobrir qualquer MeetingPipeline criado no teste.
    monkeypatch.setattr(pipemod.os, "startfile", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(pipemod.subprocess, "Popen", lambda *a, **k: None)

    monkeypatch.setenv("ATA_PATHS_DATA_DIR", str(tmp_path / "data"))
    cfg = load_config(tmp_path / "nope.toml")
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    hub = EventHub()
    pipe = pipemod.MeetingPipeline(cfg, db, hub)
    yield cfg, db, hub, pipe
    db.close()


def _make_meeting(cfg, db, language="en"):
    mid = "meet1"
    folder = cfg.meetings_path / mid
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("system.wav", "microphone.wav"):
        with wave.open(str(folder / name), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 16000)
    db.create_meeting(mid, "Reunião EN", language, "2026-09-02T12:00:00Z", str(folder))
    db.finish_recording(mid, "2026-09-02T12:05:00Z", 300, dbmod.STATUS_SUMMARIZING)
    return mid, folder


def test_reprocess_passes_meeting_language_to_asr(env, monkeypatch):
    cfg, db, hub, pipe = env
    mid, _ = _make_meeting(cfg, db, language="en")
    seen = {}

    def fake_transcribe(config, wav_path, source, *, beam_size=None, language=None, progress=None):
        seen["language"] = language
        return [(source, 0.0, 1.0, f"hello from {source}")]

    monkeypatch.setattr(pipemod, "transcribe_file", fake_transcribe)
    monkeypatch.setattr(pipemod, "summarize_meeting",
                        lambda *a, **k: ({"summary": "ok", "agenda_items": [],
                                          "action_items": [], "next_steps": "",
                                          "other_notes": []}, {"status": "ok"}))

    pipe._reprocess_job(db.get_meeting(mid), beam_size=5)

    assert seen["language"] == "en"                       # <- bug do Codex: idioma ignorado
    assert db.get_meeting(mid)["status"] == dbmod.STATUS_DONE
    texts = [s["text"] for s in db.get_segments(mid)]
    assert "hello from system" in texts and "hello from me" in texts


def test_reprocess_blocked_while_meeting_active(env):
    cfg, db, hub, pipe = env
    mid, _ = _make_meeting(cfg, db)

    class _FakeActive:
        meeting_id = "other"
    pipe._active = _FakeActive()
    with pytest.raises(RuntimeError, match="ocupado|andamento"):
        pipe.reprocess(mid, 5)


def test_delete_blocked_during_reprocess(env):
    cfg, db, hub, pipe = env
    mid, _ = _make_meeting(cfg, db)
    pipe._reprocessing.add(mid)
    with pytest.raises(RuntimeError, match="reprocess"):
        pipe.delete(mid)


def test_recover_does_not_block_and_marks_no_wav_failed(env, monkeypatch):
    cfg, db, hub, pipe = env
    # reunião presa SEM wav -> failed direto
    db.create_meeting("nowav", "X", "pt", "2026-09-02T12:00:00Z", str(cfg.meetings_path / "nowav"))
    calls = []
    monkeypatch.setattr(pipe, "_reprocess_job", lambda *a, **k: calls.append(a))

    pipe.recover()  # deve retornar imediatamente

    assert db.get_meeting("nowav")["status"] == dbmod.STATUS_FAILED
    assert calls == []


# --- exportação Obsidian ------------------------------------------------------

def _pipe_with_obsidian(cfg, db, hub, **obsidian_overrides):
    obsidian_cfg = ObsidianConfig(enabled=True, auto_export=True, **obsidian_overrides)
    cfg2 = dataclasses.replace(cfg, obsidian=obsidian_cfg)
    return cfg2, pipemod.MeetingPipeline(cfg2, db, hub)


def _fake_summarize(*a, **k):
    return ({"summary": "ok", "agenda_items": [], "action_items": [], "next_steps": "",
             "other_notes": [], "participants": [], "decisions": [], "risks": [],
             "technical_points": {"services": [], "architecture": [], "dependencies": []},
             "cloud_providers": []}, {"status": "ok"})


def test_reprocess_triggers_obsidian_export_when_auto_export_enabled(env, monkeypatch, tmp_path):
    cfg, db, hub, _ = env
    vault = tmp_path / "vault"; vault.mkdir()
    cfg2, pipe = _pipe_with_obsidian(cfg, db, hub, vault_path=str(vault))
    mid, _ = _make_meeting(cfg2, db)
    monkeypatch.setattr(pipemod, "transcribe_file", lambda cfg, wav, source, **k: [(source, 0.0, 1.0, "hello")])
    monkeypatch.setattr(pipemod, "summarize_meeting", _fake_summarize)

    calls = []
    monkeypatch.setattr(pipemod, "export_meeting_note",
                        lambda *a, **k: calls.append(a) or ObsidianExportResult(status="ok", path="x.md"))

    pipe._reprocess_job(db.get_meeting(mid), beam_size=5)

    assert db.get_meeting(mid)["status"] == dbmod.STATUS_DONE
    assert len(calls) == 1
    assert db.get_meeting(mid)["obsidian_export_status"] == "ok"
    assert db.get_meeting(mid)["obsidian_export_path"] == "x.md"


def test_reprocess_does_not_export_when_auto_export_disabled(env, monkeypatch, tmp_path):
    cfg, db, hub, _ = env
    vault = tmp_path / "vault"; vault.mkdir()
    cfg2 = dataclasses.replace(cfg, obsidian=ObsidianConfig(enabled=True, auto_export=False, vault_path=str(vault)))
    pipe = pipemod.MeetingPipeline(cfg2, db, hub)
    mid, _ = _make_meeting(cfg2, db)
    monkeypatch.setattr(pipemod, "transcribe_file", lambda *a, **k: [("me", 0.0, 1.0, "hello")])
    monkeypatch.setattr(pipemod, "summarize_meeting", _fake_summarize)
    calls = []
    monkeypatch.setattr(pipemod, "export_meeting_note", lambda *a, **k: calls.append(a))

    pipe._reprocess_job(db.get_meeting(mid), beam_size=5)

    assert db.get_meeting(mid)["status"] == dbmod.STATUS_DONE
    assert calls == []  # auto_export=False -> nada disparado automaticamente
    assert db.get_meeting(mid)["obsidian_export_status"] is None


def test_export_obsidian_failure_never_marks_meeting_failed(env, monkeypatch, tmp_path):
    cfg, db, hub, _ = env
    cfg2, pipe = _pipe_with_obsidian(cfg, db, hub, vault_path=str(tmp_path / "nao-existe"))
    mid, _ = _make_meeting(cfg2, db)
    db.finish_recording(mid, "2026-09-02T12:05:00Z", 300, dbmod.STATUS_DONE)

    result = pipe.export_obsidian(mid)

    assert result["status"] == "failed"
    assert result["error"]
    assert db.get_meeting(mid)["status"] == dbmod.STATUS_DONE  # não foi contaminado
    assert db.get_meeting(mid)["obsidian_export_status"] == "failed"


def test_export_obsidian_manual_call_works_even_with_auto_export_disabled(env, tmp_path):
    cfg, db, hub, _ = env
    vault = tmp_path / "vault"; vault.mkdir()
    cfg2 = dataclasses.replace(cfg, obsidian=ObsidianConfig(enabled=True, auto_export=False, vault_path=str(vault)))
    pipe = pipemod.MeetingPipeline(cfg2, db, hub)
    mid, _ = _make_meeting(cfg2, db)
    db.finish_recording(mid, "2026-09-02T12:05:00Z", 300, dbmod.STATUS_DONE)

    result = pipe.export_obsidian(mid)  # endpoint manual ignora auto_export

    assert result["status"] == "ok"
    assert db.get_meeting(mid)["obsidian_export_status"] == "ok"


def test_export_obsidian_skipped_when_disabled(env, tmp_path):
    cfg, db, hub, pipe = env  # obsidian.enabled=False por default
    mid, _ = _make_meeting(cfg, db)
    db.finish_recording(mid, "2026-09-02T12:05:00Z", 300, dbmod.STATUS_DONE)

    result = pipe.export_obsidian(mid)

    assert result["status"] == "skipped"
    assert db.get_meeting(mid)["obsidian_export_status"] is None  # skipped não é gravado no banco


# --- rebuild do Dashboard ------------------------------------------------------

def test_export_obsidian_ok_triggers_dashboard_rebuild(env, tmp_path):
    cfg, db, hub, _ = env
    vault = tmp_path / "vault"; vault.mkdir()
    cfg2, pipe = _pipe_with_obsidian(cfg, db, hub, vault_path=str(vault))  # dashboard_enabled=True (default)
    mid, _ = _make_meeting(cfg2, db)
    db.finish_recording(mid, "2026-09-02T12:05:00Z", 300, dbmod.STATUS_DONE)

    result = pipe.export_obsidian(mid)  # export real (não mockado) escreve a nota de verdade em `vault`

    assert result["status"] == "ok"
    dashboard_path = vault / "Dashboard" / "Dashboard.md"
    assert dashboard_path.is_file()
    assert "# 🏠 Dashboard" in dashboard_path.read_text(encoding="utf-8")


def test_export_obsidian_does_not_rebuild_dashboard_when_dashboard_disabled(env, tmp_path):
    cfg, db, hub, _ = env
    vault = tmp_path / "vault"; vault.mkdir()
    cfg2, pipe = _pipe_with_obsidian(cfg, db, hub, vault_path=str(vault), dashboard_enabled=False)
    mid, _ = _make_meeting(cfg2, db)
    db.finish_recording(mid, "2026-09-02T12:05:00Z", 300, dbmod.STATUS_DONE)

    result = pipe.export_obsidian(mid)

    assert result["status"] == "ok"
    assert not (vault / "Dashboard").exists()


def test_dashboard_rebuild_failure_never_affects_meeting_or_export(env, monkeypatch, tmp_path):
    cfg, db, hub, _ = env
    vault = tmp_path / "vault"; vault.mkdir()
    cfg2, pipe = _pipe_with_obsidian(cfg, db, hub, vault_path=str(vault))
    mid, _ = _make_meeting(cfg2, db)
    db.finish_recording(mid, "2026-09-02T12:05:00Z", 300, dbmod.STATUS_DONE)

    monkeypatch.setattr(pipemod, "rebuild_dashboard_file",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    result = pipe.export_obsidian(mid)  # export em si deve continuar "ok" mesmo com o dashboard explodindo

    assert result["status"] == "ok"
    assert db.get_meeting(mid)["status"] == dbmod.STATUS_DONE
    assert db.get_meeting(mid)["obsidian_export_status"] == "ok"


def test_rebuild_dashboard_public_method_returns_failed_on_exception(env, monkeypatch, tmp_path):
    cfg, db, hub, _ = env
    vault = tmp_path / "vault"; vault.mkdir()
    cfg2, pipe = _pipe_with_obsidian(cfg, db, hub, vault_path=str(vault))

    monkeypatch.setattr(pipemod, "rebuild_dashboard_file",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    result = pipe.rebuild_dashboard()

    assert result["status"] == "failed" and "boom" in result["error"]


def test_rebuild_dashboard_manual_call_works_standalone(env, tmp_path):
    cfg, db, hub, _ = env
    vault = tmp_path / "vault"; vault.mkdir()
    (vault / "Reunioes").mkdir()
    cfg2, pipe = _pipe_with_obsidian(cfg, db, hub, vault_path=str(vault))

    result = pipe.rebuild_dashboard()

    assert result["status"] == "ok"
    assert (vault / "Dashboard" / "Dashboard.md").is_file()


def test_new_meeting_folder_uses_note_name_and_disambiguates(env):
    cfg, db, hub, pipe = env
    first = pipe._new_meeting_folder("aaaaaaaa1111", "Daily: Time", "2026-10-02T15:00:00Z")
    assert first.name == "2026-10-02 - Daily- Time"      # mesmo nome da nota, sem ':'
    assert first.parent == cfg.meetings_path and first.is_dir()

    second = pipe._new_meeting_folder("bbbbbbbb2222", "Daily: Time", "2026-10-02T15:00:00Z")
    assert second.name == "2026-10-02 - Daily- Time (bbbbbbbb)"
    assert second != first and second.is_dir()
