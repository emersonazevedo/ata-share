"""Dashboard.md é view derivada e determinística — tudo aqui usa tmp_path como
vault fake, nenhum teste toca o vault real nem escreve fora de Dashboard.md."""

from datetime import datetime
from pathlib import Path

from ata.dashboard import (
    DashboardResult,
    PendingAction,
    ProjectRow,
    collect_meetings,
    collect_projects,
    parse_meeting_note,
    parse_project_note,
    rebuild_dashboard,
    render_dashboard,
)

_NOW = datetime(2026, 9, 15, 16, 0)


def _write_note(dir_: Path, filename: str, frontmatter: dict, body: str = "") -> Path:
    fm_lines = ["---"]
    for k, v in frontmatter.items():
        fm_lines.append(f'{k}: "{v}"' if v else f"{k}: ")
    fm_lines.append("---")
    text = "\n".join(fm_lines) + "\n\n" + body
    path = dir_ / filename
    path.write_text(text, encoding="utf-8")
    return path


_FULL_BODY = """# Título

## 🚨 Riscos / Bloqueios

- {risk}

## 🚀 Ações / Próximos Passos

{actions}
"""


def _note_body(risk="Nenhum identificado.", actions="- [ ] Nenhuma ação registrada."):
    return _FULL_BODY.format(risk=risk, actions=actions)


# --- fixture de Project Note (formato esperado) --

_PROJECT_RISK_PLACEHOLDER = "_Nenhum risco/bloqueio extraído nesta sincronização._"


def _write_project_note(
    dir_: Path,
    filename: str,
    *,
    ticket: str = "1000",
    cliente: str = "",
    cloud: tuple[str, ...] | None = ("OCI",),
    status: str = "Em andamento",
    checkpoint: str = "CP1",
    atualizado_em: str = "2026-09-15",
    riscos: str = _PROJECT_RISK_PLACEHOLDER,
    extra_frontmatter: str = "",
) -> Path:
    cloud_block = "cloud:\n" + "".join(f"  - {c}\n" for c in cloud) if cloud else "cloud:\n"
    fm = (
        "---\n"
        "tipo: projeto\n"
        f'ticket: "{ticket}"\n'
        f"cliente: {cliente}\n"
        f"{cloud_block}"
        f"status: {status}\n"
        f"checkpoint: {checkpoint}\n"
        "responsavel:\n"
        f"atualizado_em: '{atualizado_em}'\n"
        "updated_by: claude-code\n"
        "tags:\n  - reuniao\n  - projeto\n"
        f"{extra_frontmatter}"
        "---\n"
    )
    body = (
        f"\n# TKT-{ticket} - Projeto Teste\n\n"
        "## 🚨 Riscos / Bloqueios\n\n"
        "<!-- ATA:AUTO:BEGIN riscos -->\n"
        f"{riscos}\n"
        "<!-- ATA:AUTO:END riscos -->\n"
    )
    path = dir_ / filename
    path.write_text(fm + body, encoding="utf-8")
    return path


# --- parse_meeting_note / collect_meetings -----------------------------------

def test_collect_meetings_zero(tmp_path):
    assert collect_meetings(tmp_path / "Reunioes") == []  # nem existe ainda


def test_collect_meetings_one(tmp_path):
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    _write_note(reunioes, "2026-09-15 - Teste.md",
                {"data": "2026-09-15", "projeto": "", "ticket": "", "cloud": "AWS", "status": "Em andamento"},
                _note_body())
    rows = collect_meetings(reunioes)
    assert len(rows) == 1
    assert rows[0].filename == "2026-09-15 - Teste"
    assert rows[0].cloud == "AWS"


def test_collect_meetings_orders_most_recent_first(tmp_path):
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    _write_note(reunioes, "2026-09-01 - Antiga.md", {"data": "2026-09-01", "status": "Concluído"}, _note_body())
    _write_note(reunioes, "2026-09-20 - Nova.md", {"data": "2026-09-20", "status": "Em andamento"}, _note_body())
    _write_note(reunioes, "2026-09-10 - Meio.md", {"data": "2026-09-10", "status": "Em andamento"}, _note_body())
    rows = collect_meetings(reunioes)
    assert [r.filename for r in rows] == [
        "2026-09-20 - Nova", "2026-09-10 - Meio", "2026-09-01 - Antiga",
    ]


def test_frontmatter_incomplete_defaults_to_empty_not_crash(tmp_path):
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    path = reunioes / "2026-09-15 - Sem tudo.md"
    path.write_text("---\ndata: \"2026-09-15\"\nstatus: \"Em andamento\"\n---\n\n" + _note_body(),
                     encoding="utf-8")
    row = parse_meeting_note(path)
    assert row.project == "" and row.ticket == "" and row.cloud == ""
    assert row.status == "Em andamento"


def test_frontmatter_missing_entirely_does_not_crash(tmp_path):
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    path = reunioes / "2026-09-15 - Nota manual.md"
    path.write_text("# Nota sem frontmatter nenhum\n\nQualquer coisa aqui.", encoding="utf-8")
    row = parse_meeting_note(path)
    assert row.project == row.ticket == row.cloud == row.status == ""
    # sem frontmatter, cai pro prefixo de data do nome do arquivo
    assert row.sort_key == "2026-09-15"


