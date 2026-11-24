"""Captura WASAPI local via PyAudioWPatch (puro-Python, sem compilar nada).

Duas entradas: SYSTEM (loopback da saída padrão) e MIC (entrada padrão).
Cada fonte: callback -> fila de bytes -> pump thread -> mono 16 kHz ->
grava WAV + alimenta o Segmenter -> envia janelas ao AsrWorker.
"""

from __future__ import annotations

import logging
import queue
import threading
import wave
from pathlib import Path

import numpy as np

from .asr import AsrWorker
from .config import Config
from .dsp import TARGET_SR, f32_to_pcm16, pcm16_to_mono_f32
from .segmenter import Segmenter

log = logging.getLogger("ata.audio")

_FRAMES_PER_BUFFER = 1600  # ~0,1 s a 16 kHz-equivalente; no device costuma ser 48 kHz


class _SourceStream:
    def __init__(self, name: str, source: str, wav_path: Path, config: Config, asr: AsrWorker) -> None:
        self.name = name
        self.source = source
        self._raw: "queue.Queue[bytes | None]" = queue.Queue()
        self._channels = 1
        self._rate = TARGET_SR
        self._wav = wave.open(str(wav_path), "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(TARGET_SR)
        self._seg = Segmenter(source, config.segmenter)
        self._asr = asr
        self._pump = threading.Thread(target=self._run_pump, name=f"pump-{source}", daemon=True)
        self._stopping = threading.Event()
        self.stream = None  # set by CaptureController

    def configure(self, channels: int, rate: int) -> None:
        self._channels = max(1, channels)
        self._rate = int(rate)

    def callback(self, in_data, frame_count, time_info, status):  # noqa: ANN001 - PyAudio API
        if status:
            log.debug("%s status=%s", self.name, status)
        self._raw.put(bytes(in_data))
        return (None, 0)  # (None, paContinue)

    def start_pump(self) -> None:
        self._pump.start()

    def _run_pump(self) -> None:
        while True:
            chunk = self._raw.get()
            if chunk is None:
                break
            mono = pcm16_to_mono_f32(chunk, self._channels, self._rate)
            if mono.size == 0:
                continue
            self._wav.writeframes(f32_to_pcm16(mono))
            for segment in self._seg.push(mono):
                self._asr.submit(segment)
        for segment in self._seg.flush():
            self._asr.submit(segment)
        try:
            self._wav.close()
        except Exception:  # pragma: no cover
            log.exception("erro ao fechar WAV %s", self.name)

    def signal_stop(self) -> None:
        self._raw.put(None)

    def join(self, timeout: float = 30.0) -> None:
        self._pump.join(timeout=timeout)
        if self._pump.is_alive():  # pragma: no cover
            log.warning("pump %s não encerrou em %ss", self.name, timeout)

    def abort(self) -> None:
        """Fecha stream e WAV sem depender do pump (usado em falha de start)."""
        try:
            if self.stream is not None:
                self.stream.stop_stream()
                self.stream.close()
        except Exception:  # pragma: no cover
            pass
        try:
            self._wav.close()
        except Exception:  # pragma: no cover
            pass


class CaptureController:
    def __init__(self, meeting_id: str, folder: Path, config: Config, asr: AsrWorker) -> None:
        self.meeting_id = meeting_id
        self._folder = folder
        self._config = config
        self._asr = asr
        self._pa = None
        self._sources: list[_SourceStream] = []
        self._started = False

    def start(self, mic_device_index: int | None = None, system_device_index: int | None = None) -> None:
        import pyaudiowpatch as pyaudio  # import tardio: servidor sobe mesmo sem o pacote

        self._pa = pyaudio.PyAudio()
        wasapi = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)

        if system_device_index is not None:
            speakers = self._pa.get_device_info_by_index(system_device_index)
        else:
            speakers = self._pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
            if not speakers.get("isLoopbackDevice", False):
                for loopback in self._pa.get_loopback_device_info_generator():
                    if speakers["name"] in loopback["name"]:
                        speakers = loopback
                        break
        if mic_device_index is not None:
            mic = self._pa.get_device_info_by_index(mic_device_index)
        else:
            mic = self._pa.get_device_info_by_index(wasapi["defaultInputDevice"])

        self._sources = [
            _SourceStream("SYSTEM", "system", self._folder / "system.wav", self._config, self._asr),
            _SourceStream("MIC", "me", self._folder / "microphone.wav", self._config, self._asr),
        ]
        devices = [speakers, mic]
        try:
            for src, dev in zip(self._sources, devices):
                channels = int(dev.get("maxInputChannels") or 1)
                rate = int(dev.get("defaultSampleRate") or 48_000)
                src.configure(channels, rate)
                src.stream = self._pa.open(
                    format=pyaudio.paInt16,
                    channels=channels,
                    rate=rate,
                    frames_per_buffer=_FRAMES_PER_BUFFER,
                    input=True,
                    input_device_index=int(dev["index"]),
                    stream_callback=src.callback,
                )
                src.start_pump()
                src.stream.start_stream()
                log.info("captura %s: %s (%d ch @ %d Hz)", src.name, dev["name"], channels, rate)
        except Exception:
            self._cleanup(via_pump=False)   # rollback do que abriu parcialmente
            raise
        self._started = True

    def _cleanup(self, *, via_pump: bool) -> None:
        for src in self._sources:
            if via_pump:
                try:
                    if src.stream is not None:
                        src.stream.stop_stream()
                        src.stream.close()
                except Exception:  # pragma: no cover
                    log.exception("erro ao parar stream %s", src.name)
            else:
                src.abort()
        if via_pump:
            for src in self._sources:
                src.signal_stop()
            for src in self._sources:
                src.join()
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:  # pragma: no cover
                pass
            self._pa = None
        self._sources = []

    def stop(self) -> None:
        if not self._started:
            self._cleanup(via_pump=False)
            self._started = False
            return
        self._cleanup(via_pump=True)
        self._started = False


