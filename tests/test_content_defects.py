"""Defeitos de conteúdo de 08/10/2026: dict cru em listas (A), ME/SYSTEM como
responsável (B), Dashboard de ações agrupado por reunião (C)."""

from datetime import datetime

from ata.config import ObsidianConfig
from ata.dashboard import collect_meetings, render_dashboard
from ata.obsidian import render_obsidian_note
from ata.summarize import clean_owner, item_to_text, normalize_payload

_NOW = datetime(2026, 10, 8, 10, 0)
_MEETING = {"id": "m1", "title": "T", "started_at": "2026-09-14T12:00:00Z"}


def _render(summary):
    return render_obsidian_note(_MEETING, summary, [], ObsidianConfig())


# --- A: dict cru -----------------------------------------------------------------

def test_item_to_text_dict_and_string():
    assert item_to_text({"topic": "Latência", "risk": "Pode passar de 200ms"}) == "Latência: Pode passar de 200ms"
    assert item_to_text({"description": "x"}) == "x"
    assert item_to_text({"foo": "bar"}) == "bar"          # chave desconhecida: valores textuais
    assert item_to_text("  texto ") == "texto"
    assert item_to_text(None) == "" and item_to_text({}) == ""


def test_note_renders_risk_dict_as_sentence():
    note = _render({"risks": [{"topic": "Latência", "risk": "A solução pode ter mais de 200ms"}],
                    "decisions": [{"decision": "Usar OCI"}], "participants": [{"name": "Ana"}]})
    assert "- Latência: A solução pode ter mais de 200ms" in note
    assert "{'" not in note and "- Usar OCI" in note and "Ana" in note


def test_normalize_payload_flattens_dicts_and_drops_empty():
    out = normalize_payload({
        "risks": [{"topic": "Latência", "risk": "Alta"}, "", {}, "ok"],
        "decisions": [{"description": "D1"}],
        "participants": ["Ana", "ME", "system"],
    })
    assert out["risks"] == ["Latência: Alta", "ok"]
    assert out["decisions"] == ["D1"]
    assert out["participants"] == ["Ana"]


def test_dashboard_cleans_legacy_dict_repr_risk(tmp_path):
    d = tmp_path / "Reunioes"; d.mkdir()
    (d / "2026-09-15 - X.md").write_text(
        "---\ndata: \"2026-09-15\"\n---\n\n## 🚨 Riscos / Bloqueios\n\n"
        "- {'topic': 'Latência', 'risk': 'Mais de 200ms'}\n\n## 🚀 Ações / Próximos Passos\n\n", encoding="utf-8")
    assert collect_meetings(d)[0].risk_lines == ["Latência: Mais de 200ms"]


# --- B: ME/SYSTEM como responsável ----------------------------------------------

def test_clean_owner_channel_labels():
    for v in ("ME", "me", "SYSTEM", "Sistema", " system "):
        assert clean_owner(v) == ""
    assert clean_owner("Ana") == "Ana" and clean_owner(None) == ""


def test_note_owner_me_system_becomes_empty():
    note = _render({"action_items": [{"task": "A", "owner": "ME"}, {"task": "B", "owner": "SYSTEM"},
                                      {"task": "C", "owner": "Ana"}]})
    assert "- [ ] A — Responsável: \n" in note + "\n"
    assert "ME" not in note.split("Ações")[1] and "SYSTEM" not in note.split("Ações")[1]
    assert "- [ ] C — Responsável: Ana" in note


def test_normalize_payload_owner_me_system():
    out = normalize_payload({"action_items": [{"task": "A", "owner": "ME"}, {"task": "B", "owner": "Sistema"},
                                               {"task": "C", "owner": "Bia"}]})
    assert [i["owner"] for i in out["action_items"]] == [None, None, "Bia"]


def test_dashboard_ignores_legacy_me_system_owner(tmp_path):
    d = tmp_path / "Reunioes"; d.mkdir()
    (d / "2026-09-15 - X.md").write_text(
        "---\ndata: \"2026-09-15\"\n---\n\n## 🚀 Ações / Próximos Passos\n\n"
        "- [ ] Definir — Responsável: ME\n- [ ] Implementar — Responsável: SYSTEM\n", encoding="utf-8")
    assert [a.owner for a in collect_meetings(d)[0].actions] == ["", ""]


# --- lista vazia / C: agrupamento -------------------------------------------------

def test_empty_lists_render_placeholders():
    note = _render({"risks": [], "decisions": [], "action_items": [], "participants": []})
    assert "- Nenhum identificado." in note and "- Nenhuma decisão identificada." in note
    assert "- Nenhuma ação registrada." in note