def test_malformed_note_is_skipped_not_fatal(tmp_path, caplog):
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    _write_note(reunioes, "2026-09-15 - Boa.md", {"data": "2026-09-15", "status": "Em andamento"}, _note_body())
    bad = reunioes / "2026-09-14 - Ruim.md"
    bad.write_bytes(b"\xff\xfe\x00\xff data invalida em utf-8")  # não decodifica como utf-8
    rows = collect_meetings(reunioes)
    assert [r.filename for r in rows] == ["2026-09-15 - Boa"]  # a ruim foi ignorada, não travou nada


def test_action_checked_does_not_enter_pending(tmp_path):
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    body = _note_body(actions="- [ ] Tarefa aberta — Responsável: Ana\n- [x] Tarefa feita — Responsável: Bruno")
    _write_note(reunioes, "2026-09-15 - X.md", {"data": "2026-09-15"}, body)
    row = collect_meetings(reunioes)[0]
    assert [a.task for a in row.actions] == ["Tarefa aberta"]


def test_action_preserves_owner_and_deadline():
    from ata.dashboard import _parse_pending_actions
    text = "## 🚀 Ações / Próximos Passos\n\n- [ ] Validar sizing — Responsável: Ana — Prazo: sexta-feira\n"
    actions = _parse_pending_actions(text)
    assert actions == [PendingAction(task="Validar sizing", owner="Ana", deadline="sexta-feira")]


def test_action_without_owner_marker_keeps_whole_line_as_task():
    from ata.dashboard import _parse_pending_actions
    text = "## 🚀 Ações / Próximos Passos\n\n- [ ] Só uma tarefa solta\n"
    actions = _parse_pending_actions(text)
    assert actions[0].task == "Só uma tarefa solta" and actions[0].owner == ""


def _actions_block(dash: str) -> str:
    marker_idx = dash.find("🚀 Ações Pendentes")
    idx = dash.rfind("\n", 0, marker_idx) + 1  # volta pro início da linha (pega o "> [!todo]-")
    rest = dash[idx:]
    marker = rest.find("\n## 📅")
    return rest[:marker] if marker != -1 else rest


def test_actions_zero_shows_plain_heading_and_placeholder(tmp_path):
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    _write_note(reunioes, "2026-09-15 - X.md", {"data": "2026-09-15"},
                _note_body(actions="- [x] Só uma feita — Responsável: Ana"))
    rows = collect_meetings(reunioes)
    dash = render_dashboard(rows, _NOW)
    assert "## 🚀 Ações Pendentes\n\nNenhuma ação pendente." in dash
    assert "[!todo]" not in dash  # sem ações -> sem callout


def test_actions_one_uses_singular_and_is_a_closed_callout(tmp_path):
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    _write_note(reunioes, "2026-09-15 - X.md", {"data": "2026-09-15"},
                _note_body(actions="- [ ] Validar sizing — Responsável: Ana — Prazo: sexta-feira"))
    rows = collect_meetings(reunioes)
    dash = render_dashboard(rows, _NOW)
    block = _actions_block(dash)
    assert "> [!todo]- 🚀 Ações Pendentes — 1 ação" in block  # singular, "-" = fechado por padrão
    assert "1 ações" not in block
    assert "> - [ ] Validar sizing — Ana — Prazo: sexta-feira — [[2026-09-15 - X]]" in block


def test_actions_multiple_uses_plural(tmp_path):
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    _write_note(reunioes, "2026-09-15 - X.md", {"data": "2026-09-15"},
                _note_body(actions="- [ ] Tarefa 1 — Responsável: Ana\n- [ ] Tarefa 2 — Responsável: Bruno"))
    rows = collect_meetings(reunioes)
    dash = render_dashboard(rows, _NOW)
    block = _actions_block(dash)
    assert "> [!todo]- 🚀 Ações Pendentes — 2 ações" in block
    assert "Tarefa 1" in block and "Tarefa 2" in block


def test_risk_placeholder_is_not_a_real_risk(tmp_path):
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    _write_note(reunioes, "2026-09-15 - X.md", {"data": "2026-09-15", "status": "Em andamento"},
                _note_body(risk="Nenhum identificado."))
    row = collect_meetings(reunioes)[0]
    assert row.risk_lines == [] and not row.blocked


def _attention_block(dash: str) -> str:
    """Isola o bloco de Atenção (callout ou heading simples) até a próxima
    seção conhecida ('## 📅' quando 0 itens, senão o início de '> [!todo]')."""
    marker_idx = dash.find("🚨 Atenção")
    idx = dash.rfind("\n", 0, marker_idx) + 1  # volta pro início da linha (pega o "> [!warning]-")
    rest = dash[idx:]
    for marker in ("\n> [!todo]", "\n## 📅"):
        m_idx = rest.find(marker)
        if m_idx != -1:
            return rest[:m_idx]
    return rest


def test_attention_zero_items_shows_plain_heading_and_placeholder(tmp_path):
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    _write_note(reunioes, "2026-09-15 - X.md", {"data": "2026-09-15", "status": "Em andamento"}, _note_body())
    rows = collect_meetings(reunioes)
    dash = render_dashboard(rows, _NOW)
    assert "## 🚨 Atenção\n\nNenhum bloqueio ou risco identificado." in dash
    assert "[!warning]" not in dash  # sem itens -> sem callout


