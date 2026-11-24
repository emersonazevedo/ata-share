"""Acumula áudio mono 16 kHz de uma fonte e fatia em janelas rolantes
cortadas no silêncio. Cada janela vira um `Segment` para o worker de ASR.

Sem threads aqui: é só a lógica de fatiamento, para poder testar sozinha.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import SegmenterConfig
from .dsp import TARGET_SR, silence_cut


@dataclass
class Segment:
    source: str          # 'me' | 'system'
    start: float          # segundos desde o início da reunião
    audio: np.ndarray     # float32 mono 16 kHz

    @property
    def duration(self) -> float:
        return self.audio.size / TARGET_SR

    @property
    def end(self) -> float:
        return self.start + self.duration


class Segmenter:
    def __init__(self, source: str, config: SegmenterConfig) -> None:
        self.source = source
        self.cfg = config
        self._buf = np.zeros(0, dtype=np.float32)
        self._buf_start = 0.0   # tempo (s) da amostra _buf[0]

    def push(self, mono_16k: np.ndarray) -> list[Segment]:
        """Adiciona áudio; devolve zero ou mais janelas prontas."""
        if mono_16k.size:
            self._buf = np.concatenate((self._buf, mono_16k.astype(np.float32, copy=False)))
        out: list[Segment] = []
        target = int(self.cfg.target_seconds * TARGET_SR)
        hard = int(self.cfg.max_seconds * TARGET_SR)
        pad = int(self.cfg.carry_pad_seconds * TARGET_SR)
        while self._buf.size >= target:
            window = self._buf[:hard]
            cut, clean = silence_cut(
                window, TARGET_SR,
                self.cfg.target_seconds, self.cfg.min_seconds, self.cfg.max_seconds,
            )
            if clean:
                cut = min(max(cut, int(self.cfg.min_seconds * TARGET_SR)), self._buf.size)
            elif self._buf.size < hard:
                break                       # espera um silêncio melhor, até max_seconds
            else:
                cut = min(hard, self._buf.size)   # sem silêncio até max -> corta na marra
            out.append(Segment(self.source, round(self._buf_start, 3), self._buf[:cut].copy()))
            keep_from = max(0, cut - pad)
            self._buf_start += keep_from / TARGET_SR
            self._buf = self._buf[keep_from:]
        return out

    def flush(self) -> list[Segment]:
        """No Stop: entrega o resto. Só descarta se for curtíssimo E silencioso,
        para não perder uma frase curta dita bem na hora do Stop."""
        buf = self._buf
        self._buf = np.zeros(0, dtype=np.float32)
        if buf.size < int(self.cfg.flush_min_seconds * TARGET_SR):
            if buf.size == 0 or float(np.sqrt(np.mean(buf * buf))) < 1e-3:
                return []
        return [Segment(self.source, round(self._buf_start, 3), buf.copy())]
