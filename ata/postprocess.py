"""Pós-processamento puro (sem I/O) dos segmentos de uma reunião.

`dedupe_echo`: com alto-falante aberto, o microfone recapta o áudio que sai da
faixa de sistema, e a mesma fala aparece duas vezes (SYSTEM e ME). Remove o ME
quando há uma fala SYSTEM quase idêntica no mesmo instante. Nunca altera o
banco: quem chama decide onde aplicar (resumo e exportação, não o `meeting.md`).
"""

from __future__ import annotations

import difflib
import re
from typing import Any

from .config import PostprocessConfig

_NON_WORD = re.compile(r"\W+")


def _norm(text: str) -> str:
    return _NON_WORD.sub(" ", text.lower()).strip()


def _is_same_speech(text: str, other: str, cfg: PostprocessConfig) -> bool:
    if difflib.SequenceMatcher(None, text, other).ratio() >= cfg.echo_min_similarity:
        return True
    # Cada canal corta a janela num ponto e erra palavras diferentes: o ratio cai mesmo
    # sendo a mesma fala. Nas falas longas, a sobreposição de palavras pega esses casos.
    words = set(text.split())
    if len(text.split()) < cfg.echo_min_words:
        return False
    return len(words & set(other.split())) / len(words) >= cfg.echo_min_word_overlap


def dedupe_echo(segments: list[dict[str, Any]], cfg: PostprocessConfig) -> list[dict[str, Any]]:
    """Retorna os segmentos sem os ME que são eco de um SYSTEM (mesma ordem)."""
    if not cfg.dedupe_echo:
        return segments
    system = sorted(
        ((float(s["start"]), _norm(str(s.get("text", "")))) for s in segments if s["source"] == "system"),
        key=lambda item: item[0],
    )
    if not system:
        return segments
    starts = [s[0] for s in system]
    window = cfg.echo_window_seconds
    out: list[dict[str, Any]] = []
    lo = 0
    for seg in segments:
        if seg["source"] not in ("me", "mic", "microphone"):
            out.append(seg)
            continue
        text = _norm(str(seg.get("text", "")))
        start = float(seg["start"])
        while lo < len(starts) and starts[lo] < start - window:
            lo += 1                      # `segments` vem ordenado por start
        is_echo = False
        i = lo
        while i < len(system) and starts[i] <= start + window:
            if _is_same_speech(text, system[i][1], cfg):
                is_echo = True
                break
            i += 1
        if not is_echo:
            out.append(seg)
    return out