def test_attention_one_item_uses_singular_and_is_a_closed_callout(tmp_path):
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    _write_note(reunioes, "2026-09-15 - X.md", {"data": "2026-09-15", "status": "Em andamento"},
                _note_body(risk="Atraso do fornecedor"))
    row = collect_meetings(reunioes)[0]
    assert row.risk_lines == ["Atraso do fornecedor"]
    dash = render_dashboard([row], _NOW)
    block = _attention_block(dash)
    assert "> [!warning]- 🚨 Atenção — 1 item" in block  # singular, "-" = fechado por padrão
    assert "1 itens" not in block
    assert "> - [[2026-09-15 - X]]" in block


def test_attention_multiple_items_uses_plural(tmp_path):
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    _write_note(reunioes, "2026-09-15 - A.md", {"data": "2026-09-15", "status": "Bloqueado"}, _note_body())
    _write_note(reunioes, "2026-09-14 - B.md", {"data": "2026-09-14", "status": "Em andamento"},
                _note_body(risk="Risco real"))
    rows = collect_meetings(reunioes)
    dash = render_dashboard(rows, _NOW)
    block = _attention_block(dash)
    assert "> [!warning]- 🚨 Atenção — 2 itens" in block
    assert "[[2026-09-15 - A]]" in block and "Bloqueado" in block
    assert "[[2026-09-14 - B]]" in block and "Risco real" in block


def test_status_bloqueado_flags_meeting_for_attention(tmp_path):
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    _write_note(reunioes, "2026-09-15 - X.md", {"data": "2026-09-15", "status": "Bloqueado"}, _note_body())
    row = collect_meetings(reunioes)[0]
    assert row.blocked is True
    dash = render_dashboard([row], _NOW)
    block = _attention_block(dash)
    assert "[[2026-09-15 - X]]" in block and "Bloqueado" in block


def test_no_attention_shows_placeholder(tmp_path):
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    _write_note(reunioes, "2026-09-15 - X.md", {"data": "2026-09-15", "status": "Em andamento"}, _note_body())
    rows = collect_meetings(reunioes)
    dash = render_dashboard(rows, _NOW)
    assert "Nenhum bloqueio ou risco identificado." in dash


def test_cloud_aggregation_counts_exact_values(tmp_path):
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    _write_note(reunioes, "2026-09-01 - A.md", {"data": "2026-09-01", "cloud": "AWS"}, _note_body())
    _write_note(reunioes, "2026-09-02 - B.md", {"data": "2026-09-02", "cloud": "AWS"}, _note_body())
    _write_note(reunioes, "2026-09-03 - C.md", {"data": "2026-09-03", "cloud": "Azure"}, _note_body())
    _write_note(reunioes, "2026-09-04 - D.md", {"data": "2026-09-04", "cloud": ""}, _note_body())
    rows = collect_meetings(reunioes)
    dash = render_dashboard(rows, _NOW)
    clouds = dash.split("## ☁️ Clouds")[1]
    assert "- AWS: 2 reuniões" in clouds
    assert "- Azure: 1 reunião" in clouds  # singular


def test_cloud_never_split_or_invented(tmp_path):
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    _write_note(reunioes, "2026-09-01 - A.md",
                {"data": "2026-09-01", "cloud": "AWS (mencionadas também Azure, GCP e OCI)"}, _note_body())
    rows = collect_meetings(reunioes)
    dash = render_dashboard(rows, _NOW)
    clouds = dash.split("## ☁️ Clouds")[1]
    assert "- AWS (mencionadas também Azure, GCP e OCI): 1 reunião" in clouds
    assert "- Azure:" not in clouds and "- GCP:" not in clouds  # nunca fabricar contagem separada


def test_wikilinks_use_real_basename_with_special_chars(tmp_path):
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    _write_note(reunioes, "2026-09-10 - AWS Exemplo - Possibilidade de uso.md",
                {"data": "2026-09-10"}, _note_body())
    rows = collect_meetings(reunioes)
    dash = render_dashboard(rows, _NOW)
    assert "[[2026-09-10 - AWS Exemplo - Possibilidade de uso]]" in dash


def test_last_meetings_table_ordering_and_dash_for_missing(tmp_path):
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    _write_note(reunioes, "2026-09-15 - X.md",
                {"data": "2026-09-15", "projeto": "", "cloud": "AWS", "status": "Em andamento"}, _note_body())
    rows = collect_meetings(reunioes)
    table = render_dashboard(rows, _NOW).split("## 📅 Últimas Reuniões")[1]
    assert "| 15/09/2026 | [[2026-09-15 - X]] | — | AWS | Em andamento |" in table


def _synthetic_rows(n: int, *, with_action_and_risk: bool = False):
    from ata.dashboard import MeetingRow, PendingAction
    rows = []
    for i in range(n):
        date = f"2026-01-{i + 1:02d}"
        kwargs = dict(filename=f"m{i}", date_display=date, sort_key=date, cloud="AWS", status="Em andamento")
        if with_action_and_risk:
            kwargs["actions"] = [PendingAction(task=f"t{i}")]
            kwargs["risk_lines"] = [f"risco {i}"]
        rows.append(MeetingRow(**kwargs))
    rows.sort(key=lambda r: (r.sort_key, r.filename), reverse=True)
    return rows


