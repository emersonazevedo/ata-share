"""Reuniões longas: eco ME×SYSTEM, recuperação retomável, telemetria de atraso."""

import logging
import types
import wave

import pytest

from ata import asr as asrmod
from ata import db as dbmod
from ata import pipeline as pipemod
from ata.config import PostprocessConfig, load_config
from ata.db import Database
from ata.events import EventHub
from ata.postprocess import dedupe_echo
from ata.telemetry import StageTimes, free_memory_mb

_CFG = PostprocessConfig()


def _seg(source, start, text):
    return {"source": source, "start": start, "end": start + 2, "text": text}


# --- eco ---------------------------------------------------------------------------

def test_dedupe_echo_drops_me_that_repeats_system_nearby():
    segs = [_seg("system", 10.0, "Então a gente acabou trazendo as pessoas."),
            _seg("me", 11.5, "Então a gente acabou trazendo as pessoas"),
            _seg("me", 40.0, "Concordo, pode seguir."),
            _seg("system", 40.5, "Próximo assunto: segurança.")]
    out = dedupe_echo(segs, _CFG)
    assert [s["text"] for s in out] == ["Então a gente acabou trazendo as pessoas.",
                                        "Concordo, pode seguir.", "Próximo assunto: segurança."]


def test_dedupe_echo_drops_long_me_with_word_overlap_despite_low_ratio():
    # cada canal cortou/errou diferente: ratio < 0.6, mas a maioria das palavras é a mesma
    sys_text = ("Então o Damasceno falou pra agendar uma reunião com ele e o Gontijo, "
                "mas precisa ter acesso a GCP aí e depois a gente vê")
    me_text = "Vai ter que fazer uma reunião com o Damasceno lá e o Gontijo pra fazer isso"
    out = dedupe_echo([_seg("system", 10.0, sys_text), _seg("me", 11.0, me_text)], _CFG)
    assert [s["source"] for s in out] == ["system"]


def test_dedupe_echo_keeps_short_me_even_if_words_are_shared():
    segs = [_seg("system", 10.0, "sim, pode ser assim mesmo então"), _seg("me", 11.0, "sim pode ser")]
    assert len(dedupe_echo(segs, _CFG)) == 2


def test_dedupe_echo_keeps_same_text_when_far_in_time():
    segs = [_seg("system", 10.0, "ok, pode ser"), _seg("me", 100.0, "ok, pode ser")]
    assert len(dedupe_echo(segs, _CFG)) == 2


def test_dedupe_echo_disabled_and_no_system():
    segs = [_seg("system", 1.0, "a b c"), _seg("me", 1.0, "a b c")]
    assert dedupe_echo(segs, PostprocessConfig(dedupe_echo=False)) == segs
    only_me = [_seg("me", 1.0, "oi")]
    assert dedupe_echo(only_me, _CFG) == only_me


# --- telemetria --------------------------------------------------------------------------

def test_stage_times_logs_stages(caplog):
    t = StageTimes("abcdef123456", "teste")
    t.mark("a"); t.mark("b")
    with caplog.at_level(logging.INFO, logger="ata.telemetry"):
        t.log()
    assert "a=" in caplog.text and "b=" in caplog.text and "ram_livre=" in caplog.text


def test_free_memory_never_raises():
    value = free_memory_mb()
    assert value is None or value > 0


