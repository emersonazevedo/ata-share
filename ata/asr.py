"""Worker de ASR: um único `large-v3-turbo` mantido quente na memória.

Puxa `Segment`s de uma fila, transcreve com beam baixo + VAD interno,
grava no banco e publica no EventHub. O modelo é carregado UMA vez no
processo (não a cada reunião, como o v1 fazia).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

from .config import AsrConfig, Config
from .db import Database
from .events import EventHub
from .segmenter import Segment
from .telemetry import free_memory_mb

log = logging.getLogger("ata.asr")

_model_lock = threading.Lock()
_infer_lock = threading.Lock()   # model.transcribe do faster-whisper não é thread-safe
_model: Any = None
_model_key: tuple | None = None

_PROMPT_CHARS = 200
_QUEUE_WARN_DEPTH = 6
_QUEUE_MAXSIZE = 600      # ~4h de backlog a 24s/janela; acima disso algo está muito errado
_WHISPER_SR = 16000
_OVERLAP_EPSILON = 0.35   # s — descarta segmento já coberto pela janela anterior (carry pad)


def get_model(config: Config):
    """WhisperModel singleton. Reaproveitado entre reuniões."""
    global _model, _model_key
    a = config.asr
    key = (a.model, a.device, a.compute_type, a.cpu_threads)
    with _model_lock:
        if _model is not None and _model_key == key:
            return _model
        from faster_whisper import WhisperModel

        t0 = time.perf_counter()
        log.info("carregando modelo ASR %s (%s/%s)…", a.model, a.device, a.compute_type)
        _model = WhisperModel(
            a.model,
            device=a.device,
            compute_type=a.compute_type,
            cpu_threads=a.cpu_threads,
            num_workers=1,
            download_root=str(config.model_cache_path),
        )
        _model_key = key
        log.info("modelo pronto em %.1fs", time.perf_counter() - t0)
        return _model


def transcribe_file(
    config: Config,
    wav_path: str,
    source: str,
    *,
    beam_size: int | None = None,
    language: str | None = None,
    progress: Any = None,
    start_at: float = 0.0,
    on_row: Any = None,
) -> list[tuple[str, float, float, str]]:
    """Transcrição offline de um WAV inteiro (reprocessamento / recuperação).

    Usa o mesmo modelo quente. `beam_size` maior = mais qualidade, mais lento.
    `language` sobrepõe o idioma global (o da reunião); "auto" deixa o modelo decidir.
    `start_at` (s) transcreve só a partir desse ponto do WAV (retomada após queda); os
    tempos devolvidos continuam absolutos. `on_row(row)` é chamado a cada trecho, para
    quem quiser gravar o progresso na hora.
    """
    model = get_model(config)
    a = config.asr
    lang = language if language is not None else a.language
    language = None if lang == "auto" else lang
    rows: list[tuple[str, float, float, str]] = []
    audio: Any = wav_path
    offset = 0.0
    if start_at > 0:
        from faster_whisper.audio import decode_audio

        decoded = decode_audio(wav_path, sampling_rate=_WHISPER_SR)
        offset = min(float(start_at), len(decoded) / _WHISPER_SR)
        audio = decoded[int(offset * _WHISPER_SR):]
        if len(audio) < _WHISPER_SR:        # menos de 1 s de áudio novo: nada a fazer
            return rows
    with _infer_lock:
        segments, info = model.transcribe(
            audio,
            language=language,
            beam_size=beam_size or a.beam_size,
            vad_filter=True,
            vad_parameters={
                "min_silence_duration_ms": a.vad_min_silence_ms,
                "speech_pad_ms": a.vad_speech_pad_ms,
            },
            condition_on_previous_text=False,
            word_timestamps=False,
        )
        duration = float(getattr(info, "duration", 0.0) or 0.0)
        for s in segments:
            text = str(s.text).strip()
            if not text:
                continue
            row = (source, offset + float(s.start), offset + float(s.end), text)
            rows.append(row)
            if on_row:
                on_row(row)
            if progress and duration:
                progress(min(1.0, float(s.end) / duration))
    return rows


def warm_up(config: Config) -> None:
    """Chamado no boot para pagar o custo de load antes da primeira reunião."""
    try:
        get_model(config)
    except Exception:  # pragma: no cover - ambiente sem modelo
        log.exception("falha ao pré-carregar o modelo ASR")


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


class AsrWorker:
    def __init__(
        self, meeting_id: str, config: Config, db: Database, hub: EventHub,
        *, language: str | None = None,
    ) -> None:
        self.meeting_id = meeting_id
        self.cfg: AsrConfig = config.asr
        self._config = config
        self._db = db
        self._hub = hub
        self._language = language if language is not None else self.cfg.language
        self._queue: "queue.Queue[Segment | None]" = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._prompts: dict[str, str] = {"me": "", "system": ""}
        self._last_end: dict[str, float] = {"me": 0.0, "system": 0.0}
        self._last_text: dict[str, str] = {"me": "", "system": ""}
        self._thread = threading.Thread(target=self._run, name=f"asr-{meeting_id[:8]}", daemon=True)
        self._done = threading.Event()      # worker terminou (drenou ou abortou)
        self._abort = threading.Event()     # descarta o backlog e sai
        self._alive = threading.Event()     # worker rodando e apto a receber
        self.segment_count = 0
        self.dropped = 0
        self._latest_end = 0.0              # fim da janela mais recente enviada à fila (áudio gravado)
        self._windows_done = 0
        self._last_lag_warn = 0.0           # perf_counter do último aviso de atraso

    def start(self) -> None:
        self._alive.set()
        self._thread.start()

    def submit(self, segment: Segment) -> None:
        if not self._alive.is_set() or self._abort.is_set():
            self.dropped += 1          # worker morto/abortado: não acumula memória à toa
            return
        self._latest_end = max(self._latest_end, segment.end)
        try:
            self._queue.put_nowait(segment)
        except queue.Full:             # pragma: no cover - só em cenário patológico
            self.dropped += 1
            self._hub.publish(self.meeting_id, {
                "type": "warn", "message": "Fila de ASR cheia; uma janela foi descartada."})
            return
        depth = self._queue.qsize()
        if depth and depth % _QUEUE_WARN_DEPTH == 0:
            self._hub.publish(self.meeting_id, {
                "type": "warn",
                "message": f"ASR {depth} janelas atrás do tempo real; vai drenar após o Stop.",
            })

    def finish(self, timeout: float) -> bool:
        """Sinaliza fim de entrada e espera a fila drenar.

        Se estourar o tempo, ABORTA o worker (descarta o resto) para que ele não
        continue gravando segmentos depois que a ata já foi montada.
        """
        try:
            self._queue.put_nowait(None)
        except queue.Full:  # pragma: no cover
            self._abort.set()
        if self._done.wait(timeout=timeout):
            return True
        log.warning("ASR não drenou em %ss; abortando o backlog de %s", timeout, self.meeting_id)
        self._abort.set()
        self._done.wait(timeout=30)
        return False

    # ------------------------------------------------------------------
    def _run(self) -> None:
        try:
            self._model = get_model(self._config)
        except Exception as error:  # pragma: no cover
            log.exception("ASR não pôde carregar o modelo")
            self._hub.publish(self.meeting_id, {"type": "error", "message": f"ASR indisponível: {error}"})
            self._alive.clear()
            self._done.set()
            return

        language = None if self._language == "auto" else self._language
        vad_params = {
            "min_silence_duration_ms": self.cfg.vad_min_silence_ms,
            "speech_pad_ms": self.cfg.vad_speech_pad_ms,
        }
        while not self._abort.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                self._transcribe(item, language, vad_params)
            except Exception as error:  # pragma: no cover - robustez
                log.exception("falha ao transcrever janela")
                self._hub.publish(self.meeting_id, {"type": "error", "message": str(error)})
        self._alive.clear()
        self._done.set()
        self._hub.publish(self.meeting_id, {"type": "asr_drained", "dropped": self.dropped})

    def _transcribe(self, seg: Segment, language: str | None, vad_params: dict) -> None:
        started = time.perf_counter()
        with _infer_lock:
            segments, _info = self._model.transcribe(
                seg.audio,
                language=language,
                beam_size=self.cfg.beam_size,
                vad_filter=True,
                vad_parameters=vad_params,
                condition_on_previous_text=False,
                initial_prompt=self._prompts.get(seg.source) or None,
                word_timestamps=False,
            )
            raw = list(segments)

        produced: list[dict[str, Any]] = []
        for s in raw:
            text = str(s.text).strip()
            if not text:
                continue
            start = seg.start + max(0.0, float(s.start))
            end = seg.start + min(seg.duration, float(s.end))
            # o carry pad de ~150ms faz a janela anterior e esta se sobreporem;
            # descarta o que já foi confirmado para não duplicar na emenda.
            if end <= self._last_end[seg.source] + _OVERLAP_EPSILON:
                continue
            if start < self._last_end[seg.source] and _norm(text) == self._last_text[seg.source]:
                continue
            self._db.add_segment(self.meeting_id, seg.source, start, end, text)
            self._last_end[seg.source] = max(self._last_end[seg.source], end)
            self._last_text[seg.source] = _norm(text)
            produced.append({"type": "segment", "source": seg.source,
                             "start": round(start, 3), "end": round(end, 3), "text": text})

        if produced:
            joined = " ".join(p["text"] for p in produced)
            self._prompts[seg.source] = (self._prompts.get(seg.source, "") + " " + joined).strip()[-_PROMPT_CHARS:]
            self.segment_count += len(produced)
            for event in produced:
                self._hub.publish(self.meeting_id, event)

        rtf = (time.perf_counter() - started) / max(seg.duration, 0.001)
        lag = max(0.0, self._latest_end - seg.end)
        self._windows_done += 1
        self._log_lag(lag, rtf)
        self._hub.publish(self.meeting_id, {
            "type": "asr_progress",
            "through": round(seg.end, 1),
            "rtf": round(rtf, 2),
            "queue": self._queue.qsize(),
            "lag": round(lag, 1),
        })

    def _log_lag(self, lag: float, rtf: float) -> None:
        """Uma linha de log a cada 10 janelas (atraso, rtf, fila, RAM livre) e um
        WARNING + evento `warn` (no máx. 1 a cada 5 min) quando o ASR passa do
        limite de atraso configurado."""
        if self._windows_done % 10 == 0:
            log.info("asr %s: atraso=%.0fs rtf=%.2f fila=%d ram_livre=%sMB",
                     self.meeting_id[:8], lag, rtf, self._queue.qsize(), free_memory_mb())
        limit = self._config.postprocess.asr_lag_warn_seconds
        now = time.perf_counter()
        if limit and lag >= limit and now - self._last_lag_warn >= 300:
            self._last_lag_warn = now
            log.warning("asr %s: %.0f min atrás do áudio gravado (rtf=%.2f); vai drenar após o Stop",
                        self.meeting_id[:8], lag / 60, rtf)
            self._hub.publish(self.meeting_id, {
                "type": "warn",
                "message": f"Transcrição ao vivo {lag / 60:.0f} min atrás do áudio; o resto sai após o Stop.",
            })