def test_table_shows_all_10_meetings_without_truncation_note(tmp_path):
    rows = _synthetic_rows(10)
    dash = render_dashboard(rows, _NOW)
    table = dash.split("## 📅 Últimas Reuniões")[1]
    for i in range(10):
        assert f"[[m{i}]]" in table
    assert "Exibindo as" not in table


def test_table_with_11_meetings_shows_only_10_most_recent_with_note(tmp_path):
    rows = _synthetic_rows(11)  # m10 = 2026-01-11 é a mais recente
    dash = render_dashboard(rows, _NOW)
    table = dash.split("## 📅 Últimas Reuniões")[1]
    assert "[[m0]]" not in table  # 2026-01-01, a mais antiga, fica de fora
    for i in range(1, 11):
        assert f"[[m{i}]]" in table
    assert "> Exibindo as 10 reuniões mais recentes de 11 reuniões." in table


def test_global_counters_consider_all_meetings_even_when_table_is_truncated():
    rows = _synthetic_rows(11, with_action_and_risk=True)
    dash = render_dashboard(rows, _NOW)

    overview = dash.split("## 📊 Visão Geral")[1].split("> [!warning]")[0]
    assert "- Reuniões: 11" in overview
    assert "- Em andamento: 11" in overview
    assert "- Ações pendentes: 11" in overview

    assert "> [!warning]- 🚨 Atenção — 11 itens" in dash  # todas as 11, não só as 10 da tabela
    assert "> [!todo]- 🚀 Ações Pendentes — 11 ações" in dash

    clouds = dash.split("## ☁️ Clouds")[1]
    assert "- AWS: 11 reuniões" in clouds


def test_visao_geral_counts():
    from ata.dashboard import MeetingRow, PendingAction
    rows = [
        MeetingRow(filename="a", date_display="2026-09-01", sort_key="2026-09-01", status="Em andamento",
                   actions=[PendingAction(task="t1")]),
        MeetingRow(filename="b", date_display="2026-09-02", sort_key="2026-09-02", status="Bloqueado"),
        MeetingRow(filename="c", date_display="2026-09-03", sort_key="2026-09-03", status="Concluído"),
        MeetingRow(filename="d", date_display="2026-09-04", sort_key="2026-09-04", status="Em análise"),
    ]
    dash = render_dashboard(rows, _NOW)
    overview = dash.split("## 📊 Visão Geral")[1].split("## 🚨")[0]
    assert "- Reuniões: 4" in overview
    assert "- Em andamento: 1" in overview
    assert "- Bloqueadas: 1" in overview
    assert "- Concluídas: 1" in overview
    assert "- Ações pendentes: 1" in overview


# --- rebuild_dashboard (I/O) ---------------------------------------------

def test_rebuild_skipped_when_disabled(tmp_path):
    result = rebuild_dashboard(tmp_path / "Reunioes", tmp_path / "Dashboard/Dashboard.md", enabled=False)
    assert result.status == "skipped"
    assert not (tmp_path / "Dashboard").exists()


def test_rebuild_failed_when_paths_none():
    result = rebuild_dashboard(None, None, enabled=True)
    assert result.status == "failed" and result.error


def test_rebuild_writes_file_and_counts_meetings(tmp_path):
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    _write_note(reunioes, "2026-09-15 - X.md", {"data": "2026-09-15"}, _note_body())
    dashboard_path = tmp_path / "Dashboard" / "Dashboard.md"

    result = rebuild_dashboard(reunioes, dashboard_path, enabled=True, now=_NOW)

    assert result.status == "ok"
    assert result.meetings_count == 1
    assert dashboard_path.is_file()
    assert "# 🏠 Dashboard" in dashboard_path.read_text(encoding="utf-8")


def test_rebuild_is_idempotent(tmp_path):
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    _write_note(reunioes, "2026-09-15 - X.md", {"data": "2026-09-15", "cloud": "AWS"}, _note_body())
    dashboard_path = tmp_path / "Dashboard" / "Dashboard.md"

    r1 = rebuild_dashboard(reunioes, dashboard_path, enabled=True, now=_NOW)
    content1 = dashboard_path.read_text(encoding="utf-8")
    r2 = rebuild_dashboard(reunioes, dashboard_path, enabled=True, now=_NOW)
    content2 = dashboard_path.read_text(encoding="utf-8")

    assert r1.status == "ok" and r2.status == "ok"
    assert content1 == content2  # mesmo input + mesmo `now` -> byte a byte igual


def test_rebuild_only_writes_the_dashboard_file(tmp_path):
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    _write_note(reunioes, "2026-09-15 - X.md", {"data": "2026-09-15"}, _note_body())
    dashboard_dir = tmp_path / "Dashboard"
    dashboard_path = dashboard_dir / "Dashboard.md"

    rebuild_dashboard(reunioes, dashboard_path, enabled=True, now=_NOW)

    assert [p.name for p in dashboard_dir.iterdir()] == ["Dashboard.md"]  # nada de arquivo .tmp sobrando
    assert [p.name for p in reunioes.iterdir()] == ["2026-09-15 - X.md"]  # Reunioes/ intacto


