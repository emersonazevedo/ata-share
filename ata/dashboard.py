"""Gera Dashboard/Dashboard.md — view DERIVADA e determinística sobre as notas
de `Reunioes/` e, a partir da Fase 3, também de `Projetos/`.

Nunca é fonte de verdade: pode ser apagado e reconstruído do zero a qualquer
momento lendo as notas existentes. Nunca usa LLM/Ollama — só leitura de
arquivo + parsing leve de frontmatter/seções conhecidas + escrita atômica.
Único writer técnico deste arquivo no vault (ver CLAUDE.md do vault). É
READ-ONLY sobre `Reunioes/` e `Projetos/` — nunca modifica uma nota durante
o rebuild.

Parsing é deliberadamente pouco esperto: frontmatter via regex line-a-linha
(sem depender de PyYAML — está no venv mas não é dependência declarada em
`requirements.txt`, então não é seguro contar com ela; o schema da Project
Note é pequeno e fixo o bastante para um parser próprio cobrir só o
necessário: escalares com aspas simples/duplas opcionais e UMA lista YAML de
1 nível para `cloud:`) e seções isoladas pelo cabeçalho `## ...` conhecido
(Reunião) ou pelos marcadores `<!-- ATA:AUTO:BEGIN/END <id> -->` (Projeto,
mais robusto a mudança de emoji/texto do heading) até o próximo limite —
nunca regex sobre o documento inteiro. Uma nota que não siga o formato
esperado não derruba o Dashboard: os campos ausentes viram vazio/"—" e uma
nota genuinamente ilegível (erro de I/O/encoding) é só pulada, com warning
no log.

REUNIÃO NÃO É PROJETO: contadores/status de reunião vêm exclusivamente de
`Reunioes/*.md`; contadores/status de projeto vêm exclusivamente de
`Projetos/*.md`. Nunca um deriva o outro.
"""

from __future__ import annotations

import ast
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .summarize import clean_cloud_providers, clean_owner, item_to_text

log = logging.getLogger("ata.dashboard")

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_SECTION_HEADING_RE = re.compile(r"^##[ \t]+.*$", re.MULTILINE)
_FILENAME_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_ACTION_LINE_RE = re.compile(r"^-\s*\[ \]\s*(.+)$")
_RISKS_HEADING = "## 🚨 Riscos / Bloqueios"
_ACTIONS_HEADING = "## 🚀 Ações / Próximos Passos"
_EMPTY_OWNER_MARKERS = ("", "—", "-")
# "— Responsável:" / "— Prazo:" com ou sem valor depois (o valor vazio deixa o rótulo no fim da linha)
_OWNER_SPLIT_RE = re.compile(r"\s+—\s+Responsável:(?:\s+|$)")
_DEADLINE_SPLIT_RE = re.compile(r"\s+—\s+Prazo:(?:\s+|$)")
_NONE_PLACEHOLDER_RE = re.compile(
    r"^nenhum[a]?\s+(?:risco|bloqueio|dúvida|pendência|identificad[oa])s?(?:\s+(?:ou|e|/)\s+\w+)*$",
    re.IGNORECASE,
)
_CONFIRM_MARK_RE = re.compile(r"\s*\[CONFIRMAR\]\s*", re.IGNORECASE)

# --- Project Note (Projetos/) ------------------------------------------------
# Formato das notas de projeto lidas de Projetos/ — aqui só consumimos o
# resultado, nunca escrevemos nelas.
_PROJECT_SCALAR_FIELDS = ("ticket", "cliente", "status", "checkpoint", "atualizado_em")
_CLOUD_LIST_RE = re.compile(r"(?m)^cloud:[ \t]*\n((?:[ \t]+-[ \t]+.+\n?)+)")
_AUTO_BLOCK_TEMPLATE = r"<!--\s*ATA:AUTO:BEGIN\s+{id}\s*-->(.*?)<!--\s*ATA:AUTO:END\s+{id}\s*-->"
_KNOWN_PROJECT_STATUSES = ("Bloqueado", "Em andamento", "Concluído")


@dataclass
class PendingAction:
    task: str
    owner: str = ""
    deadline: str = ""