def test_dashboard_actions_grouped_by_meeting_with_owner_counts(tmp_path):
    d = tmp_path / "Reunioes"; d.mkdir()
    for name, acts in (("2026-09-15 - A", "- [ ] t1 — Responsável: Ana\n- [ ] t2 — Responsável: \n"),
                       ("2026-09-16 - B", "- [ ] t3 — Responsável: Ana\n- [ ] t4 — Responsável: ME\n")):
        (d / f"{name}.md").write_text(
            f"---\ndata: \"{name[:10]}\"\n---\n\n## 🚀 Ações / Próximos Passos\n\n{acts}", encoding="utf-8")
    dash = render_dashboard(collect_meetings(d), _NOW)
    assert "Ações Pendentes — 4 ações" in dash
    assert "**Por responsável:** Ana (2) · Sem responsável (2)" in dash
    assert "> > [!todo]- [[2026-09-15 - A]] — 2 ações" in dash
    assert "> > [!todo]- [[2026-09-16 - B]] — 2 ações" in dash


def test_dashboard_empty_meeting_set_and_no_project_has_no_warning(tmp_path):
    dash = render_dashboard([], _NOW)
    assert "Nenhuma ação pendente." in dash and "ticket" not in dash.lower()


# --- chave de identidade de Projeto (DIRECT) ----------------------------------------

def _proj(dir_, name, ticket):
    (dir_ / name).write_text(
        f'---\ntipo: projeto\nticket: "{ticket}"\ncloud:\n  - OCI\nstatus: Em andamento\n---\n\n# X\n',
        encoding="utf-8")


def _collect(tmp_path, files, caplog):
    from ata.dashboard import collect_projects
    d = tmp_path / "Projetos"; d.mkdir()
    for name, ticket in files:
        _proj(d, name, ticket)
    with caplog.at_level("WARNING"):
        return collect_projects(d)


def test_two_distinct_direct_count_two(tmp_path, caplog):
    rows = _collect(tmp_path, [("DIRECT-101630 - A.md", "NOT_APPLICABLE"),
                               ("DIRECT-102059 - B.md", "NOT_APPLICABLE")], caplog)
    assert len(rows) == 2 and not caplog.records


def test_same_direct_code_counts_one_with_warning(tmp_path, caplog):
    rows = _collect(tmp_path, [("DIRECT-101630 - A.md", "NOT_APPLICABLE"),
                               ("DIRECT-101630 - B.md", "NOT_APPLICABLE")], caplog)
    assert len(rows) == 1 and any("duplicado" in r.message for r in caplog.records)


def test_not_applicable_without_code_counts_one_with_warning(tmp_path, caplog):
    rows = _collect(tmp_path, [("Projeto solto.md", "NOT_APPLICABLE")], caplog)
    assert len(rows) == 1 and any("sem chave de identidade" in r.message for r in caplog.records)


def test_real_ticket_unchanged(tmp_path, caplog):
    rows = _collect(tmp_path, [("TKT-1 - A.md", "1"), ("TKT-1 - B.md", "1"), ("TKT-2 - C.md", "2")], caplog)
    assert [r.ticket for r in rows] == ["2"]


def test_clean_owner_parenthesised_labels():
    assert clean_owner("Ana (ME)") == "Ana"
    assert clean_owner("(equipe/SYSTEM)") == ""
    assert clean_owner("Ana (Infra)") == "Ana (Infra)"


# --- cloud do template, duplicatas, prompt de decisões -------------------------------

from ata.obsidian import _resolve_cloud  # noqa: E402
from ata.summarize import _MAP_PROMPT, _REDUCE_PROMPT  # noqa: E402


def test_cloud_empty_one_and_full_template_set():
    assert _resolve_cloud({}, {"cloud_providers": []}) == ""
    assert _resolve_cloud({}, {"cloud_providers": ["AWS"]}) == "AWS"
    assert _resolve_cloud({}, {"cloud_providers": ["AWS", "Azure", "GCP", "OCI", "Huawei"]}) == ""
    assert _resolve_cloud({}, {"cloud_providers": ["AWS", "Azure"]}) == "AWS, Azure"
    assert _resolve_cloud({"cloud": "OCI"}, {"cloud_providers": ["AWS", "Azure", "GCP", "OCI", "Huawei"]}) == "OCI"


def test_normalize_payload_full_cloud_set_is_not_identified():
    out = normalize_payload({"cloud_providers": ["aws", "AZURE", "GCP", "oci", "Huawei"]})
    assert out["cloud_providers"] == []
    assert normalize_payload({"cloud_providers": ["OCI"]})["cloud_providers"] == ["OCI"]


def test_normalize_payload_dedupes_ignoring_case_space_and_trailing_punct():
    out = normalize_payload({
        "risks": ["Problemas com o namespace", "problemas  com o namespace.", "Outro", "Problemas com o namespace"],
        "participants": ["Ana", "ana", "Alan", "Alan "],
        "decisions": [{"description": "Usar OCI."}, "usar oci"],
        "other_notes": ["a", "A!"],
    })
    assert out["risks"] == ["Problemas com o namespace", "Outro"]
    assert out["participants"] == ["Ana", "Alan"]
    assert out["decisions"] == ["Usar OCI."] and out["other_notes"] == ["a"]