def test_rebuild_atomic_write_leaves_no_partial_or_temp_file_on_replace_error(tmp_path, monkeypatch):
    """Simula falha no passo final (os.replace) — depois de já ter escrito o
    .tmp — e confirma que nada fica pela metade: nem Dashboard.md corrompido,
    nem arquivo .tmp* esquecido no diretório."""
    import ata.dashboard as dashboard_mod
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    _write_note(reunioes, "2026-09-15 - X.md", {"data": "2026-09-15"}, _note_body())
    dashboard_dir = tmp_path / "Dashboard"
    dashboard_path = dashboard_dir / "Dashboard.md"

    def _boom(*a, **k):
        raise OSError("falha simulada no os.replace")

    monkeypatch.setattr(dashboard_mod.os, "replace", _boom)
    result = rebuild_dashboard(reunioes, dashboard_path, enabled=True, now=_NOW)

    assert result.status == "failed"
    assert not dashboard_path.exists()
    assert list(dashboard_dir.glob("*.tmp*")) == []


def test_rebuild_dashboard_result_shape():
    r = DashboardResult(status="ok", path="x", error=None, meetings_count=3)
    assert r.status == "ok" and r.meetings_count == 3


# ==============================================================================
# Fase 3 — Projetos/ (Project Notes mantidas fora do ata)
# ==============================================================================

# --- parse_project_note / collect_projects -----------------------------------

def test_collect_projects_zero(tmp_path):
    # 1. zero projetos
    assert collect_projects(tmp_path / "Projetos") == []  # pasta nem existe


def test_collect_projects_one_em_andamento(tmp_path):
    # 2. um projeto Em andamento
    projetos = tmp_path / "Projetos"; projetos.mkdir()
    _write_project_note(projetos, "TKT-1000 - X.md", status="Em andamento")
    rows = collect_projects(projetos)
    assert len(rows) == 1
    assert rows[0].status == "Em andamento"
    assert rows[0].ticket == "1000"


def test_collect_projects_bloqueado(tmp_path):
    # 3. projeto Bloqueado
    projetos = tmp_path / "Projetos"; projetos.mkdir()
    _write_project_note(projetos, "TKT-1000 - X.md", status="Bloqueado")
    row = collect_projects(projetos)[0]
    assert row.status == "Bloqueado"


def test_collect_projects_concluido(tmp_path):
    # 4. projeto Concluído
    projetos = tmp_path / "Projetos"; projetos.mkdir()
    _write_project_note(projetos, "TKT-1000 - X.md", status="Concluído")
    row = collect_projects(projetos)[0]
    assert row.status == "Concluído"


def test_collect_projects_status_desconhecido_nao_derruba(tmp_path, caplog):
    # 5. status desconhecido: preservado como está, warning, não inventa nem quebra
    projetos = tmp_path / "Projetos"; projetos.mkdir()
    _write_project_note(projetos, "TKT-1000 - X.md", status="Pausado")
    with caplog.at_level("WARNING"):
        rows = collect_projects(projetos)
    assert len(rows) == 1
    assert rows[0].status == "Pausado"  # preservado, não convertido silenciosamente
    assert not rows[0].status_known
    assert any("desconhecido" in r.message for r in caplog.records)


def test_project_note_malformada_e_pulada_nao_derruba_as_outras(tmp_path):
    # 6. Project Note malformada -> skip + warning, resto continua
    projetos = tmp_path / "Projetos"; projetos.mkdir()
    _write_project_note(projetos, "TKT-1000 - Boa.md", ticket="1000")
    bad = projetos / "TKT-2000 - Ruim.md"
    bad.write_bytes(b"\xff\xfe\x00\xff frontmatter invalido em utf-8")
    rows = collect_projects(projetos)
    assert [r.ticket for r in rows] == ["1000"]


def test_ticket_ausente_conta_com_aviso(tmp_path):
    # 7. Ticket ausente -> não inventa identidade, exclui
    projetos = tmp_path / "Projetos"; projetos.mkdir()
    path = projetos / "Sem Ticket.md"
    path.write_text(
        "---\ntipo: projeto\nticket: \ncliente: \ncloud:\n  - OCI\nstatus: Em andamento\n---\n\n# Sem Ticket\n",
        encoding="utf-8",
    )
    assert parse_project_note(path).key == ""
    assert len(collect_projects(projetos)) == 1  # sem chave: conta + aviso (nunca descarta)


def test_cloud_lista_com_uma_cloud(tmp_path):
    # 8. cloud lista com uma cloud
    projetos = tmp_path / "Projetos"; projetos.mkdir()
    _write_project_note(projetos, "TKT-1000 - X.md", cloud=("OCI",))
    assert collect_projects(projetos)[0].cloud == ["OCI"]


def test_projeto_multicloud(tmp_path):
    # 9. projeto multicloud
    projetos = tmp_path / "Projetos"; projetos.mkdir()
    _write_project_note(projetos, "TKT-1000 - X.md", cloud=("AWS", "Azure"))
    assert collect_projects(projetos)[0].cloud == ["AWS", "Azure"]


def test_reuniao_cloud_scalar_continua_funcionando(tmp_path):
    # 10. reunião cloud scalar (schema antigo) não foi afetada pela Fase 3
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    _write_note(reunioes, "2026-09-15 - X.md", {"data": "2026-09-15", "cloud": "AWS"}, _note_body())
    assert collect_meetings(reunioes)[0].cloud == "AWS"


# --- Projetos Ativos ----------------------------------------------------------

