"""DSP puro (numpy). Sem I/O, sem estado global — fácil de testar."""

from __future__ import annotations

import numpy as np

TARGET_SR = 16_000


def pcm16_to_mono_f32(pcm: bytes, channels: int, sample_rate: int) -> np.ndarray:
    """PCM16 intercalado -> float32 mono em 16 kHz, faixa [-1, 1]."""
    samples = np.frombuffer(pcm, dtype="<i2")
    if channels > 1:
        usable = (samples.size // channels) * channels
        samples = samples[:usable].reshape(-1, channels).mean(axis=1)
    mono = samples.astype(np.float32) / 32768.0
    return resample_to_16k(mono, sample_rate)


def resample_to_16k(mono: np.ndarray, sample_rate: int) -> np.ndarray:
    if sample_rate == TARGET_SR or mono.size == 0:
        return mono.astype(np.float32, copy=False)
    if sample_rate % TARGET_SR == 0:
        factor = sample_rate // TARGET_SR
        usable = (mono.size // factor) * factor
        if usable == 0:
            return np.zeros(0, dtype=np.float32)
        return mono[:usable].reshape(-1, factor).mean(axis=1).astype(np.float32)
    out_size = max(1, round(mono.size * TARGET_SR / sample_rate))
    src = np.arange(mono.size, dtype=np.float64)
    dst = np.linspace(0, mono.size - 1, out_size)
    return np.interp(dst, src, mono).astype(np.float32)


def f32_to_pcm16(mono: np.ndarray) -> bytes:
    clipped = np.clip(mono, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def frame_rms(mono: np.ndarray, frame: int) -> np.ndarray:
    """RMS por quadro (janelas contíguas de `frame` amostras)."""
    if mono.size < frame:
        return np.array([float(np.sqrt(np.mean(mono**2)))]) if mono.size else np.array([0.0])
    n = mono.size // frame
    block = mono[: n * frame].reshape(n, frame)
    return np.sqrt(np.mean(block * block, axis=1))


def silence_cut(
    mono: np.ndarray,
    sample_rate: int,
    target_seconds: float,
    min_seconds: float,
    max_seconds: float,
    frame_ms: int = 20,
) -> tuple[int, bool]:
    """Onde cortar a janela + se o corte cai num silêncio "de verdade".

    Retorna (índice_de_amostra, is_clean). `is_clean=False` quando o ponto mais
    silencioso ainda tem energia perto da mediana da região — sinal de que vale
    esperar mais áudio (até `max_seconds`) por um silêncio melhor.
    """
    n = mono.size
    lo = int(min_seconds * sample_rate)
    hi = min(int(max_seconds * sample_rate), n)
    if hi <= lo:
        return n, True
    target = int(target_seconds * sample_rate)
    frame = max(1, int(sample_rate * frame_ms / 1000))

    starts = np.arange(lo, hi - frame, frame)
    if starts.size == 0:
        return min(target, n), True
    rms = np.array([np.sqrt(np.mean(mono[s : s + frame] ** 2)) for s in starts])
    dist = np.abs(starts - target) / max(1, (hi - lo))
    score = rms + 0.15 * dist * float(rms.max() or 1.0)
    idx = int(np.argmin(score))
    cut_rms = float(rms[idx])
    median = float(np.median(rms)) or 1e-9
    is_clean = cut_rms < 0.35 * median or cut_rms < 0.02
    return int(starts[idx]), is_clean


def find_cut_point(
    mono: np.ndarray,
    sample_rate: int,
    target_seconds: float,
    min_seconds: float,
    max_seconds: float,
    frame_ms: int = 20,
) -> int:
    return silence_cut(mono, sample_rate, target_seconds, min_seconds, max_seconds, frame_ms)[0]
