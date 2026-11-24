from ata.render import render_ata, render_meeting_md

MEETING = {"title": "Semana 1", "started_at": "2026-09-02T12:00:00Z",
           "duration_seconds": 3661, "language": "pt"}


def test_ata_has_all_sections():
    md = render_ata(MEETING, {
        "summary": "Discutimos o roadmap.",
        "agenda_items": [{"topic": "Roadmap", "discussion": "3 entregas; decidiu adiar release"}],
        "action_items": [{"task": "revisar PR", "owner": "Ana", "deadline": None}],
        "next_steps": "", "other_notes": ["prazo apertado"],
    })
    for header in ("# Ata — Semana 1", "## Resumo executivo", "## Itens da Agenda", "## Itens de Ação",
                   "## Próximos Passos", "## Outras Observações"):
        assert header in md
    assert "**Data e Hora:** 02/09/2026 09:00" in md   # 12:00 UTC -> Brasília (UTC-3)
    assert "**Duração:** 01:01:01" in md
    assert "### Roadmap" in md
    assert "- [ ] revisar PR — resp.: Ana — prazo: —" in md
    assert "_nenhum próximo passo registrado_" in md   # next_steps vazio


def test_ata_without_summary_shows_placeholder():
    md = render_ata(MEETING, None)
    assert "_Resumo não disponível._" in md


def test_meeting_md_appends_transcript():
    segs = [{"source": "me", "start": 1.0, "end": 2.0, "text": "olá"},
            {"source": "system", "start": 3.0, "end": 4.0, "text": "oi"}]
    md = render_meeting_md(MEETING, {"summary": "x"}, segs)
    assert "## Transcrição" in md
    assert "**[00:00:01] ME**" in md
    assert "**[00:00:03] SYSTEM**" in md
