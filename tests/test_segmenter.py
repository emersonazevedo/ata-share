import numpy as np

from ata.config import SegmenterConfig
from ata.dsp import TARGET_SR
from ata.segmenter import Segmenter


def _cfg(**kw):
    base = dict(target_seconds=10.0, min_seconds=4.0, max_seconds=12.0,
               carry_pad_seconds=0.1, flush_min_seconds=1.0)
    base.update(kw)
    return SegmenterConfig(**base)


def _noise(seconds, seed=0):
    rng = np.random.default_rng(seed)
    return (0.3 * rng.standard_normal(int(seconds * TARGET_SR))).astype(np.float32)


def test_no_emit_before_target():
    seg = Segmenter("me", _cfg())
    assert seg.push(_noise(6)) == []


def test_emits_window_at_target_and_keeps_remainder():
    seg = Segmenter("system", _cfg())
    out = seg.push(_noise(13))
    assert len(out) == 1
    w = out[0]
    assert w.source == "system"
    assert w.start == 0.0
    assert 4.0 <= w.duration <= 12.0


def test_timeline_is_continuous_across_windows():
    seg = Segmenter("me", _cfg())
    windows = []
    for _ in range(6):
        windows += seg.push(_noise(9, seed=len(windows)))
    windows += seg.flush()
    assert len(windows) >= 3
    for a, b in zip(windows, windows[1:]):
        # próxima janela começa perto do fim da anterior (com o carry pad)
        assert abs(b.start - (a.end - 0.1)) < 0.25


def test_flush_keeps_short_but_audible_tail():
    # frase curta dita bem na hora do Stop NÃO pode sumir
    seg = Segmenter("me", _cfg(flush_min_seconds=0.4))
    seg.push(_noise(0.8))
    assert len(seg.flush()) == 1


def test_flush_drops_only_short_silence():
    seg = Segmenter("me", _cfg(flush_min_seconds=0.4))
    seg.push(np.zeros(int(0.2 * TARGET_SR), dtype=np.float32))
    assert seg.flush() == []


def test_waits_for_silence_up_to_max_then_hard_cuts():
    cfg = _cfg(target_seconds=10.0, min_seconds=4.0, max_seconds=14.0)
    seg = Segmenter("system", cfg)
    # ruído contínuo, sem silêncio: não corta em target, espera até max
    out = seg.push(_noise(13.0, seed=1))
    assert out == []                       # 13s < max=14 e sem silêncio -> aguarda
    out = seg.push(_noise(3.0, seed=2))    # agora passa de 14s
    assert len(out) == 1
    assert 13.0 <= out[0].duration <= 14.5  # corte forçado perto de max, não de target


def test_cuts_at_silence_before_max_when_available():
    cfg = _cfg(target_seconds=10.0, min_seconds=4.0, max_seconds=20.0)
    seg = Segmenter("me", cfg)
    audio = _noise(18.0, seed=3)
    audio[int(11 * TARGET_SR): int(11.5 * TARGET_SR)] = 0.0   # silêncio em ~11s
    out = seg.push(audio)
    assert len(out) == 1
    assert 10.5 <= out[0].duration <= 12.0   # cortou no silêncio, bem antes de max