def test_projetos_ativos_exclui_concluido(tmp_path):
    # 11. Projetos Ativos exclui Concluído
    projetos = tmp_path / "Projetos"; projetos.mkdir()
    _write_project_note(projetos, "TKT-1000 - A.md", ticket="1000", status="Em andamento")
    _write_project_note(projetos, "TKT-2000 - B.md", ticket="2000", status="Concluído")
    rows = collect_projects(projetos)
    dash = render_dashboard([], _NOW, rows)
    active = dash.split("## 📁 Projetos Ativos")[1].split("## 🕘")[0]
    assert "TKT-1000 - A" in active
    assert "TKT-2000 - B" not in active


def test_projetos_ativos_ordena_bloqueado_primeiro(tmp_path):
    # 12. Projetos Ativos ordena Bloqueado primeiro
    projetos = tmp_path / "Projetos"; projetos.mkdir()
    _write_project_note(projetos, "TKT-1000 - A.md", ticket="1000", status="Em andamento", atualizado_em="2026-09-15")
    _write_project_note(projetos, "TKT-2000 - B.md", ticket="2000", status="Bloqueado", atualizado_em="2026-09-10")
    rows = collect_projects(projetos)
    dash = render_dashboard([], _NOW, rows)
    active = dash.split("## 📁 Projetos Ativos")[1].split("## 🕘")[0]
    assert active.find("TKT-2000 - B") < active.find("TKT-1000 - A")  # Bloqueado antes mesmo sendo mais antigo


def test_projetos_ativos_zero_mostra_placeholder(tmp_path):
    dash = render_dashboard([], _NOW, [])
    assert "## 📁 Projetos Ativos\n\nNenhum projeto ativo." in dash


# --- Atenção combinada ---------------------------------------------------------

def test_risco_de_project_note_entra_em_atencao(tmp_path):
    # 13. risco de Project Note entra em Atenção
    projetos = tmp_path / "Projetos"; projetos.mkdir()
    _write_project_note(projetos, "TKT-1000 - X.md", riscos="- Divergência relevante identificada na validação de preços.")
    rows = collect_projects(projetos)
    dash = render_dashboard([], _NOW, rows)
    assert "Divergência relevante identificada na validação de preços." in dash
    assert "> [!warning]- 🚨 Atenção — 1 item" in dash


def test_projeto_sem_risco_nao_entra_em_atencao(tmp_path):
    # 14. projeto sem risco não entra em Atenção
    projetos = tmp_path / "Projetos"; projetos.mkdir()
    _write_project_note(projetos, "TKT-1000 - X.md", status="Em andamento", riscos=_PROJECT_RISK_PLACEHOLDER)
    rows = collect_projects(projetos)
    dash = render_dashboard([], _NOW, rows)
    assert "Nenhum bloqueio ou risco identificado." in dash


def test_placeholder_vazio_nao_entra_em_atencao(tmp_path):
    # 15. placeholder/vazio não entra em Atenção (mesmo com texto diferente, formato itálico)
    projetos = tmp_path / "Projetos"; projetos.mkdir()
    _write_project_note(projetos, "TKT-1000 - X.md", riscos="_Sem evidência clara extraída do README._")
    row = collect_projects(projetos)[0]
    assert row.risk_lines == []


def test_atencao_diferencia_projeto_e_reuniao(tmp_path):
    # 16. Atenção diferencia Projeto/Reunião — cada grupo com seu próprio rótulo
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    _write_note(reunioes, "2026-09-15 - R.md", {"data": "2026-09-15", "status": "Bloqueado"}, _note_body())
    projetos = tmp_path / "Projetos"; projetos.mkdir()
    _write_project_note(projetos, "TKT-1000 - P.md", status="Bloqueado")

    meetings = collect_meetings(reunioes)
    projects = collect_projects(projetos)
    dash = render_dashboard(meetings, _NOW, projects)

    assert "> [!warning]- 🚨 Atenção — 2 itens" in dash
    assert "> **Projetos:**" in dash and "> **Reuniões:**" in dash
    projetos_idx = dash.find("> **Projetos:**")
    reunioes_idx = dash.find("> **Reuniões:**")
    projeto_item_idx = dash.find("[[TKT-1000 - P]]")
    reuniao_item_idx = dash.find("[[2026-09-15 - R]]")
    assert projetos_idx < projeto_item_idx < reunioes_idx  # item de projeto cai no grupo certo
    assert reunioes_idx < reuniao_item_idx


# --- Ações Pendentes: só Reunioes/ ---------------------------------------------

def test_acoes_pendentes_continuam_apenas_de_reunioes(tmp_path):
    # 17. ações pendentes continuam apenas de Reunioes/ — Project Note não vira checkbox global
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    _write_note(reunioes, "2026-09-15 - R.md", {"data": "2026-09-15"},
                _note_body(actions="- [ ] Tarefa da reunião"))
    projetos = tmp_path / "Projetos"; projetos.mkdir()
    _write_project_note(projetos, "TKT-1000 - P.md")  # Próximos Passos da Project Note não é lido aqui

    meetings = collect_meetings(reunioes)
    projects = collect_projects(projetos)
    dash = render_dashboard(meetings, _NOW, projects)
    actions_block = dash.split("🚀 Ações Pendentes")[1].split("## 📅")[0]
    assert "Tarefa da reunião" in actions_block
    assert "TKT-1000" not in actions_block


