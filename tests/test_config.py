from ata.config import load_config


def test_defaults(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.asr.model == "large-v3-turbo"
    assert cfg.asr.beam_size == 1
    assert cfg.ollama.model == "llama3.2:3b"
    assert cfg.server.port == 8756


def test_toml_override(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        '[asr]\nbeam_size = 3\ncpu_threads = 8\n[ollama]\nmodel = "qwen2.5:7b"\n',
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.asr.beam_size == 3
    assert cfg.asr.cpu_threads == 8
    assert cfg.ollama.model == "qwen2.5:7b"


def test_env_override_beats_file(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text('[asr]\nbeam_size = 3\n', encoding="utf-8")
    monkeypatch.setenv("ATA_ASR_BEAM_SIZE", "5")
    monkeypatch.setenv("ATA_SERVER_PORT", "9000")
    cfg = load_config(p)
    assert cfg.asr.beam_size == 5
    assert cfg.server.port == 9000


def test_obsidian_defaults_are_safe(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.obsidian.enabled is False
    assert cfg.obsidian.vault_path == ""
    assert cfg.obsidian.meetings_folder == "Reunioes"
    assert cfg.obsidian.auto_export is True
    assert cfg.obsidian.default_status == "Em andamento"
    assert cfg.obsidian_meetings_path is None  # sem vault_path -> não impede o app de funcionar


def test_obsidian_toml_override(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        f'[obsidian]\nenabled = true\nvault_path = "{tmp_path.as_posix()}"\n'
        'meetings_folder = "Reunioes"\nauto_export = false\n',
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.obsidian.enabled is True
    assert cfg.obsidian.auto_export is False
    assert cfg.obsidian_meetings_path == tmp_path / "Reunioes"


def test_obsidian_meetings_path_none_when_vault_missing(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[obsidian]\nenabled = true\nvault_path = "Z:/nao/existe/de/verdade"\n', encoding="utf-8")
    cfg = load_config(p)
    assert cfg.obsidian_meetings_path is None


def test_dashboard_defaults_are_safe(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.obsidian.dashboard_enabled is True
    assert cfg.obsidian.dashboard_folder == "Dashboard"
    assert cfg.obsidian.dashboard_filename == "Dashboard.md"
    assert cfg.obsidian_dashboard_path is None  # sem vault_path -> não impede o app de funcionar


def test_obsidian_dashboard_path_resolves_under_vault(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(f'[obsidian]\nenabled = true\nvault_path = "{tmp_path.as_posix()}"\n', encoding="utf-8")
    cfg = load_config(p)
    assert cfg.obsidian_dashboard_path == tmp_path / "Dashboard" / "Dashboard.md"


def test_obsidian_dashboard_path_none_when_vault_missing(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[obsidian]\nenabled = true\nvault_path = "Z:/nao/existe/de/verdade"\n', encoding="utf-8")
    cfg = load_config(p)
    assert cfg.obsidian_dashboard_path is None


def test_dashboard_folder_toml_override(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        f'[obsidian]\nenabled = true\nvault_path = "{tmp_path.as_posix()}"\n'
        'dashboard_enabled = false\ndashboard_folder = "Painel"\ndashboard_filename = "Home.md"\n',
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.obsidian.dashboard_enabled is False
    assert cfg.obsidian_dashboard_path == tmp_path / "Painel" / "Home.md"