@dataclass
class MeetingRow:
    filename: str            # basename sem ".md" -> usado literalmente no wikilink
    date_display: str        # "" se não achou nenhuma data utilizável
    sort_key: str            # "YYYY-MM-DD" normalizado p/ ordenar; "" = vai por último
    project: str = ""
    ticket: str = ""
    cloud: str = ""
    status: str = ""
    blocked: bool = False
    low_confidence: bool = False  # confianca: baixa (ata de texto sem falantes); só marcador visual
    risk_lines: list[str] = field(default_factory=list)
    actions: list[PendingAction] = field(default_factory=list)


@dataclass
class ProjectRow:
    filename: str                       # basename sem ".md" -> usado literalmente no wikilink
    ticket: str
    key: str = ""                       # identidade: Ticket real, ou DIRECT-NNNNNN; "" = sem chave
    cliente: str = ""
    cloud: list[str] = field(default_factory=list)
    status: str = ""                    # já normalizado (ver `_normalize_project_status`)
    status_known: bool = True           # False = status fora do enum conhecido (warning já logado)
    checkpoint: str = ""
    atualizado_em: str = ""             # "YYYY-MM-DD" válido, ou "" se ausente/malformado
    risk_lines: list[str] = field(default_factory=list)


@dataclass
class DashboardResult:
    status: str  # "ok" | "skipped" | "failed"
    path: str | None = None
    error: str | None = None
    meetings_count: int = 0
    projects_count: int = 0


# --- parsing -----------------------------------------------------------------