# --- Projetos Recentes ----------------------------------------------------------

def test_projetos_recentes_limitado_a_10(tmp_path):
    # 18. Projetos Recentes limitado a 10
    projetos = tmp_path / "Projetos"; projetos.mkdir()
    for i in range(11):
        _write_project_note(
            projetos, f"TKT-{1000 + i} - P{i}.md", ticket=str(1000 + i), atualizado_em=f"2026-09-{i + 1:02d}",
        )
    rows = collect_projects(projetos)
    dash = render_dashboard([], _NOW, rows)
    recentes = dash.split("## 🕘 Projetos Recentes")[1].split("## ☁️")[0]
    assert "> Exibindo os 10 projetos mais recentes de 11 projetos." in recentes
    assert "TKT-1000 - P0" not in recentes  # 2026-09-01, a mais antiga, fica de fora
    assert "TKT-1010 - P10" in recentes     # 2026-09-11, a mais recente, entra


def test_projetos_recentes_ordena_por_atualizado_em_desc(tmp_path):
    # 19. ordenação por atualizado_em, mais recente primeiro
    projetos = tmp_path / "Projetos"; projetos.mkdir()
    _write_project_note(projetos, "TKT-1000 - Antigo.md", ticket="1000", atualizado_em="2026-09-01")
    _write_project_note(projetos, "TKT-2000 - Novo.md", ticket="2000", atualizado_em="2026-09-15")
    rows = collect_projects(projetos)
    dash = render_dashboard([], _NOW, rows)
    recentes = dash.split("## 🕘 Projetos Recentes")[1].split("## ☁️")[0]
    assert recentes.find("TKT-2000 - Novo") < recentes.find("TKT-1000 - Antigo")


def test_projetos_recentes_zero_mostra_placeholder(tmp_path):
    dash = render_dashboard([], _NOW, [])
    recentes = dash.split("## 🕘 Projetos Recentes")[1].split("## ☁️")[0]
    assert "_Nenhum projeto registrado._" in recentes


# --- Clouds ---------------------------------------------------------------------

def test_clouds_separa_reunioes_e_projetos(tmp_path):
    # 20. Clouds separa Reuniões/Projetos
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    _write_note(reunioes, "2026-09-15 - R.md", {"data": "2026-09-15", "cloud": "AWS"}, _note_body())
    projetos = tmp_path / "Projetos"; projetos.mkdir()
    _write_project_note(projetos, "TKT-1000 - P.md", cloud=("OCI",))

    meetings = collect_meetings(reunioes)
    projects = collect_projects(projetos)
    dash = render_dashboard(meetings, _NOW, projects)
    clouds = dash.split("## ☁️ Clouds")[1]
    assert "**Reuniões:**" in clouds and "- AWS: 1 reunião" in clouds
    assert "**Projetos:**" in clouds and "- OCI: 1 projeto" in clouds
    assert "- OCI: 1 projeto" not in clouds.split("**Projetos:**")[0]  # não vazou pro bloco de reuniões


def test_projeto_multicloud_nao_duplica_total_global(tmp_path):
    # 21. projeto multicloud conta 1x por cloud na tabela, mas total global (Visão Geral) continua 1
    projetos = tmp_path / "Projetos"; projetos.mkdir()
    _write_project_note(projetos, "TKT-1000 - P.md", cloud=("AWS", "OCI"))
    rows = collect_projects(projetos)
    dash = render_dashboard([], _NOW, rows)

    overview = dash.split("### Projetos")[1].split("> [!warning]")[0]
    assert "- Projetos: 1" in overview

    clouds = dash.split("## ☁️ Clouds")[1]
    assert "- AWS: 1 projeto" in clouds
    assert "- OCI: 1 projeto" in clouds


# --- Visão Geral separada -------------------------------------------------------

def test_visao_geral_separa_reunioes_e_projetos(tmp_path):
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    _write_note(reunioes, "2026-09-15 - R.md", {"data": "2026-09-15", "status": "Em andamento"}, _note_body())
    projetos = tmp_path / "Projetos"; projetos.mkdir()
    _write_project_note(projetos, "TKT-1000 - P.md", status="Bloqueado")

    meetings = collect_meetings(reunioes)
    projects = collect_projects(projetos)
    dash = render_dashboard(meetings, _NOW, projects)

    reunioes_block = dash.split("### Reuniões")[1].split("### Projetos")[0]
    projetos_block = dash.split("### Projetos")[1].split("> [!warning]")[0]
    assert "- Reuniões: 1" in reunioes_block and "- Em andamento: 1" in reunioes_block
    assert "- Projetos: 1" in projetos_block and "- Bloqueados: 1" in projetos_block
    assert "- Em andamento: 1" not in projetos_block  # reunião "Em andamento" não vira projeto "Em andamento"


# --- Ticket duplicado -----------------------------------------------------------

def test_ticket_duplicado_gera_warning_e_nao_conta_como_dois(tmp_path, caplog):
    # 27. Ticket duplicado -> warning, nenhuma das duas é contada
    projetos = tmp_path / "Projetos"; projetos.mkdir()
    _write_project_note(projetos, "TKT-1000 - A.md", ticket="1000")
    _write_project_note(projetos, "TKT-1000 - B (dup).md", ticket="1000")
    _write_project_note(projetos, "TKT-2000 - C.md", ticket="2000")
    with caplog.at_level("WARNING"):
        rows = collect_projects(projetos)
    assert [r.ticket for r in rows] == ["2000"]  # só a não-conflitante sobrevive
    assert any("duplicado" in r.message for r in caplog.records)


