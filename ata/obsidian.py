"""Exportação da ata para uma nota Markdown no Vault Obsidian.

Responsabilidade isolada: só escrita de arquivo no filesystem, sem depender de
plugin/API do Obsidian. Segue o template oficial do vault
(`Templates/Template - Reuniao.md`) — este módulo não inventa um segundo
formato, só preenche os campos desse template com o que já foi processado
(meeting + summary + segments), deixando em branco o que não há evidência.

Funções puras (fáceis de testar sem filesystem): `sanitize_filename`,
`build_filename`, `render_obsidian_note`. `export_meeting_note` é o único ponto
com I/O — grava com escrita atômica (tmp + os.replace) e nunca lança exceção
para quem chama: qualquer falha vira `ObsidianExportResult(status="failed")`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ObsidianConfig
from .render import _BR_TZ, _hms
from .summarize import clean_cloud_providers, clean_owner, is_channel_label, item_to_text

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_CCTRANSCRIPT_ID_RE = re.compile(r"^cctranscript_id:\s*(\S+)\s*$", re.MULTILINE)

_TEMPLATE_TIPO = "reuniao"
_TEMPLATE_TAGS = ("reuniao",)
SOURCE_AUDIO = "audio"
SOURCE_TEXT_IMPORT = "importacao-texto"


@dataclass
class ObsidianExportResult:
    status: str  # "ok" | "skipped" | "failed"
    path: str | None = None
    error: str | None = None


# --- helpers de data ---------------------------------------------------------

def _parse_started_at(started_at: str | None) -> datetime | None:
    if not started_at:
        return None
    try:
        return datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _date_iso_br(started_at: str | None) -> str:
    dt = _parse_started_at(started_at)
    if dt is None:
        return ""
    return dt.astimezone(_BR_TZ).strftime("%Y-%m-%d")


def _date_br(started_at: str | None) -> str:
    dt = _parse_started_at(started_at)
    if dt is None:
        return "—"
    return dt.astimezone(_BR_TZ).strftime("%d/%m/%Y")


# --- nome de arquivo -----------------------------------------------------------

def sanitize_filename(name: str) -> str:
    """Remove caracteres inválidos no Windows e apara espaços/pontos finais."""
    cleaned = _INVALID_FILENAME_CHARS.sub("-", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or "Reuniao"


def build_filename(meeting: dict[str, Any]) -> str:
    date = _date_iso_br(meeting.get("started_at")) or "sem-data"
    title = sanitize_filename(str(meeting.get("title") or "Reunião"))
    return sanitize_filename(f"{date} - {title}") + ".md"


def _with_disambiguator(filename: str, meeting_id: str) -> str:
    stem = filename[:-3] if filename.endswith(".md") else filename
    return f"{stem} ({meeting_id[:8]}).md"


# --- frontmatter e corpo -------------------------------------------------------

def _yaml_scalar(value: str) -> str:
    """Sempre aspas duplas quando não-vazio (evita qualquer problema com ':',
    '#', '[[...]]' etc. no valor); vazio fica em branco, igual ao template."""
    value = (value or "").strip()
    if not value:
        return ""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _wikilink_projeto(meeting: dict[str, Any]) -> str:
    ticket = str(meeting.get("ticket") or "").strip()
    project_name = str(meeting.get("project_name") or "").strip()
    if ticket and project_name:
        return f"[[TKT-{ticket} - {project_name}]]"
    return ""


def _resolve_cloud(meeting: dict[str, Any], summary: dict[str, Any]) -> str:
    """metadata.cloud informado pelo usuário tem prioridade; só cai para
    cloud_providers extraído do summary quando o usuário não informou nada."""
    user_cloud = str(meeting.get("cloud") or "").strip()
    if user_cloud:
        return user_cloud
    providers = clean_cloud_providers([t for p in (summary or {}).get("cloud_providers", []) if (t := item_to_text(p))])
    return ", ".join(providers)


def _fonte_confianca(meeting: dict[str, Any]) -> tuple[str, str]:
    """Importação de texto (sem falantes/timestamps) = confiança baixa; qualquer
    outro caso é áudio, sem campo de confiança."""
    if str(meeting.get("fonte") or "").strip() == SOURCE_TEXT_IMPORT:
        return SOURCE_TEXT_IMPORT, "baixa"
    return SOURCE_AUDIO, ""


def _frontmatter(meeting: dict[str, Any], summary: dict[str, Any], cfg: ObsidianConfig) -> str:
    projeto = _wikilink_projeto(meeting)
    ticket = str(meeting.get("ticket") or "").strip()
    cloud = _resolve_cloud(meeting, summary)
    fonte, confianca = _fonte_confianca(meeting)
    lines = [
        "---",
        f'data: {_yaml_scalar(_date_iso_br(meeting.get("started_at")))}',
        f"projeto: {_yaml_scalar(projeto)}",
        f"ticket: {_yaml_scalar(ticket)}",
        f"cloud: {_yaml_scalar(cloud)}",
        f"status: {_yaml_scalar(cfg.default_status)}",
        f"tipo: {_yaml_scalar(_TEMPLATE_TIPO)}",
        f"fonte: {fonte}",
        f"confianca: {confianca}",
        "tags:",
        *[f"  - {tag}" for tag in _TEMPLATE_TAGS],
        f"cctranscript_id: {meeting['id']}",
        "---",
    ]
    return "\n".join(lines)


def _bullets(items: list[Any], empty_text: str) -> str:
    cleaned = [t for i in (items or []) if (t := item_to_text(i))]
    if not cleaned:
        return f"- {empty_text}"
    return "\n".join(f"- {item}" for item in cleaned)


def _pauta(agenda_items: list[dict[str, Any]]) -> str:
    topics = [item_to_text((it or {}).get("topic") if isinstance(it, dict) else it) for it in (agenda_items or [])]
    return _bullets([t for t in topics if t], "Nenhum item de pauta identificado.")


def _acoes(action_items: list[dict[str, Any]], next_steps: str) -> str:
    rows = []
    for item in action_items or []:
        task = item_to_text((item or {}).get("task"))
        if not task:
            continue
        owner = clean_owner((item or {}).get("owner"))
        deadline = str((item or {}).get("deadline") or "").strip()
        line = f"- [ ] {task} — Responsável: {owner}"
        if deadline:
            line += f" — Prazo: {deadline}"
        rows.append(line)
    body = "\n".join(rows) if rows else "- Nenhuma ação registrada."
    if next_steps and next_steps.strip():
        body += f"\n\n{next_steps.strip()}"
    return body


def _technical_points_block(technical_points: dict[str, Any]) -> str:
    tp = technical_points or {}

    def sub(key: str) -> str:
        items = [t for i in (tp.get(key) or []) if (t := item_to_text(i))]
        if not items:
            return "> - Nenhum identificado."
        return "\n".join(f"> - {i}" for i in items)

    return "\n".join([
        "> [!info]- Abrir detalhes técnicos",
        "> **Serviços envolvidos:**",
        sub("services"),
        ">",
        "> **Arquitetura:**",
        sub("architecture"),
        ">",
        "> **Dependências:**",
        sub("dependencies"),
    ])


def _transcript_callout(segments: list[dict[str, Any]]) -> str:
    if not segments:
        return "> [!note]- Abrir transcript completo\n> _Transcrição não disponível._"
    lines = ["> [!note]- Abrir transcript completo"]
    for i, seg in enumerate(segments):
        label = "ME" if seg["source"] in ("me", "mic", "microphone") else "SYSTEM"
        lines.append(f"> **[{_hms(seg['start'])}] {label}**")
        lines.append(">")
        text = str(seg.get("text", "")).strip()
        for line in (text.splitlines() or [""]):
            lines.append(f"> {line}" if line else ">")
        if i < len(segments) - 1:
            lines.append(">")
    return "\n".join(lines)


def render_obsidian_note(
    meeting: dict[str, Any],
    summary: dict[str, Any] | None,
    segments: list[dict[str, Any]],
    cfg: ObsidianConfig,
) -> str:
    """Monta a nota completa seguindo a ordem/seções do template oficial de
    Reunião. Não inventa dados: campo sem evidência fica vazio."""
    s = summary or {}
    ticket = str(meeting.get("ticket") or "").strip()
    project_name = str(meeting.get("project_name") or "").strip()
    client = str(meeting.get("client") or "").strip()
    if project_name and client:
        projeto_cliente = f"{project_name} — {client}"
    else:
        projeto_cliente = project_name or client
    participantes = ", ".join(
        t for p in s.get("participants", []) if (t := item_to_text(p)) and not is_channel_label(t)
    )
    cloud = _resolve_cloud(meeting, s)

    parts = [
        _frontmatter(meeting, s, cfg),
        "",
        f"# {meeting.get('title', '')}",
        "",
        f"**📅 Data:** {_date_br(meeting.get('started_at'))}",
        f"**🎫 Ticket:** {ticket}",
        f"**🏢 Projeto / Cliente:** {projeto_cliente}",
        f"**👥 Participantes:** {participantes}",
        f"**☁️ Cloud:** {cloud}",
        f"**📌 Status:** {cfg.default_status}",
        "",
        "---",
        "",
        "## 🎯 Objetivo",
        "",
        "",
        "## 📋 Pauta",
        "",
        _pauta(s.get("agenda_items", [])),
        "",
        "## 📝 Resumo da Reunião",
        "",
        (s.get("summary") or "").strip(),
        "",
        "## ✅ Decisões",
        "",
        _bullets(s.get("decisions", []), "Nenhuma decisão identificada."),
        "",
        "## 🚨 Riscos / Bloqueios",
        "",
        _bullets(s.get("risks", []), "Nenhum identificado."),
        "",
        "## 🔧 Pontos Técnicos",
        "",
        _technical_points_block(s.get("technical_points", {})),
        "",
        "## 🚀 Ações / Próximos Passos",
        "",
        _acoes(s.get("action_items", []), s.get("next_steps", "")),
        "",
        "## 📎 Links / Evidências",
        "",
        f"- **Ticket:** {ticket}",
        "- **Jira / chamado:**",
        "- **Documentação:**",
        "- **Arquitetura / Diagrama:**",
        "- **Repositório:**",
        "",
        "## 🎙️ Transcript da Reunião",
        "",
        _transcript_callout(segments),
        "",
    ]
    return "\n".join(parts)


# --- I/O -----------------------------------------------------------------

def _read_existing_cctranscript_id(path: Path) -> str | None:
    try:
        head = path.read_text(encoding="utf-8")[:4096]
    except OSError:
        return None
    match = _CCTRANSCRIPT_ID_RE.search(head)
    return match.group(1) if match else None


def export_meeting_note(
    meeting: dict[str, Any],
    summary: dict[str, Any] | None,
    segments: list[dict[str, Any]],
    cfg: ObsidianConfig,
    meetings_path: Path | None,
) -> ObsidianExportResult:
    """`meetings_path` já resolvido (ver `Config.obsidian_meetings_path`) — `None`
    quando `vault_path` não existe/é inválido. `cfg.enabled=False` é a única
    condição que resulta em `skipped`; qualquer outro problema (vault ausente,
    permissão, disco) é `failed` — nunca mascarado como `skipped`."""
    if not cfg.enabled:
        return ObsidianExportResult(status="skipped")
    if meetings_path is None:
        return ObsidianExportResult(
            status="failed",
            error=f"vault_path não configurado ou inexistente: {cfg.vault_path!r}",
        )
    try:
        meetings_path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        return ObsidianExportResult(status="failed", error=f"não foi possível preparar {meetings_path}: {error}")

    filename = build_filename(meeting)
    target = meetings_path / filename
    meeting_id = str(meeting["id"])
    if target.exists():
        existing_id = _read_existing_cctranscript_id(target)
        if existing_id != meeting_id:   # None (sem marcador) ou de outra reunião -> colisão
            target = meetings_path / _with_disambiguator(filename, meeting_id)

    try:
        content = render_obsidian_note(meeting, summary, segments, cfg)
    except Exception as error:  # noqa: BLE001 - nunca deixa a exportação derrubar o pipeline
        return ObsidianExportResult(status="failed", error=f"falha ao montar a nota: {error}")

    tmp_path = target.with_name(target.name + f".tmp{os.getpid()}")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, target)
    except OSError as error:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return ObsidianExportResult(status="failed", error=str(error))

    return ObsidianExportResult(status="ok", path=str(target))