def _extract_frontmatter(text: str) -> dict[str, str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    block = match.group(1)
    out: dict[str, str] = {}
    for key in ("data", "projeto", "ticket", "cloud", "status", "confianca"):
        m = re.search(rf'(?m)^{re.escape(key)}:[ \t]*"?([^"\n]*?)"?[ \t]*$', block)
        if m:
            out[key] = m.group(1).strip()
    return out


def _extract_section(text: str, heading: str) -> str:
    """Isola só o texto entre `heading` e o próximo `## ` (ou o fim do arquivo) —
    nunca varre o documento inteiro."""
    idx = text.find(heading)
    if idx == -1:
        return ""
    rest = text[idx + len(heading):]
    m = _SECTION_HEADING_RE.search(rest)
    return rest[: m.start()] if m else rest


def _date_from_filename(filename: str) -> str:
    m = _FILENAME_DATE_RE.match(filename)
    return m.group(1) if m else ""


def _normalize_date(value: str) -> str:
    value = (value or "").strip()
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return ""
    return value


def _parse_risks(text: str) -> list[str]:
    section = _extract_section(text, _RISKS_HEADING)
    out: list[str] = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        content = line[2:].strip()
        if not content:
            continue
        # placeholder do próprio template ("Nenhum identificado.") não é risco real
        if content.rstrip(".").casefold() == "nenhum identificado":
            continue
        if _NONE_PLACEHOLDER_RE.match(content.rstrip(".")):    # "Nenhuma dúvida ou pendência" etc.
            continue
        out.append(_unrepr_dict(content))
    return out


def _unrepr_dict(content: str) -> str:
    """Notas antigas gravaram o repr de um dict do LLM como bullet. Só na
    exibição: converte de volta numa frase, sem tocar a nota."""
    if re.match(r"^'\w+':\s", content):          # repr sem as chaves externas
        content = "{" + content + "}"
    if content.startswith("{") and content.endswith("}"):
        try:
            value = ast.literal_eval(content)
        except (ValueError, SyntaxError):
            return content
        if isinstance(value, dict):
            return item_to_text(value) or content
    return content


def _parse_pending_actions(text: str) -> list[PendingAction]:
    section = _extract_section(text, _ACTIONS_HEADING)
    actions: list[PendingAction] = []
    for line in section.splitlines():
        line = line.strip()
        m = _ACTION_LINE_RE.match(line)
        if not m:
            continue
        rest = m.group(1)
        deadline = ""
        pm = _DEADLINE_SPLIT_RE.search(rest)
        if pm:
            rest, deadline = rest[: pm.start()], rest[pm.end():]
        owner = ""
        om = _OWNER_SPLIT_RE.search(rest)
        if om:
            rest, owner = rest[: om.start()], rest[om.end():]
        task = rest.strip()
        owner = clean_owner(owner)
        deadline = deadline.strip()
        if owner in _EMPTY_OWNER_MARKERS:
            owner = ""
        if not task:
            continue
        actions.append(PendingAction(task=task, owner=owner, deadline=deadline))
    return actions


def _clean_meeting_cloud(raw: str) -> str:
    """Nota antiga gravou a lista completa de clouds do template (eco do prompt);
    só na exibição isso vira "não identificado". O resto fica como está."""
    raw = (raw or "").strip()
    if raw and not clean_cloud_providers([c.strip() for c in raw.split(",")]):
        return ""
    return raw


def parse_meeting_note(path: Path) -> MeetingRow:
    """Lê e parseia UMA nota. Pode levantar (IO/encoding) — quem chama decide
    se pula a nota; nunca inventa dado ausente, só deixa vazio."""
    text = path.read_text(encoding="utf-8")
    fm = _extract_frontmatter(text)
    filename = path.stem
    raw_date = fm.get("data", "") or _date_from_filename(filename)
    sort_key = _normalize_date(fm.get("data", "")) or _normalize_date(_date_from_filename(filename))
    status = fm.get("status", "")
    return MeetingRow(
        filename=filename,
        date_display=raw_date,
        sort_key=sort_key,
        project=fm.get("projeto", ""),
        ticket=fm.get("ticket", ""),
        cloud=_clean_meeting_cloud(fm.get("cloud", "")),
        status=status,
        blocked=status.strip().casefold() == "bloqueado",
        low_confidence=fm.get("confianca", "").strip().casefold() == "baixa",
        risk_lines=_parse_risks(text),
        actions=_parse_pending_actions(text),
    )


def collect_meetings(meetings_path: Path) -> list[MeetingRow]:
    """Lê todas as `*.md` de `meetings_path`. Uma nota que falhe ao parsear
    (erro de I/O/encoding) é ignorada com warning — nunca derruba as demais."""
    if not meetings_path.is_dir():
        return []
    rows: list[MeetingRow] = []
    for note_path in sorted(meetings_path.glob("*.md")):
        try:
            rows.append(parse_meeting_note(note_path))
        except Exception as error:  # noqa: BLE001 - nota malformada não pode derrubar o dashboard
            log.warning("ignorando nota malformada em %s: %s", note_path, error)
    rows.sort(key=lambda r: (r.sort_key, r.filename), reverse=True)
    return rows


# --- parsing: Project Note (Projetos/) ---------------------------------------

def _unquote(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def _project_scalar(block: str, key: str) -> str:
    m = re.search(rf"(?m)^{re.escape(key)}:[ \t]*(.*?)[ \t]*$", block)
    return _unquote(m.group(1)) if m else ""


def _project_cloud_list(block: str) -> list[str]:
    """Só entende a forma gravada pelo obsidian_sync — `cloud:` sozinho na
    linha seguido de itens `  - Item` indentados. `cloud:` escalar (não usado
    pelo contrato de Projeto) intencionalmente não é interpretado como lista
    — evita parsing frágil fora do que o schema realmente produz."""
    m = _CLOUD_LIST_RE.search(block)
    if not m:
        return []
    items = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if line.startswith("- "):
            value = _unquote(line[2:].strip())
            if value:
                items.append(value)
    return items


def _extract_project_frontmatter(text: str) -> dict[str, Any]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    block = match.group(1)
    out: dict[str, Any] = {key: _project_scalar(block, key) for key in _PROJECT_SCALAR_FIELDS}
    out["cloud"] = _project_cloud_list(block)
    return out


def _extract_auto_block(text: str, block_id: str) -> str:
    pattern = re.compile(_AUTO_BLOCK_TEMPLATE.format(id=re.escape(block_id)), re.DOTALL)
    m = pattern.search(text)
    return m.group(1).strip() if m else ""


def _is_placeholder(text: str) -> bool:
    """Todo placeholder "vazio" do obsidian_sync é emitido como `_texto em
    itálico_` (ex. `_Nenhum risco/bloqueio extraído nesta sincronização._`).
    Checar esse padrão em vez de strings específicas sobrevive a mudança de
    texto do placeholder no writer."""
    stripped = text.strip()
    return len(stripped) >= 2 and stripped.startswith("_") and stripped.endswith("_")


def _parse_project_risks(text: str) -> list[str]:
    section = _extract_auto_block(text, "riscos")
    if not section or _is_placeholder(section):
        return []
    out: list[str] = []
    for line in section.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("- "):
            line = line[2:].strip()
        if line and not _is_placeholder(line):
            out.append(line)
    return out


def _normalize_project_status(raw: str) -> tuple[str, bool]:
    """Retorna (status_normalizado, conhecido). Comparação case-insensitive;
    valor fora do enum é preservado como está (nunca inventado), com
    `conhecido=False` para quem chama decidir se loga warning."""
    raw = (raw or "").strip()
    for known in _KNOWN_PROJECT_STATUSES:
        if raw.casefold() == known.casefold():
            return known, True
    return raw, False


_DIRECT_CODE_RE = re.compile(r"DIRECT-\d+", re.IGNORECASE)
_NOT_APPLICABLE = "NOT_APPLICABLE"


def _project_key(ticket: str, filename: str) -> str:
    """Ticket real (não vazio, não NOT_APPLICABLE) é a chave; demanda DIRECT usa
    o código DIRECT-NNNNNN do nome do arquivo. NOT_APPLICABLE nunca é
    chave. Retorna "" quando não há nenhuma das duas."""
    if ticket and ticket.upper() != _NOT_APPLICABLE:
        return ticket
    m = _DIRECT_CODE_RE.search(filename)
    return m.group(0) if m else ""


def parse_project_note(path: Path) -> ProjectRow:
    """Lê e parseia UMA Project Note. Pode levantar (IO/encoding) — quem
    chama decide se pula a nota. Nunca descarta por falta de chave: `key`
    fica vazia e quem coleta emite o aviso."""
    text = path.read_text(encoding="utf-8")
    fm = _extract_project_frontmatter(text)
    ticket = str(fm.get("ticket") or "").strip()
    status, status_known = _normalize_project_status(str(fm.get("status") or ""))
    return ProjectRow(
        filename=path.stem,
        ticket=ticket,
        key=_project_key(ticket, path.stem),
        cliente=str(fm.get("cliente") or "").strip(),
        cloud=fm.get("cloud") or [],
        status=status,
        status_known=status_known,
        checkpoint=str(fm.get("checkpoint") or "").strip(),
        atualizado_em=_normalize_date(str(fm.get("atualizado_em") or "")),
        risk_lines=_parse_project_risks(text),
    )


def collect_projects(projetos_path: Path) -> list[ProjectRow]:
    """Lê todas as `*.md` de `projetos_path`. Nota malformada (I/O/encoding) é
    ignorada com warning. Duplicidade = mesma chave em 2+ arquivos: com Ticket
    real nenhuma é contada (warning); com código DIRECT conta só a primeira
    (por nome de arquivo) + warning. Sem chave de identidade a nota é contada
    com warning — nunca descartada."""
    if not projetos_path.is_dir():
        return []
    by_key: dict[str, list[ProjectRow]] = {}
    rows: list[ProjectRow] = []
    for note_path in sorted(projetos_path.glob("*.md")):
        try:
            row = parse_project_note(note_path)
        except Exception as error:  # noqa: BLE001 - Project Note malformada não pode derrubar o dashboard
            log.warning("ignorando Project Note malformada em %s: %s", note_path, error)
            continue
        if not row.status_known:
            log.warning("Project Note %s com status desconhecido: %r", note_path, row.status)
        if not row.key:
            log.warning("projeto sem chave de identidade: %s", note_path.name)
            rows.append(row)
            continue
        by_key.setdefault(row.key, []).append(row)

    for key, group in by_key.items():
        if len(group) > 1:
            names = ", ".join(r.filename for r in group)
            direct = key.upper().startswith("DIRECT-")
            log.warning(
                "%s duplicado (%s) em %d Project Notes (%s) — %s",
                "Código DIRECT" if direct else "Ticket", key, len(group), names,
                "contando só a primeira" if direct else "nenhuma será contada",
            )
            if direct:
                rows.append(group[0])
            continue
        rows.append(group[0])
    rows.sort(key=lambda r: r.filename)
    return rows


def _date_sort_value(value: str) -> int:
    """Ordinal do dia p/ ordenar por data; sem data válida = menor prioridade
    (fica por último mesmo em ordenação decrescente)."""
    normalized = _normalize_date(value)
    if not normalized:
        return -1
    return datetime.strptime(normalized, "%Y-%m-%d").toordinal()


# --- renderização --------------------------------------------------------------
# v1.1: só apresentação (callouts recolhíveis + limite visual da tabela). Nenhuma
# função acima desta linha (parsing/coleta) foi alterada; contagens globais
# (Visão Geral, Clouds, Ações, Atenção) sempre consideram TODAS as reuniões —
# só a tabela "Últimas Reuniões" tem limite visual.

_TABLE_LIMIT = 10
_NO_OWNER = "Sem responsável"
_LOW_CONFIDENCE_MARK = "⚠ fonte sem falantes"


def _meeting_link(m: MeetingRow) -> str:
    """Wikilink da reunião; com marcador quando a fonte tem confiança baixa.
    Nunca altera contadores — só apresentação."""
    link = f"[[{m.filename}]]"
    return f"{link} {_LOW_CONFIDENCE_MARK}" if m.low_confidence else link


def _count_label(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def _project_is_attention(p: ProjectRow) -> bool:
    return p.status == "Bloqueado" or bool(p.risk_lines)


def _render_attention_section(meetings: list[MeetingRow], projects: list[ProjectRow]) -> str:
    """Projeto e reunião nunca se misturam numa linha só — cada grupo tem seu
    próprio subtítulo dentro do callout, então o tipo de cada item nunca é
    ambíguo. Formato de cada item de REUNIÃO é o mesmo de antes da Fase 3
    (nenhuma nota/teste existente sobre reunião precisa mudar)."""
    meeting_items: list[str] = []
    for m in meetings:
        if not (m.blocked or m.risk_lines):
            continue
        parts = [_meeting_link(m)]
        if m.blocked:
            parts.append("Bloqueado")
        if m.risk_lines:
            parts.append("Riscos: " + "; ".join(m.risk_lines))
        meeting_items.append("- " + " — ".join(parts))

    project_items: list[str] = []
    for p in projects:
        if not _project_is_attention(p):
            continue
        parts = [f"[[{p.filename}]]"]
        if p.status == "Bloqueado":
            parts.append("Bloqueado")
        if p.risk_lines:
            parts.append("Riscos: " + "; ".join(p.risk_lines))
        project_items.append("- " + " — ".join(parts))

    total = len(meeting_items) + len(project_items)
    if total == 0:
        return "## 🚨 Atenção\n\nNenhum bloqueio ou risco identificado."
    label = _count_label(total, "item", "itens")
    lines = [f"> [!warning]- 🚨 Atenção — {label}"]
    if project_items:
        lines.append("> **Projetos:**")
        lines += [f"> {item}" for item in project_items]
    if meeting_items:
        lines.append("> **Reuniões:**")
        lines += [f"> {item}" for item in meeting_items]
    return "\n".join(lines)


def _render_actions_section(meetings: list[MeetingRow]) -> str:
    """Agrupa por reunião (callout aninhado, recolhido) e resume a contagem por
    responsável. Só apresentação: nenhuma ação é descartada aqui (o parser já
    descarta task vazia) e a nota de reunião continua sendo a fonte de verdade."""
    total = sum(len(m.actions) for m in meetings)
    if total == 0:
        return "## 🚀 Ações Pendentes\n\nNenhuma ação pendente."
    by_owner: dict[str, int] = {}
    for m in meetings:
        for action in m.actions:
            key = _CONFIRM_MARK_RE.sub(" ", action.owner).strip() or _NO_OWNER
            by_owner[key] = by_owner.get(key, 0) + 1
    ordered = sorted(by_owner.items(), key=lambda kv: (kv[0] == _NO_OWNER, -kv[1], kv[0]))

    lines = [f"> [!todo]- 🚀 Ações Pendentes — {_count_label(total, 'ação', 'ações')}"]
    lines.append("> **Por responsável:** " + " · ".join(f"{o} ({n})" for o, n in ordered))
    for m in meetings:
        if not m.actions:
            continue
        lines.append(">")
        lines.append(f"> > [!todo]- {_meeting_link(m)} — {_count_label(len(m.actions), 'ação', 'ações')}")
        for action in m.actions:
            parts = [f"- [ ] {action.task}"]
            if action.owner:
                parts.append(action.owner)
            if action.deadline:
                parts.append(f"Prazo: {action.deadline}")
            parts.append(f"[[{m.filename}]]")
            lines.append("> > " + " — ".join(parts))
    return "\n".join(lines)


def _display_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return value or "—"


def _render_table_section(meetings: list[MeetingRow]) -> str:
    total = len(meetings)
    shown = meetings[:_TABLE_LIMIT]
    if not shown:
        body = "_Nenhuma reunião registrada._"
    else:
        rows = ["| Data | Reunião | Projeto | Cloud | Status |", "|---|---|---|---|---|"]
        for m in shown:
            rows.append(
                f"| {_display_date(m.date_display)} | {_meeting_link(m)} | "
                f"{m.project or '—'} | {m.cloud or '—'} | {m.status or '—'} |"
            )
        body = "\n".join(rows)
    if total > _TABLE_LIMIT:
        body += f"\n\n> Exibindo as {_TABLE_LIMIT} reuniões mais recentes de {total} reuniões."
    return body


def _render_clouds(meetings: list[MeetingRow], projects: list[ProjectRow]) -> str:
    """Reuniões e Projetos contam a cloud cada um a partir da sua própria
    fonte (nunca misturados) — projeto multicloud conta 1x em cada cloud sua,
    sem inflar a contagem GLOBAL de projetos (essa vem só de `len(projects)`,
    em `render_dashboard`)."""
    meeting_counts: dict[str, int] = {}
    for m in meetings:
        cloud = m.cloud.strip()
        if cloud:
            meeting_counts[cloud] = meeting_counts.get(cloud, 0) + 1

    project_counts: dict[str, int] = {}
    for p in projects:
        for cloud in p.cloud:
            cloud = cloud.strip()
            if cloud:
                project_counts[cloud] = project_counts.get(cloud, 0) + 1

    if not meeting_counts and not project_counts:
        return "_Nenhuma cloud identificada._"

    lines: list[str] = []
    if meeting_counts:
        lines.append("**Reuniões:**")
        ordered = sorted(meeting_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        lines += [f"- {cloud}: {n} reuni{'ão' if n == 1 else 'ões'}" for cloud, n in ordered]
    if project_counts:
        if lines:
            lines.append("")
        lines.append("**Projetos:**")
        ordered = sorted(project_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        lines += [f"- {cloud}: {n} projeto{'' if n == 1 else 's'}" for cloud, n in ordered]
    return "\n".join(lines)


def _render_active_projects(projects: list[ProjectRow]) -> str:
    active = [p for p in projects if p.status != "Concluído"]
    if not active:
        return "## 📁 Projetos Ativos\n\nNenhum projeto ativo."
    order = {"Bloqueado": 0, "Em andamento": 1}
    ordered = sorted(
        active,
        key=lambda p: (order.get(p.status, 2), -_date_sort_value(p.atualizado_em), p.filename),
    )
    rows = ["| Ticket | Projeto | Cloud | Checkpoint | Status |", "| --- | --- | --- | --- | --- |"]
    for p in ordered:
        cloud = ", ".join(p.cloud) if p.cloud else "—"
        rows.append(f"| {p.ticket if p.ticket.upper() != _NOT_APPLICABLE else (p.key or '—')} | [[{p.filename}]] | {cloud} | {p.checkpoint or '—'} | {p.status or '—'} |")
    return "## 📁 Projetos Ativos\n\n" + "\n".join(rows)


def _render_recent_projects(projects: list[ProjectRow]) -> str:
    total = len(projects)
    ordered = sorted(projects, key=lambda p: (-_date_sort_value(p.atualizado_em), p.filename))
    shown = ordered[:_TABLE_LIMIT]
    if not shown:
        body = "_Nenhum projeto registrado._"
    else:
        rows = ["| Projeto | Cloud | Checkpoint | Status | Atualizado |", "| --- | --- | --- | --- | --- |"]
        for p in shown:
            cloud = ", ".join(p.cloud) if p.cloud else "—"
            rows.append(
                f"| [[{p.filename}]] | {cloud} | {p.checkpoint or '—'} | {p.status or '—'} | "
                f"{_display_date(p.atualizado_em)} |"
            )
        body = "\n".join(rows)
    if total > _TABLE_LIMIT:
        body += f"\n\n> Exibindo os {_TABLE_LIMIT} projetos mais recentes de {total} projetos."
    return "## 🕘 Projetos Recentes\n\n" + body


def render_dashboard(
    meetings: list[MeetingRow], generated_at: datetime, projects: list[ProjectRow] | None = None
) -> str:
    projects = projects or []

    total = len(meetings)
    counts = {"Em andamento": 0, "Bloqueado": 0, "Concluído": 0}
    for m in meetings:
        key = m.status.strip()
        if key in counts:
            counts[key] += 1
    pending_count = sum(len(m.actions) for m in meetings)

    projects_total = len(projects)
    project_counts = {"Em andamento": 0, "Bloqueado": 0, "Concluído": 0}
    for p in projects:
        if p.status in project_counts:
            project_counts[p.status] += 1

    parts = [
        "# 🏠 Dashboard",
        "",
        f"> Atualizado automaticamente em {generated_at.strftime('%d/%m/%Y %H:%M')}",
        "> Fontes: notas em Reunioes/ e Projetos/",
        "",
        "## 📊 Visão Geral",
        "",
        "### Reuniões",
        "",
        f"- Reuniões: {total}",
        f"- Em andamento: {counts['Em andamento']}",
        f"- Bloqueadas: {counts['Bloqueado']}",
        f"- Concluídas: {counts['Concluído']}",
        f"- Ações pendentes: {pending_count}",
        "",
        "### Projetos",
        "",
        f"- Projetos: {projects_total}",
        f"- Em andamento: {project_counts['Em andamento']}",
        f"- Bloqueados: {project_counts['Bloqueado']}",
        f"- Concluídos: {project_counts['Concluído']}",
        "",
        _render_attention_section(meetings, projects),
        "",
        _render_actions_section(meetings),
        "",
        "## 📅 Últimas Reuniões",
        "",
        _render_table_section(meetings),
        "",
        _render_active_projects(projects),
        "",
        _render_recent_projects(projects),
        "",
        "## ☁️ Clouds",
        "",
        _render_clouds(meetings, projects),
        "",
    ]
    return "\n".join(parts)


# --- I/O -----------------------------------------------------------------

def rebuild_dashboard(
    meetings_path: Path | None,
    dashboard_path: Path | None,
    projetos_path: Path | None = None,
    *,
    enabled: bool = True,
    now: datetime | None = None,
) -> DashboardResult:
    """`skipped` só quando `enabled=False` (feature desligada). Vault ausente/
    inválido com `enabled=True` é `failed`, nunca mascarado como `skipped` —
    mesma convenção de `obsidian.export_meeting_note`. Nunca lança.

    `projetos_path=None` (não configurado) é tratado como "zero projetos" —
    não é erro, mantém compatível com quem ainda chama sem esse argumento.
    READ-ONLY sobre `meetings_path`/`projetos_path`: nunca escreve nada além
    de `dashboard_path`."""
    if not enabled:
        return DashboardResult(status="skipped")
    if meetings_path is None or dashboard_path is None:
        return DashboardResult(status="failed", error="vault_path não configurado ou inexistente")
    try:
        meetings = collect_meetings(meetings_path)
        projects = collect_projects(projetos_path) if projetos_path is not None else []
        content = render_dashboard(meetings, now or datetime.now(), projects)
        dashboard_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dashboard_path.with_name(dashboard_path.name + f".tmp{os.getpid()}")
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, dashboard_path)
    except Exception as error:  # noqa: BLE001 - rebuild nunca pode propagar
        try:
            tmp_path.unlink(missing_ok=True)  # type: ignore[possibly-undefined]
        except Exception:
            pass
        return DashboardResult(status="failed", error=str(error))
    return DashboardResult(
        status="ok", path=str(dashboard_path), meetings_count=len(meetings), projects_count=len(projects),
    )
