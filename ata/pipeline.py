"""Orquestra o ciclo de vida de uma reunião: captura -> ASR ao vivo ->
finalização (segundos) -> ata (map-reduce). Uma reunião ativa por vez.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import db as dbmod
from .asr import AsrWorker, transcribe_file
from .audio import CaptureController
from .config import Config
from .dashboard import rebuild_dashboard as rebuild_dashboard_file
from .db import Database
from .events import EventHub
from .obsidian import build_filename, export_meeting_note
from .postprocess import dedupe_echo
from .render import render_ata, render_meeting_md
from .summarize import OllamaClient, summarize_meeting
from .telemetry import StageTimes
from .xlsx_export import write_action_items_xlsx

log = logging.getLogger("ata.pipeline")

_MAX_RETRIES = 2


@dataclass
class _Active:
    meeting_id: str
    folder: Path
    asr: AsrWorker
    capture: CaptureController
    started_at: float
    title: str
    language: str
    limit_cancel: threading.Event = field(default_factory=threading.Event)   # encerra o guardrail
    limit_extend: threading.Event = field(default_factory=threading.Event)   # usuário pediu "continuar"

    def stop_guard(self) -> None:
        """Encerra a thread do guardrail (parada manual/shutdown), acordando qualquer espera."""
        self.limit_cancel.set()
        self.limit_extend.set()


class MeetingPipeline:
    def __init__(self, config: Config, database: Database, hub: EventHub) -> None:
        self.cfg = config
        self.db = database
        self.hub = hub
        self._active: _Active | None = None
        self._lock = threading.Lock()
        self._reprocessing: set[str] = set()   # ids com job de reprocesso/recuperação em curso

    @property
    def active_id(self) -> str | None:
        return self._active.meeting_id if self._active else None

    def _new_meeting_folder(self, meeting_id: str, title: str, started_iso: str) -> Path:
        """Pasta com o mesmo nome da nota no Obsidian ("AAAA-MM-DD - Título");
        em colisão acrescenta o prefixo do id, como a nota."""
        name = build_filename({"title": title, "started_at": started_iso})[:-3]
        base = self.cfg.meetings_path
        folder = base / name
        if folder.exists():
            folder = base / f"{name} ({meeting_id[:8]})"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    # ------------------------------------------------------------------
    def start(
        self,
        title: str,
        language: str,
        mic_device_index: int | None = None,
        system_device_index: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self._active is not None:
                raise RuntimeError("Já existe uma reunião em andamento.")
            language = language if language in ("pt", "en", "auto") else "pt"
            meeting_id = uuid.uuid4().hex
            started_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            title = title.strip() or time.strftime("Reunião %Y-%m-%d %H:%M")
            folder = self._new_meeting_folder(meeting_id, title, started_iso)
            self.db.create_meeting(meeting_id, title, language, started_iso, str(folder))

            asr = AsrWorker(meeting_id, self.cfg, self.db, self.hub, language=language)
            asr.start()
            capture = CaptureController(meeting_id, folder, self.cfg, asr)
            try:
                capture.start(mic_device_index, system_device_index)
            except Exception as error:
                self.db.set_status(meeting_id, dbmod.STATUS_FAILED)
                try:
                    capture.stop()          # rollback do que abriu parcialmente
                finally:
                    asr.finish(timeout=5)
                raise RuntimeError(f"Falha ao iniciar a captura de áudio: {error}") from error

            self._active = _Active(meeting_id, folder, asr, capture, time.time(), title, language)
            self.hub.publish(meeting_id, {"type": "status", "status": dbmod.STATUS_RECORDING})
            log.info("reunião %s iniciada", meeting_id)
            self._start_limit_guard(self._active)
            return self.db.get_meeting(meeting_id)  # type: ignore[return-value]

    def stop(self) -> dict[str, Any]:
        with self._lock:
            active = self._active
            if active is None:
                raise RuntimeError("Nenhuma reunião em andamento.")
            self._active = None

        active.stop_guard()   # encerra a thread do guardrail
        self.db.set_status(active.meeting_id, dbmod.STATUS_TRANSCRIBING)
        self.hub.publish(active.meeting_id, {"type": "status", "status": dbmod.STATUS_TRANSCRIBING})

        duration = int(round(time.time() - active.started_at))
        ended_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        threading.Thread(
            target=self._finalize, args=(active, ended_iso, duration),
            name=f"finalize-{active.meeting_id[:8]}", daemon=True,
        ).start()
        return {"id": active.meeting_id, "status": dbmod.STATUS_TRANSCRIBING}

    # ------------------------------------------------------------------
    # Guardrail: limite de duração da gravação
    def _start_limit_guard(self, active: _Active) -> None:
        guard = self.cfg.guardrails
        limit = int(guard.max_recording_minutes) * 60
        if limit <= 0:
            return
        warn = min(max(int(guard.warn_before_seconds), 0), limit)
        threading.Thread(
            target=self._limit_guard, args=(active, limit, warn),
            name=f"limit-{active.meeting_id[:8]}", daemon=True,
        ).start()

    def _limit_guard(self, active: _Active, limit: int, warn: int) -> None:
        """A cada `limit` s: avisa `warn` s antes do fim; se o usuário não pedir para
        continuar, encerra. Continuar renova por mais `limit` s e o ciclo se repete
        (aviso de novo, e encerra se ninguém responder) — nunca grava para sempre."""
        mid = active.meeting_id
        deadline = active.started_at + limit
        while True:
            # Event.wait -> True quando acordado (parada manual ou "continuar").
            if active.limit_extend.wait(max(0.0, deadline - warn - time.time())):
                if active.limit_cancel.is_set():
                    return
                active.limit_extend.clear()          # estendeu antes do aviso
                deadline += limit
                self.hub.publish(mid, {"type": "limit_extended", "minutes": limit // 60, "deadline": deadline})
                continue
            left = max(0, int(round(deadline - time.time())))
            self.hub.publish(mid, {"type": "limit_warning", "seconds_left": left, "deadline": deadline})
            self._beep(3)
            if active.limit_extend.wait(max(0.0, deadline - time.time())):
                if active.limit_cancel.is_set():
                    return
                active.limit_extend.clear()
                deadline += limit
                log.info("reunião %s: usuário continuou; novo limite em %d min", mid, limit // 60)
                self.hub.publish(mid, {"type": "limit_extended", "minutes": limit // 60, "deadline": deadline})
                continue
            break
        log.warning("reunião %s atingiu o limite de %d min; encerrando", mid, limit // 60)
        self.hub.publish(mid, {"type": "limit_reached", "minutes": limit // 60})
        self._beep(1, long=True)
        if self.active_id == mid:
            try:
                self.stop()
            except RuntimeError:  # parou manualmente no mesmo instante
                pass

    def cancel_auto_stop(self, meeting_id: str) -> None:
        """Usuário optou por continuar: renova o limite por mais um período (o aviso
        volta ao fim dele e a gravação é encerrada se ninguém responder)."""
        active = self._active
        if active is None or active.meeting_id != meeting_id:
            raise RuntimeError("Essa reunião não está ativa.")
        active.limit_extend.set()

    def _beep(self, times: int, long: bool = False) -> None:
        if not self.cfg.guardrails.beep:
            return

        def _run() -> None:
            try:
                import winsound  # só Windows
                for _ in range(times):
                    winsound.Beep(1000, 900 if long else 250)
                    if not long:
                        time.sleep(0.15)
            except Exception:  # noqa: BLE001 - bip é best-effort
                log.debug("bip indisponível", exc_info=True)

        threading.Thread(target=_run, name="ata-beep", daemon=True).start()

    # ------------------------------------------------------------------
    def _finalize(self, active: _Active, ended_iso: str, duration: int) -> None:
        mid = active.meeting_id
        times = StageTimes(mid, "finalização")
        try:
            active.capture.stop()   # fecha WAVs e flusha os segmenters -> últimas janelas na fila
            drained = active.asr.finish(timeout=self.cfg.asr.drain_timeout_seconds)
            times.mark("fila_asr")
            if not drained:
                self.hub.publish(mid, {"type": "warn",
                                       "message": "ASR não terminou a tempo; parte do fim pode faltar."})
            # transcrição já está disponível — libera antes da ata
            self.db.finish_recording(mid, ended_iso, duration, dbmod.STATUS_SUMMARIZING)
            self._write_docs(mid)
            self.hub.publish(mid, {"type": "transcript_ready",
                                   "segments": active.asr.segment_count,
                                   "dropped": active.asr.dropped})
            self.hub.publish(mid, {"type": "status", "status": dbmod.STATUS_SUMMARIZING})

            segments = self.db.get_segments(mid)
            client = OllamaClient(self.cfg.ollama)
            payload, diag = summarize_meeting(
                dedupe_echo(segments, self.cfg.postprocess), self.cfg.ollama, client=client,
                progress=lambda e: self.hub.publish(mid, e),
            )
            times.mark("resumo")
            self.db.save_summary(mid, payload, diag)
            self._write_docs(mid)
            self.db.set_status(mid, dbmod.STATUS_DONE)
            self.hub.publish(mid, {"type": "status", "status": dbmod.STATUS_DONE,
                                   "summary_status": diag.get("status"),
                                   "folder": str(active.folder)})
            if self.cfg.server.open_folder_on_done:
                self._reveal(active.folder)
            log.info("reunião %s finalizada (%d segmentos)", mid, len(segments))
            if self.cfg.obsidian.enabled and self.cfg.obsidian.auto_export:
                self.export_obsidian(mid)
            times.mark("exportacao")
            times.log()
        except Exception as error:  # pragma: no cover - robustez
            times.log()
            log.exception("finalização falhou para %s", mid)
            self.db.set_status(mid, dbmod.STATUS_FAILED)
            self.hub.publish(mid, {"type": "status", "status": dbmod.STATUS_FAILED, "message": str(error)})

    def _write_docs(self, meeting_id: str) -> tuple[Path, Path, Path]:
        meeting = self.db.get_meeting(meeting_id)
        assert meeting is not None
        segments = self.db.get_segments(meeting_id)
        summary = self.db.get_summary(meeting_id)
        notes = self.db.get_notes(meeting_id)
        folder = Path(meeting["folder"])
        ata_path = folder / "ata.md"
        meeting_path = folder / "meeting.md"
        actions_path = folder / "acoes.xlsx"
        ata_path.write_text(render_ata(meeting, summary, notes), encoding="utf-8")
        meeting_path.write_text(render_meeting_md(meeting, summary, segments, notes), encoding="utf-8")
        write_action_items_xlsx(actions_path, (summary or {}).get("action_items", []))
        return ata_path, meeting_path, actions_path

    def export(self, meeting_id: str) -> dict[str, str]:
        ata_path, meeting_path, actions_path = self._write_docs(meeting_id)
        return {"ata": str(ata_path), "meeting": str(meeting_path), "actions": str(actions_path),
                "folder": str(ata_path.parent)}

    def export_obsidian(self, meeting_id: str) -> dict[str, Any]:
        """Escreve (ou reescreve) a nota do Vault Obsidian para esta reunião.

        Chamado automaticamente por `_finalize`/`_reprocess_job` (gate de
        `auto_export` já verificado por quem chama ali) e pelo endpoint manual
        de retry (que ignora `auto_export`, só respeita `enabled`). Nunca lança:
        qualquer problema vira um resultado `failed`, registrado no banco, sem
        afetar `ata.md`/`meeting.md`/`acoes.xlsx` nem o status da reunião."""
        mid = meeting_id
        exported_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            meeting = self.db.get_meeting(mid)
            if meeting is None:
                raise RuntimeError("Reunião não encontrada.")
            summary = self.db.get_summary(mid)
            segments = dedupe_echo(self.db.get_segments(mid), self.cfg.postprocess)
            result = export_meeting_note(
                meeting, summary, segments, self.cfg.obsidian, self.cfg.obsidian_meetings_path,
            )
        except Exception as error:  # noqa: BLE001 - exportação Obsidian nunca derruba o pipeline
            log.exception("exportação Obsidian falhou para %s", mid)
            result_dict = {"status": "failed", "path": None, "error": str(error)}
        else:
            result_dict = {"status": result.status, "path": result.path, "error": result.error}

        if result_dict["status"] != "skipped":
            self.db.save_obsidian_export(
                mid, status=result_dict["status"], path=result_dict["path"],
                error=result_dict["error"], exported_at=exported_at,
            )
            self.hub.publish(mid, {"type": "obsidian_export", **result_dict})

        if result_dict["status"] == "ok" and self.cfg.obsidian.dashboard_enabled:
            self.rebuild_dashboard()
        return result_dict

    def rebuild_dashboard(self) -> dict[str, Any]:
        """Reconstrói Dashboard/Dashboard.md a partir de `Reunioes/` e
        `Projetos/` — view derivada e 100% determinística (nunca chama
        Ollama/LLM). Nunca lança: chamado tanto depois de um export
        bem-sucedido quanto pelo endpoint manual; falha aqui nunca desfaz a
        exportação da reunião nem seu status (o Dashboard não tem estado
        próprio no banco — é só reconstruível). `Projetos/` é só lida aqui —
        quem escreve lá é você (ou outra ferramenta), independente deste."""
        try:
            result = rebuild_dashboard_file(
                self.cfg.obsidian_meetings_path, self.cfg.obsidian_dashboard_path,
                self.cfg.obsidian_projetos_path,
                enabled=self.cfg.obsidian.dashboard_enabled,
            )
        except Exception as error:  # noqa: BLE001 - rebuild do Dashboard nunca derruba o pipeline
            log.exception("rebuild do Dashboard falhou")
            return {"status": "failed", "path": None, "error": str(error)}
        if result.status == "failed":
            log.warning("rebuild do Dashboard falhou: %s", result.error)
        return {"status": result.status, "path": result.path, "error": result.error,
                "meetings_count": result.meetings_count, "projects_count": result.projects_count}

    # ------------------------------------------------------------------
    def open_folder(self, meeting_id: str) -> str:
        meeting = self.db.get_meeting(meeting_id)
        if meeting is None:
            raise RuntimeError("Reunião não encontrada.")
        folder = Path(meeting["folder"])
        self._reveal(folder)
        return str(folder)

    def _reveal(self, folder: Path) -> None:
        """Abre a pasta da reunião no gerenciador de arquivos. Nunca derruba o fluxo."""
        try:
            resolved = folder.resolve()
            if self.cfg.meetings_path.resolve() not in resolved.parents or not resolved.exists():
                return
            if sys.platform.startswith("win"):
                os.startfile(resolved)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(resolved)])
            else:
                subprocess.Popen(["xdg-open", str(resolved)])
        except Exception:  # pragma: no cover
            log.debug("não consegui abrir a pasta %s", folder, exc_info=True)

    # ------------------------------------------------------------------
    def reprocess(self, meeting_id: str, beam_size: int) -> None:
        with self._lock:
            meeting = self.db.get_meeting(meeting_id)
            if meeting is None:
                raise RuntimeError("Reunião não encontrada.")
            if self._active is not None:
                raise RuntimeError("Há uma reunião em andamento; o modelo está ocupado.")
            if meeting_id in self._reprocessing:
                raise RuntimeError("Essa reunião já está sendo reprocessada.")
            self._reprocessing.add(meeting_id)
        # status síncrono: a UI abre o SSE sabendo que há processamento em curso
        self.db.set_status(meeting_id, dbmod.STATUS_TRANSCRIBING)
        threading.Thread(
            target=self._reprocess_job, args=(meeting, beam_size),
            name=f"reprocess-{meeting_id[:8]}", daemon=True,
        ).start()

    def _reprocess_job(self, meeting: dict[str, Any], beam_size: int, *, resume: bool = False) -> None:
        """`resume=True` (recuperação após queda): mantém os trechos já gravados e
        transcreve só o que falta de cada WAV, gravando cada trecho na hora — uma
        segunda queda não perde o que já foi feito. `resume=False` refaz tudo e
        troca os segmentos no fim (reprocesso pedido pelo usuário)."""
        mid = meeting["id"]
        times = StageTimes(mid, "recuperação" if resume else "reprocesso")
        folder = Path(meeting["folder"])
        language = meeting.get("language") or self.cfg.asr.language
        self._reprocessing.add(mid)
        try:
            self.db.set_status(mid, dbmod.STATUS_TRANSCRIBING)
            self.hub.publish(mid, {"type": "status", "status": dbmod.STATUS_TRANSCRIBING})
            rows: list[tuple[str, float, float, str]] = []
            last_end: dict[str, float] = {}
            if resume:
                for seg in self.db.get_segments(mid):
                    last_end[seg["source"]] = max(last_end.get(seg["source"], 0.0), float(seg["end"]))
                log.info("recuperação %s: retomando após %s", mid[:8],
                         {k: round(v) for k, v in last_end.items()} or "início")
            for name, source in (("system.wav", "system"), ("microphone.wav", "me")):
                wav = folder / name
                if not wav.is_file() or wav.stat().st_size <= 44:
                    continue
                kwargs: dict[str, Any] = {}
                if resume:
                    done_until = last_end.get(source, 0.0)

                    def save_row(row: tuple[str, float, float, str], done_until: float = done_until) -> None:
                        if row[2] > done_until + 0.35:        # já coberto pela transcrição ao vivo
                            self.db.add_segment(mid, *row)

                    kwargs = {"start_at": max(0.0, done_until - 1.0), "on_row": save_row}
                rows += transcribe_file(
                    self.cfg, str(wav), source, beam_size=beam_size, language=language,
                    progress=lambda f, s=source: self.hub.publish(
                        mid, {"type": "asr_progress", "track": s, "fraction": round(f, 3)}),
                    **kwargs,
                )
                times.mark(f"asr_{source}")
            rows.sort(key=lambda r: (r[1], r[0]))
            if self.db.get_meeting(mid) is None:      # excluída durante o reprocesso
                log.info("reunião %s sumiu durante o reprocesso; abortando", mid)
                return
            if not resume:
                self.db.replace_segments(mid, rows)

            self.db.set_status(mid, dbmod.STATUS_SUMMARIZING)
            self.hub.publish(mid, {"type": "status", "status": dbmod.STATUS_SUMMARIZING})
            payload, diag = summarize_meeting(
                dedupe_echo(self.db.get_segments(mid), self.cfg.postprocess), self.cfg.ollama,
                progress=lambda e: self.hub.publish(mid, e),
            )
            times.mark("resumo")
            self.db.save_summary(mid, payload, diag)
            self._write_docs(mid)
            self.db.set_status(mid, dbmod.STATUS_DONE)
            self.hub.publish(mid, {"type": "status", "status": dbmod.STATUS_DONE, "folder": str(folder)})
            if self.cfg.server.open_folder_on_done:
                self._reveal(folder)
            if self.cfg.obsidian.enabled and self.cfg.obsidian.auto_export:
                self.export_obsidian(mid)
            times.mark("exportacao")
            times.log()
        except Exception as error:  # pragma: no cover
            times.log()
            log.exception("reprocessamento falhou para %s", mid)
            if self.db.get_meeting(mid) is not None:
                self.db.set_status(mid, dbmod.STATUS_FAILED)
                self.hub.publish(mid, {"type": "status", "status": dbmod.STATUS_FAILED, "message": str(error)})
        finally:
            self._reprocessing.discard(mid)

    def delete(self, meeting_id: str) -> None:
        with self._lock:
            if self.active_id == meeting_id:
                raise RuntimeError("Pare a reunião antes de excluí-la.")
            if meeting_id in self._reprocessing:
                raise RuntimeError("Aguarde o reprocessamento terminar para excluir.")
        folder = self.db.delete_meeting(meeting_id)
        self.hub.clear(meeting_id)
        if folder:
            resolved = Path(folder).resolve()
            if self.cfg.meetings_path.resolve() in resolved.parents and resolved.exists():
                shutil.rmtree(resolved, ignore_errors=True)

    # ------------------------------------------------------------------
    def recover(self) -> None:
        """No boot: NÃO bloqueia a subida do servidor. Reuniões presas viram
        'failed' (sem WAV ou estouraram o teto) ou vão para uma fila em thread —
        uma por vez, respeitando o lock de inferência."""
        pending: list[dict[str, Any]] = []
        for meeting in self.db.stuck_meetings():
            mid = meeting["id"]
            folder = Path(meeting["folder"])
            has_wav = any(
                (folder / n).is_file() and (folder / n).stat().st_size > 44
                for n in ("system.wav", "microphone.wav")
            )
            if not has_wav or int(meeting.get("retries", 0)) >= _MAX_RETRIES:
                self.db.set_status(mid, dbmod.STATUS_FAILED)
                log.warning("reunião %s marcada como failed na recuperação", mid)
                continue
            self.db.bump_retries(mid)
            pending.append(meeting)

        if not pending:
            return

        def _run_queue() -> None:
            for meeting in pending:
                log.info("recuperando reunião %s a partir do WAV", meeting["id"])
                self._reprocess_job(meeting, self.cfg.asr.beam_size, resume=True)

        threading.Thread(target=_run_queue, name="ata-recover", daemon=True).start()

    def shutdown(self) -> None:
        """Parada coordenada: encerra a captura ativa antes de o processo morrer."""
        with self._lock:
            active = self._active
            self._active = None
        if active is None:
            return
        active.stop_guard()
        log.info("shutdown: encerrando captura da reunião %s", active.meeting_id)
        try:
            active.capture.stop()
            active.asr.finish(timeout=10)
            self.db.set_status(active.meeting_id, dbmod.STATUS_TRANSCRIBING)  # recover pega depois
        except Exception:  # pragma: no cover
            log.exception("erro no shutdown da captura")