# --- rebuild real: idempotência, escopo de escrita, wikilink -------------------

def test_rebuild_nao_modifica_project_notes(tmp_path):
    # 22. rebuild não modifica Project Notes
    projetos = tmp_path / "Projetos"; projetos.mkdir()
    note_path = _write_project_note(projetos, "TKT-1000 - X.md")
    before = note_path.read_bytes()
    dashboard_path = tmp_path / "Dashboard" / "Dashboard.md"

    rebuild_dashboard(tmp_path / "Reunioes", dashboard_path, projetos, enabled=True, now=_NOW)

    assert note_path.read_bytes() == before


def test_rebuild_nao_modifica_meeting_notes(tmp_path):
    # 23. rebuild não modifica Meeting Notes
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    note_path = _write_note(reunioes, "2026-09-15 - X.md", {"data": "2026-09-15"}, _note_body())
    before = note_path.read_bytes()
    projetos = tmp_path / "Projetos"; projetos.mkdir()
    dashboard_path = tmp_path / "Dashboard" / "Dashboard.md"

    rebuild_dashboard(reunioes, dashboard_path, projetos, enabled=True, now=_NOW)

    assert note_path.read_bytes() == before


def test_rebuild_com_projetos_e_idempotente(tmp_path):
    # 24. segundo rebuild não duplica conteúdo
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    _write_note(reunioes, "2026-09-15 - X.md", {"data": "2026-09-15"}, _note_body())
    projetos = tmp_path / "Projetos"; projetos.mkdir()
    _write_project_note(projetos, "TKT-1000 - X.md")
    dashboard_path = tmp_path / "Dashboard" / "Dashboard.md"

    r1 = rebuild_dashboard(reunioes, dashboard_path, projetos, enabled=True, now=_NOW)
    content1 = dashboard_path.read_text(encoding="utf-8")
    r2 = rebuild_dashboard(reunioes, dashboard_path, projetos, enabled=True, now=_NOW)
    content2 = dashboard_path.read_text(encoding="utf-8")

    assert r1.status == "ok" and r2.status == "ok"
    assert r1.projects_count == 1 and r2.projects_count == 1
    assert content1 == content2


def test_rebuild_falha_de_uma_project_note_nao_impede_as_outras(tmp_path):
    # 25. falha/malformed de uma Project Note não impede outras
    projetos = tmp_path / "Projetos"; projetos.mkdir()
    _write_project_note(projetos, "TKT-1000 - Boa.md", ticket="1000")
    (projetos / "TKT-2000 - Ruim.md").write_bytes(b"\xff\xfe\x00\xff invalido")
    dashboard_path = tmp_path / "Dashboard" / "Dashboard.md"

    result = rebuild_dashboard(tmp_path / "Reunioes", dashboard_path, projetos, enabled=True, now=_NOW)

    assert result.status == "ok"
    assert result.projects_count == 1


def test_wikilink_de_projeto_usa_basename_real(tmp_path):
    # 26. wikilink usa basename real (nome do arquivo, não reconstrução do ticket+nome)
    projetos = tmp_path / "Projetos"; projetos.mkdir()
    _write_project_note(projetos, "TKT-1000 - Nome Com Espaços & Acentuação.md", ticket="1000")
    rows = collect_projects(projetos)
    dash = render_dashboard([], _NOW, rows)
    assert "[[TKT-1000 - Nome Com Espaços & Acentuação]]" in dash


def test_rebuild_only_writes_dashboard_with_projetos_configured(tmp_path):
    # confirma escopo de escrita: só Dashboard.md, Reunioes/ e Projetos/ intactos
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    _write_note(reunioes, "2026-09-15 - X.md", {"data": "2026-09-15"}, _note_body())
    projetos = tmp_path / "Projetos"; projetos.mkdir()
    _write_project_note(projetos, "TKT-1000 - X.md")
    dashboard_dir = tmp_path / "Dashboard"
    dashboard_path = dashboard_dir / "Dashboard.md"

    rebuild_dashboard(reunioes, dashboard_path, projetos, enabled=True, now=_NOW)

    assert [p.name for p in dashboard_dir.iterdir()] == ["Dashboard.md"]
    assert [p.name for p in reunioes.iterdir()] == ["2026-09-15 - X.md"]
    assert [p.name for p in projetos.iterdir()] == ["TKT-1000 - X.md"]


def test_rebuild_sem_projetos_path_trata_como_zero_projetos(tmp_path):
    """Compatibilidade: quem ainda chama sem `projetos_path` continua funcionando."""
    reunioes = tmp_path / "Reunioes"; reunioes.mkdir()
    _write_note(reunioes, "2026-09-15 - X.md", {"data": "2026-09-15"}, _note_body())
    dashboard_path = tmp_path / "Dashboard" / "Dashboard.md"

    result = rebuild_dashboard(reunioes, dashboard_path, enabled=True, now=_NOW)

    assert result.status == "ok"
    assert result.projects_count == 0