def list_devices() -> dict:
    """Best-effort para a UI. Nunca levanta."""
    try:
        import pyaudiowpatch as pyaudio
    except Exception as error:  # pragma: no cover
        return {"available": False, "error": str(error)}
    pa = pyaudio.PyAudio()
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        speakers = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        mic = pa.get_device_info_by_index(wasapi["defaultInputDevice"])
        return {"available": True, "system": speakers.get("name"), "mic": mic.get("name")}
    except Exception as error:  # pragma: no cover
        return {"available": False, "error": str(error)}
    finally:
        pa.terminate()


def list_devices_full() -> dict:
    """Lista completa de dispositivos WASAPI para a UI escolher antes de gravar.

    Nunca levanta — devolve {"available": False, "error": ...} em falha.
    O default é recalculado a cada chamada (reflete o que o Windows tem
    como padrão agora, útil se o usuário trocou de fone/dispositivo).
    """
    try:
        import pyaudiowpatch as pyaudio
    except Exception as error:  # pragma: no cover
        return {"available": False, "error": str(error)}
    pa = pyaudio.PyAudio()
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_mic_index = wasapi["defaultInputDevice"]
        default_speakers = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        default_system_name = default_speakers.get("name")
        if not default_speakers.get("isLoopbackDevice", False):
            for loopback in pa.get_loopback_device_info_generator():
                if default_speakers["name"] in loopback["name"]:
                    default_system_name = loopback.get("name")
                    break

        mics = []
        systems = []
        for i in range(pa.get_device_count()):
            dev = pa.get_device_info_by_index(i)
            if dev.get("hostApi") != wasapi["index"] or int(dev.get("maxInputChannels") or 0) <= 0:
                continue
            entry = {"index": dev["index"], "name": dev.get("name")}
            if dev.get("isLoopbackDevice", False):
                entry["is_default"] = dev.get("name") == default_system_name
                systems.append(entry)
            else:
                entry["is_default"] = dev["index"] == default_mic_index
                mics.append(entry)
        return {"available": True, "mics": mics, "systems": systems}
    except Exception as error:  # pragma: no cover
        return {"available": False, "error": str(error)}
    finally:
        pa.terminate()
