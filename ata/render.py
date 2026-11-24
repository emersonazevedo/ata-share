"""Renderização Markdown. Funções puras -> fáceis de testar.

`ata.md`  = documento de ata de verdade (estrutura própria).
`meeting.md` = ata + transcrição integral com timestamps.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

_BR_TZ = ZoneInfo("America/Sao_Paulo")


def _hms(seconds: float | int | None) -> str:
    s = int(seconds or 0)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _fmt_datetime_br(started_at: str | None) -> str:
    """Converte o `started_at` (ISO UTC, ex. 2026-09-02T12:00:00Z) para DD/MM/AAAA HH:MM
    no fuso horário de Brasília."""
    if not started_at:
        return "—"
    try:
        dt = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return started_at
    return dt.astimezone(_BR_TZ).strftime("%d/%m/%Y %H:%M")


def _bullets(items: list[Any]) -> str:
    if not items:
        return "- _nenhum item identificado_"
    return "\n".join(f"- {str(i).strip()}" for i in items if str(i).strip())


def _actions(items: list[dict[str, Any]]) -> str:
    if not items:
        return "- _nenhuma ação registrada_"
    rows = []
    for it in items:
        task = str(it.get("task", "")).strip()
        if not task:
            continue
        owner = (it.get("owner") or "—")
        deadline = (it.get("deadline") or "—")
        rows.append(f"- [ ] {task} — resp.: {owner} — prazo: {deadline}")
    return "\n".join(rows) or "- _nenhuma ação registrada_"


def _agenda(items: list[dict[str, Any]]) -> str:
    if not items:
        return "- _nenhum item de pauta identificado_"
    parts: list[str] = []
    for it in items:
        topic = str(it.get("topic", "")).strip()
        if not topic:
            continue
        discussion = str(it.get("discussion", "")).strip()
        parts.append(f"### {topic}\n\n{discussion or '_sem detalhes registrados_'}")
    return "\n\n".join(parts) or "- _nenhum item de pauta identificado_"


def render_ata(meeting: dict[str, Any], summary: dict[str, Any] | None, notes: str = "") -> str:
    s = summary or {}
    diag = s.get("_diagnostics") or {}
    participants = "Eu (microfone) e demais participantes (áudio do sistema)"
    banner: list[str] = []
    if diag.get("status") == "partial":
        banner = [f"> ⚠ **Ata parcial.** {diag.get('reason', 'Cobre apenas parte da reunião.')}", ""]
    elif diag.get("status") in ("failed", "empty"):
        banner = [f"> ⚠ **Ata não gerada.** {diag.get('reason', 'Resumo indisponível.')}", ""]
    if diag.get("model_fallback"):
        mf = diag["model_fallback"]
        banner += [f"> Gerada com `{mf.get('used')}` (o modelo configurado `{mf.get('requested')}` não está instalado).", ""]
    parts = [
        f"# Ata — {meeting['title']}",
        "",
        *banner,
        f"**Data e Hora:** {_fmt_datetime_br(meeting.get('started_at'))}  ",
        f"**Duração:** {_hms(meeting.get('duration_seconds'))}  ",
        f"**Idioma:** {meeting.get('language', 'pt')}  ",
        f"**Participantes:** {participants}",
        "",
        "## Resumo executivo",
        "",
        (s.get("summary") or "_Resumo não disponível._"),
        "",
        "## Itens da Agenda",
        "",
        _agenda(s.get("agenda_items", [])),
        "",
        "## Itens de Ação",
        "",
        _actions(s.get("action_items", [])),
        "",
        "## Próximos Passos",
        "",
        (s.get("next_steps") or "_nenhum próximo passo registrado_"),
        "",
        "## Outras Observações",
        "",
        _bullets(s.get("other_notes", [])),
        "",
    ]
    if notes.strip():
        parts += ["## Notas", "", notes.strip(), ""]
    return "\n".join(parts)


def render_meeting_md(
    meeting: dict[str, Any],
    summary: dict[str, Any] | None,
    segments: list[dict[str, Any]],
    notes: str = "",
) -> str:
    out = [render_ata(meeting, summary, notes), "## Transcrição", ""]
    if not segments:
        out.append("_Transcrição não disponível._")
        return "\n".join(out) + "\n"
    for seg in segments:
        label = "ME" if seg["source"] in ("me", "mic", "microphone") else "SYSTEM"
        out.append(f"**[{_hms(seg['start'])}] {label}**")
        out.append("")
        out.append(seg["text"].strip())
        out.append("")
    return "\n".join(out) + "\n"
