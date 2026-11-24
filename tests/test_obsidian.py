"""Exportador Obsidian: tudo com tmp_path como vault fake — nenhum teste toca
um vault real."""

from pathlib import Path

from ata.config import ObsidianConfig
from ata.obsidian import (
    ObsidianExportResult,
    build_filename,
    export_meeting_note,
    render_obsidian_note,
    sanitize_filename,
)


def _meeting(**overrides):
    base = {
        "id": "meet123abc",
        "title": "Reunião de Kickoff",
        "started_at": "2026-09-14T12:00:00Z",
        "ticket": None,
        "project_name": None,
        "client": None,
        "cloud": None,
    }
    base.update(overrides)
    return base


def _summary(**overrides):
    base = {
        "summary": "Discutimos o roadmap do trimestre.",
        "agenda_items": [{"topic": "Roadmap", "discussion": "3 entregas"}],
        "action_items": [{"task": "revisar PR", "owner": "Ana", "deadline": None}],
        "next_steps": "Marcar follow-up.",
        "other_notes": [],
        "participants": ["Ana", "Bruno"],
        "decisions": ["Adiar o release para o próximo sprint"],
        "risks": ["Atraso do fornecedor de infraestrutura"],
        "technical_points": {"services": ["S3"], "architecture": [], "dependencies": ["API legada"]},
        "cloud_providers": ["AWS"],
    }
    base.update(overrides)
    return base


_SEGMENTS = [
    {"source": "me", "start": 1.0, "end": 2.0, "text": "olá pessoal"},
    {"source": "system", "start": 3.0, "end": 4.0, "text": "oi, tudo bem"},
]


# --- sanitize_filename / build_filename -------------------------------------

def test_sanitize_filename_removes_windows_invalid_chars():
    assert sanitize_filename('Reunião: Kickoff / Fase 1 <teste>') == "Reunião- Kickoff - Fase 1 -teste-"


def test_sanitize_filename_trims_trailing_dots_and_spaces():
    assert sanitize_filename("Nome.  ") == "Nome"


def test_sanitize_filename_replaces_invalid_chars_without_emptying():
    assert sanitize_filename("::::") == "----"


def test_sanitize_filename_falls_back_when_empty():
    assert sanitize_filename("") == "Reuniao"
    assert sanitize_filename("   ") == "Reuniao"
    assert sanitize_filename(" . . ") == "Reuniao"


def test_build_filename_uses_date_and_sanitized_title():
    m = _meeting(title="Kickoff: Projeto X", started_at="2026-09-14T12:00:00Z")
    assert build_filename(m) == "2026-09-14 - Kickoff- Projeto X.md"


def test_build_filename_never_includes_ticket():
    m = _meeting(title="Reunião", ticket="99999")
    assert "99999" not in build_filename(m)


# --- render_obsidian_note ----------------------------------------------------

def _cfg(**overrides):
    base = dict(enabled=True, vault_path="", meetings_folder="Reunioes", auto_export=True,
                default_status="Em andamento")
    base.update(overrides)
    return ObsidianConfig(**base)


def test_render_standalone_meeting_has_empty_project_fields():
    note = render_obsidian_note(_meeting(), _summary(), _SEGMENTS, _cfg())
    assert 'projeto: \n' in note or note.split("\n")[2] == "projeto: "
    assert "ticket: \n" in note or "ticket: " in note
    assert "[[TKT-" not in note


def test_render_meeting_with_project_generates_wikilink():
    m = _meeting(ticket="12345", project_name="Projeto X", client="Cliente Y")
    note = render_obsidian_note(m, _summary(), _SEGMENTS, _cfg())
    assert 'projeto: "[[TKT-12345 - Projeto X]]"' in note
    assert "Projeto X — Cliente Y" in note


def test_render_wikilink_requires_both_ticket_and_project_name():
    only_ticket = render_obsidian_note(_meeting(ticket="1"), _summary(), _SEGMENTS, _cfg())
    assert "[[TKT-" not in only_ticket
    assert 'ticket: "1"' in only_ticket

    only_project = render_obsidian_note(_meeting(project_name="Sem Ticket"), _summary(), _SEGMENTS, _cfg())
    assert "[[TKT-" not in only_project


def test_render_note_when_project_note_does_not_exist_yet_does_not_fail():
    """CLAUDE.md: vínculo com projeto não deve bloquear o registro da reunião,
    mesmo que a nota em Projetos/ ainda não exista — este módulo não checa nem
    cria essa nota, só referencia via wikilink."""
    m = _meeting(ticket="1", project_name="Projeto Inexistente Ainda")
    note = render_obsidian_note(m, _summary(), _SEGMENTS, _cfg())
    assert "[[TKT-1 - Projeto Inexistente Ainda]]" in note


def test_render_template_sections_present():
    note = render_obsidian_note(_meeting(), _summary(), _SEGMENTS, _cfg())
    for header in ("## 🎯 Objetivo", "## 📋 Pauta", "## 📝 Resumo da Reunião", "## ✅ Decisões",
                   "## 🚨 Riscos / Bloqueios", "## 🔧 Pontos Técnicos", "## 🚀 Ações / Próximos Passos",
                   "## 📎 Links / Evidências", "## 🎙️ Transcript da Reunião"):
        assert header in note
    for key in ("data:", "projeto:", "ticket:", "cloud:", "status:", "tipo:", "tags:", "cctranscript_id:"):
        assert key in note