def test_prompts_require_evidence_for_decisions():
    assert "Aprovada" in _MAP_PROMPT and "other_notes" in _MAP_PROMPT.split("decisions:")[1].split("risks:")[0]
    assert "Aprovada" in _REDUCE_PROMPT


# --- fonte / confianca ----------------------------------------------------------------

def _front(meeting_extra):
    note = render_obsidian_note({**_MEETING, **meeting_extra}, {}, [], ObsidianConfig())
    return note.split("---")[1]


def test_audio_note_has_fonte_audio_and_empty_confianca():
    fm = _front({})
    assert "fonte: audio\n" in fm and "confianca: \n" in fm + "\n"
    assert "baixa" not in fm


def test_text_import_note_has_low_confidence():
    fm = _front({"fonte": "importacao-texto"})
    assert "fonte: importacao-texto" in fm and "confianca: baixa" in fm


def _confidence_dir(tmp_path, with_mark):
    d = tmp_path / ("a" if with_mark else "b"); d.mkdir()
    conf = "confianca: baixa\n" if with_mark else ""
    (d / "2026-08-25 - Hub.md").write_text(
        f"---\ndata: \"2026-08-25\"\nstatus: Em andamento\n{conf}---\n\n## 🚨 Riscos / Bloqueios\n\n- Risco X\n\n"
        "## 🚀 Ações / Próximos Passos\n\n- [ ] t — Responsável: Ana\n", encoding="utf-8")
    return d


def test_dashboard_marker_only_for_low_confidence_and_counters_equal(tmp_path):
    marked = render_dashboard(collect_meetings(_confidence_dir(tmp_path, True)), _NOW)
    plain = render_dashboard(collect_meetings(_confidence_dir(tmp_path, False)), _NOW)
    assert "[[2026-08-25 - Hub]] ⚠ fonte sem falantes" in marked
    assert "⚠" not in plain
    assert marked.replace(" ⚠ fonte sem falantes", "").split("\n", 2)[2] == plain.split("\n", 2)[2]


# --- Dashboard: saneamento só na exibição (notas antigas) --------------------------------

def _meeting_dir(tmp_path, body, front='data: "2026-09-22"\n'):
    d = tmp_path / "Reunioes"; d.mkdir(exist_ok=True)
    (d / "2026-09-22 - X.md").write_text(f"---\n{front}---\n\n{body}", encoding="utf-8")
    return d


def test_dashboard_empty_owner_label_does_not_leak_into_task(tmp_path):
    d = _meeting_dir(tmp_path, "## 🚀 Ações / Próximos Passos\n\n- [ ] Falar com o fornecedor — Responsável: \n"
                               "- [ ] Revisar — Responsável: Ana — Prazo: sexta\n- [ ] Ligar — Responsável:\n")
    acts = collect_meetings(d)[0].actions
    assert [(a.task, a.owner, a.deadline) for a in acts] == [
        ("Falar com o fornecedor", "", ""), ("Revisar", "Ana", "sexta"), ("Ligar", "", "")]
    dash = render_dashboard(collect_meetings(d), _NOW)
    assert "Responsável" not in dash


def test_dashboard_truncated_dict_repr_and_none_placeholder_in_risks(tmp_path):
    d = _meeting_dir(tmp_path, "## 🚨 Riscos / Bloqueios\n\n- 'topic': 'Cluster único', 'discussion': 'Precisa de ajuda'\n"
                               "- Nenhuma dúvida ou pendência\n- Nenhum plano de contingência definido\n")
    assert collect_meetings(d)[0].risk_lines == ["Cluster único: Precisa de ajuda",
                                                 "Nenhum plano de contingência definido"]


def test_dashboard_ignores_full_template_cloud_in_old_notes(tmp_path):
    d = _meeting_dir(tmp_path, "", front='data: "2026-09-22"\ncloud: "AWS, Azure, GCP, OCI, Huawei"\n')
    assert collect_meetings(d)[0].cloud == ""
    d2 = _meeting_dir(tmp_path, "", front='data: "2026-09-22"\ncloud: "AWS, OCI"\n')
    assert collect_meetings(d2)[0].cloud == "AWS, OCI"


def test_dashboard_owner_counts_ignore_confirm_marker(tmp_path):
    d = _meeting_dir(tmp_path, "## 🚀 Ações / Próximos Passos\n\n- [ ] a — Responsável: Ana\n"
                               "- [ ] b — Responsável: Ana [CONFIRMAR]\n- [ ] c — Responsável: [CONFIRMAR]\n")
    dash = render_dashboard(collect_meetings(d), _NOW)
    assert "**Por responsável:** Ana (2) · Sem responsável (1)" in dash
