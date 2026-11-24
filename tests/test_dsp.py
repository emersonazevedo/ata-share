import numpy as np

from ata.dsp import f32_to_pcm16, find_cut_point, pcm16_to_mono_f32, resample_to_16k


def test_resample_48k_to_16k_thirds_length():
    x = np.ones(48_000, dtype=np.float32)
    y = resample_to_16k(x, 48_000)
    assert y.size == 16_000


def test_resample_passthrough_16k():
    x = np.linspace(-1, 1, 16_000, dtype=np.float32)
    assert np.array_equal(resample_to_16k(x, 16_000), x)


def test_pcm_roundtrip_mono():
    tone = (0.5 * np.sin(np.linspace(0, 40, 16_000))).astype(np.float32)
    pcm = f32_to_pcm16(tone)
    back = pcm16_to_mono_f32(pcm, channels=1, sample_rate=16_000)
    assert back.size == 16_000
    assert np.max(np.abs(back - tone)) < 1e-3


def test_pcm_stereo_downmix_and_resample():
    frames = 48_000
    stereo = np.zeros((frames, 2), dtype="<i2")
    stereo[:, 0] = 1000
    stereo[:, 1] = 3000
    mono = pcm16_to_mono_f32(stereo.tobytes(), channels=2, sample_rate=48_000)
    assert mono.size == 16_000
    assert abs(float(np.mean(mono)) - (2000 / 32768.0)) < 1e-3


def test_find_cut_point_prefers_silence_near_target():
    sr = 16_000
    audio = (0.4 * np.random.default_rng(1).standard_normal(30 * sr)).astype(np.float32)
    # silêncio de 300 ms começando em 22 s
    gap0 = 22 * sr
    audio[gap0 : gap0 + int(0.3 * sr)] = 0.0
    cut = find_cut_point(audio, sr, target_seconds=24, min_seconds=8, max_seconds=28)
    assert abs(cut - gap0) < 0.5 * sr
