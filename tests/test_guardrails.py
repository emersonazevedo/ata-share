"""Guardrail de duração: aviso 60 s antes, corte automático e cancelamento pelo usuário.

Sem áudio real: monta um `_Active` falso direto no pipeline e chama o guard com
tempos curtos (limite em segundos inteiros).
"""

import threading
import time

import pytest

from ata import pipeline as pipemod
from ata.config import GuardrailsConfig, load_config
from tests.test_pipeline import env  # noqa: F401 - fixture


def _activate(pipe, db, started_offset=0.0):
    mid = "live1"
    db.create_meeting(mid, "Live", "pt", "2026-09-02T12:00:00Z", "x")
    active = pipemod._Active(mid, None, None, None, time.time() - started_offset, "Live", "pt")
    pipe._active = active
    pipe._finalize = lambda *a, **k: None   # não roda captura/ASR/ata
    return active


def _events(hub, mid):
    return [e["type"] for e in hub._history.get(mid, [])]


def test_defaults_and_config(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.guardrails.max_recording_minutes == 25
    assert cfg.guardrails.warn_before_seconds == 60
    p = tmp_path / "config.toml"
    p.write_text("[guardrails]\nmax_recording_minutes = 40\nbeep = false\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.guardrails.max_recording_minutes == 40
    assert cfg.guardrails.beep is False


def test_warns_then_stops_at_limit(env):  # noqa: F811
    cfg, db, hub, pipe = env
    active = _activate(pipe, db)
    pipe._beep = lambda *a, **k: None
    pipe._limit_guard(active, limit=1, warn=1)
    types = _events(hub, "live1")
    assert types.index("limit_warning") < types.index("limit_reached")
    assert pipe.active_id is None   # stop() foi chamado


def test_continue_extends_then_warns_again_and_stops(env):  # noqa: F811
    """Continuar renova por mais um período; ao fim dele avisa de novo e encerra."""
    cfg, db, hub, pipe = env
    active = _activate(pipe, db)
    pipe._beep = lambda *a, **k: None
    t = threading.Thread(target=pipe._limit_guard, args=(active, 2, 1))
    t.start()
    time.sleep(1.3)                       # já passou o 1º aviso
    pipe.cancel_auto_stop("live1")        # "Continuar gravando"
    t.join(timeout=10)
    assert not t.is_alive()
    types = _events(hub, "live1")
    assert types.count("limit_warning") == 2          # avisou de novo após o período extra
    assert "limit_extended" in types
    assert types.index("limit_extended") < types.index("limit_reached")
    assert pipe.active_id is None                     # e encerrou


def test_manual_stop_cancels_guard(env):  # noqa: F811
    cfg, db, hub, pipe = env
    active = _activate(pipe, db)
    t = threading.Thread(target=pipe._limit_guard, args=(active, 30, 5))
    t.start()
    pipe.stop()
    t.join(timeout=5)
    assert not t.is_alive()
    assert "limit_warning" not in _events(hub, "live1")


def test_disabled_limit_starts_no_guard(env, monkeypatch):  # noqa: F811
    cfg, db, hub, pipe = env
    import dataclasses
    pipe.cfg = dataclasses.replace(cfg, guardrails=GuardrailsConfig(max_recording_minutes=0))
    active = _activate(pipe, db)
    before = threading.active_count()
    pipe._start_limit_guard(active)
    assert threading.active_count() == before


def test_cancel_requires_active_meeting(env):  # noqa: F811
    _cfg, _db, _hub, pipe = env
    with pytest.raises(RuntimeError):
        pipe.cancel_auto_stop("nao-existe")
