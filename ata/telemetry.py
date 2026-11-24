"""Telemetria leve: memória livre e tempo por etapa, só para o log.

Existe para que a próxima reunião longa deixe a causa de um atraso ou de uma
queda registrada, em vez de suposição. Nunca levanta exceção.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import time

log = logging.getLogger("ata.telemetry")


def free_memory_mb() -> int | None:
    """RAM física livre em MB (Windows). None se não der para medir."""
    if sys.platform != "win32":
        return None
    try:
        class _Status(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        status = _Status()
        status.dwLength = ctypes.sizeof(_Status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
            return None
        return int(status.ullAvailPhys // (1024 * 1024))
    except Exception:  # noqa: BLE001 - telemetria nunca derruba nada
        return None


class StageTimes:
    """Cronometra etapas em sequência e escreve uma linha de resumo no log."""

    def __init__(self, meeting_id: str, label: str) -> None:
        self._id = meeting_id[:8]
        self._label = label
        self._t0 = self._last = time.perf_counter()
        self._stages: list[tuple[str, float]] = []

    def mark(self, stage: str) -> None:
        now = time.perf_counter()
        self._stages.append((stage, now - self._last))
        self._last = now

    def log(self) -> None:
        total = time.perf_counter() - self._t0
        parts = ", ".join(f"{name}={secs:.0f}s" for name, secs in self._stages)
        log.info("etapas %s %s: %s, total=%.0fs, ram_livre=%sMB",
                 self._label, self._id, parts, total, free_memory_mb())