def test_render_transcript_inside_callout():
    note = render_obsidian_note(_meeting(), _summary(), _SEGMENTS, _cfg())
    assert "> [!note]- Abrir transcript completo" in note
    assert "> **[00:00:01] ME**" in note
    assert "> olá pessoal" in note
    assert "> **[00:00:03] SYSTEM**" in note


def test_render_cloud_prefers_user_metadata_over_summary():
    m = _meeting(cloud="Azure")
    note = render_obsidian_note(m, _summary(cloud_providers=["AWS"]), _SEGMENTS, _cfg())
    assert 'cloud: "Azure"' in note
    assert "AWS" not in note


def test_render_cloud_falls_back_to_summary_when_user_did_not_inform():
    note = render_obsidian_note(_meeting(cloud=None), _summary(cloud_providers=["AWS", "GCP"]), _SEGMENTS, _cfg())
    assert 'cloud: "AWS, GCP"' in note


def test_render_old_meeting_without_new_summary_fields_does_not_crash():
    old_summary = {"summary": "resumo antigo", "agenda_items": [], "action_items": [],
                   "next_steps": "", "other_notes": []}
    note = render_obsidian_note(_meeting(), old_summary, _SEGMENTS, _cfg())
    assert "resumo antigo" in note
    assert "Nenhuma decisão identificada." in note
    assert "Nenhum identificado." in note  # riscos e/ou sub-itens de pontos técnicos


def test_render_none_summary_does_not_crash():
    note = render_obsidian_note(_meeting(), None, _SEGMENTS, _cfg())
    assert "## 📝 Resumo da Reunião" in note


def test_render_technical_points_normalized_dict_used():
    tp = {"services": ["S3", "Lambda"], "architecture": ["Event-driven"], "dependencies": []}
    note = render_obsidian_note(_meeting(), _summary(technical_points=tp), _SEGMENTS, _cfg())
    assert "> - S3" in note and "> - Lambda" in note
    assert "> - Event-driven" in note


# --- export_meeting_note (I/O em tmp_path) -----------------------------------

def test_export_skipped_when_disabled(tmp_path):
    result = export_meeting_note(_meeting(), _summary(), _SEGMENTS, _cfg(enabled=False), tmp_path)
    assert result.status == "skipped"
    assert list(tmp_path.iterdir()) == []


def test_export_failed_when_vault_path_missing(tmp_path):
    result = export_meeting_note(_meeting(), _summary(), _SEGMENTS, _cfg(enabled=True), None)
    assert result.status == "failed"
    assert result.error


def test_export_writes_file_successfully(tmp_path):
    result = export_meeting_note(_meeting(), _summary(), _SEGMENTS, _cfg(enabled=True), tmp_path)
    assert result.status == "ok"
    path = Path(result.path)
    assert path.is_file()
    assert path.parent == tmp_path
    assert "cctranscript_id: meet123abc" in path.read_text(encoding="utf-8")


def test_export_reexport_same_meeting_overwrites_not_duplicates(tmp_path):
    cfg = _cfg(enabled=True)
    r1 = export_meeting_note(_meeting(), _summary(), _SEGMENTS, cfg, tmp_path)
    r2 = export_meeting_note(_meeting(), _summary(summary="atualizado"), _SEGMENTS, cfg, tmp_path)
    assert r1.status == "ok" and r2.status == "ok"
    assert r1.path == r2.path
    files = list(tmp_path.glob("*.md"))
    assert len(files) == 1
    assert "atualizado" in Path(r2.path).read_text(encoding="utf-8")


def test_export_collision_with_different_meeting_gets_disambiguated(tmp_path):
    cfg = _cfg(enabled=True)
    m1 = _meeting(id="meet-aaa")
    m2 = _meeting(id="meet-bbb")  # mesmo título/data -> mesmo filename base
    r1 = export_meeting_note(m1, _summary(), _SEGMENTS, cfg, tmp_path)
    r2 = export_meeting_note(m2, _summary(), _SEGMENTS, cfg, tmp_path)
    assert r1.status == "ok" and r2.status == "ok"
    assert r1.path != r2.path
    # a nota da primeira reunião continua intacta
    assert "cctranscript_id: meet-aaa" in Path(r1.path).read_text(encoding="utf-8")
    assert "cctranscript_id: meet-bbb" in Path(r2.path).read_text(encoding="utf-8")


def test_export_does_not_touch_unrelated_file_with_same_name(tmp_path):
    """Nota criada manualmente (sem cctranscript_id) com o mesmo nome -> tratada
    como colisão, nunca sobrescrita."""
    cfg = _cfg(enabled=True)
    m = _meeting()
    target = tmp_path / build_filename(m)
    target.write_text("nota manual do usuário, sem marcador", encoding="utf-8")

    result = export_meeting_note(m, _summary(), _SEGMENTS, cfg, tmp_path)

    assert result.status == "ok"
    assert result.path != str(target)
    assert target.read_text(encoding="utf-8") == "nota manual do usuário, sem marcador"


def test_export_failed_on_write_error_does_not_raise(tmp_path, monkeypatch):
    cfg = _cfg(enabled=True)
    # o "diretório de reuniões" na verdade é um arquivo -> mkdir falha
    blocked = tmp_path / "Reunioes"
    blocked.write_text("não é uma pasta", encoding="utf-8")

    result = export_meeting_note(_meeting(), _summary(), _SEGMENTS, cfg, blocked)

    assert result.status == "failed"
    assert result.error


def test_export_result_is_plain_dataclass():
    r = ObsidianExportResult(status="ok", path="x", error=None)
    assert r.status == "ok" and r.path == "x" and r.error is None