def test_asr_lag_warning_published_and_throttled(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ATA_PATHS_DATA_DIR", str(tmp_path / "data"))
    cfg = load_config(tmp_path / "nope.toml")
    hub = EventHub()
    events = []
    monkeypatch.setattr(hub, "publish", lambda mid, ev: events.append(ev))
    worker = asrmod.AsrWorker("m" * 32, cfg, None, hub)      # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING, logger="ata.asr"):
        worker._log_lag(lag=cfg.postprocess.asr_lag_warn_seconds + 60, rtf=1.2)
        worker._log_lag(lag=cfg.postprocess.asr_lag_warn_seconds + 90, rtf=1.2)   # dentro dos 5 min
    warns = [e for e in events if e["type"] == "warn"]
    assert len(warns) == 1 and "atrás" in warns[0]["message"]
    worker._log_lag(lag=5, rtf=0.5)
    assert len([e for e in events if e["type"] == "warn"]) == 1


# --- transcribe_file retomável -------------------------------------------------------------

def test_transcribe_file_start_at_offsets_timestamps(tmp_path, monkeypatch):
    wav = tmp_path / "a.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000 * 10)               # 10 s de silêncio
    seen = {}

    class _Model:
        def transcribe(self, audio, **kw):
            seen["samples"] = len(audio)
            return iter([types.SimpleNamespace(start=0.5, end=1.5, text=" oi ")]), types.SimpleNamespace(duration=5.0)

    monkeypatch.setattr(asrmod, "get_model", lambda cfg: _Model())
    cfg = load_config(tmp_path / "nope.toml")
    rows, got = [], []
    out = asrmod.transcribe_file(cfg, str(wav), "system", start_at=5.0, on_row=got.append)
    assert seen["samples"] == 16000 * 5                          # só os 5 s finais
    assert out == [("system", 5.5, 6.5, "oi")] and got == out    # tempos absolutos


# --- recuperação retomável -------------------------------------------------------------------

@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(pipemod.os, "startfile", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(pipemod.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setenv("ATA_PATHS_DATA_DIR", str(tmp_path / "data"))
    cfg = load_config(tmp_path / "nope.toml")
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    yield cfg, db, pipemod.MeetingPipeline(cfg, db, EventHub())
    db.close()


def _meeting_with_live_segments(cfg, db):
    mid = "meet-resume"
    folder = cfg.meetings_path / mid
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("system.wav", "microphone.wav"):
        with wave.open(str(folder / name), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 16000)
    db.create_meeting(mid, "Longa", "pt", "2026-10-15T18:00:00Z", str(folder))
    db.set_status(mid, dbmod.STATUS_TRANSCRIBING)
    db.add_segment(mid, "system", 0.0, 100.0, "ao vivo system")
    db.add_segment(mid, "me", 0.0, 60.0, "ao vivo me")
    return mid


def test_recovery_resumes_after_last_live_segment(env, monkeypatch):
    cfg, db, pipe = env
    mid = _meeting_with_live_segments(cfg, db)
    calls = {}

    def fake(config, wav, source, *, start_at=0.0, on_row=None, **kw):
        calls[source] = start_at
        rows = [(source, start_at, start_at + 0.5, "já coberto"), (source, 120.0, 130.0, f"novo {source}")]
        for r in rows:
            on_row(r)
        return rows

    monkeypatch.setattr(pipemod, "transcribe_file", fake)
    seen = {}

    def fake_sum(segments, *a, **k):
        seen["texts"] = [s["text"] for s in segments]
        return {"summary": "ok", "agenda_items": [], "action_items": [], "next_steps": "", "other_notes": []}, {"status": "ok"}

    monkeypatch.setattr(pipemod, "summarize_meeting", fake_sum)
    monkeypatch.setattr(pipe, "export_obsidian", lambda m: {})

    pipe._reprocess_job(db.get_meeting(mid), 1, resume=True)

    assert calls == {"system": 99.0, "me": 59.0}                 # retoma 1 s antes do último trecho
    texts = [s["text"] for s in db.get_segments(mid)]
    assert "ao vivo system" in texts and "ao vivo me" in texts   # nada foi descartado
    assert "novo system" in texts and "novo me" in texts
    assert "já coberto" not in texts                              # sem duplicar a emenda
    assert db.get_meeting(mid)["status"] == dbmod.STATUS_DONE


def test_user_reprocess_still_replaces_everything(env, monkeypatch):
    cfg, db, pipe = env
    mid = _meeting_with_live_segments(cfg, db)
    monkeypatch.setattr(pipemod, "transcribe_file", lambda c, w, s, **k: [(s, 0.0, 1.0, f"refeito {s}")])
    monkeypatch.setattr(pipemod, "summarize_meeting", lambda *a, **k: (
        {"summary": "ok", "agenda_items": [], "action_items": [], "next_steps": "", "other_notes": []}, {"status": "ok"}))
    monkeypatch.setattr(pipe, "export_obsidian", lambda m: {})
    pipe._reprocess_job(db.get_meeting(mid), 5)
    assert sorted(s["text"] for s in db.get_segments(mid)) == ["refeito me", "refeito system"]
