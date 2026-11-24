"""Configuração central. Defaults sensatos, override por config.toml e por env ATA_*."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8756
    open_browser: bool = True
    open_folder_on_done: bool = True   # abre a pasta da reunião no Explorer ao finalizar


@dataclass(frozen=True)
class GuardrailsConfig:
    max_recording_minutes: int = 25     # 0 = sem limite
    warn_before_seconds: int = 60       # aviso (popup + bip) antes do corte automático
    beep: bool = True                   # bip no servidor (Windows); funciona com a aba minimizada


@dataclass(frozen=True)
class PathsConfig:
    data_dir: str = "./data"
    model_cache_dir: str = "./models"


@dataclass(frozen=True)
class AsrConfig:
    model: str = "large-v3-turbo"
    device: str = "cpu"
    compute_type: str = "int8"
    cpu_threads: int = 4
    beam_size: int = 1
    language: str = "pt"
    vad_min_silence_ms: int = 400
    vad_speech_pad_ms: int = 200
    drain_timeout_seconds: int = 1800   # tempo máx. p/ a fila de ASR esvaziar após o Stop


@dataclass(frozen=True)
class SegmenterConfig:
    target_seconds: float = 24.0
    min_seconds: float = 8.0
    max_seconds: float = 30.0
    carry_pad_seconds: float = 0.15
    flush_min_seconds: float = 0.4   # no Stop: abaixo disso só descarta se for silêncio


@dataclass(frozen=True)
class ObsidianConfig:
    enabled: bool = False
    vault_path: str = ""
    meetings_folder: str = "Reunioes"
    auto_export: bool = True
    default_status: str = "Em andamento"
    dashboard_enabled: bool = True
    dashboard_folder: str = "Dashboard"
    dashboard_filename: str = "Dashboard.md"
    # Fase 3: só LEITURA (o ata nunca escreve em Projetos/ — são notas
    # mantidas por você ou por outra ferramenta). O Dashboard
    # passa a também consolidar essas notas.
    projetos_folder: str = "Projetos"


@dataclass(frozen=True)
class OllamaConfig:
    base_url: str = "http://127.0.0.1:11434"
    model: str = "llama3.2:3b"
    keep_alive: str = "30m"
    window_minutes: float = 10.0
    temperature: float = 0.2
    allow_fallback: bool = True   # se o modelo configurado não existir, usar outro instalado
    num_ctx_min: int = 4096
    num_ctx_max: int = 32768
    request_timeout_seconds: int = 900


@dataclass(frozen=True)
class PostprocessConfig:
    dedupe_echo: bool = True            # remove fala ME que repete a SYSTEM (eco do alto-falante no microfone)
    echo_window_seconds: float = 4.0    # janela de tempo para considerar a mesma fala
    echo_min_similarity: float = 0.6    # 0..1 (difflib) para considerar a mesma fala
    echo_min_word_overlap: float = 0.6  # fração das palavras do ME presentes no SYSTEM (cortes/erros do ASR diferem entre canais)
    echo_min_words: int = 5             # só aplica a sobreposição em falas com pelo menos tantas palavras
    asr_lag_warn_seconds: int = 300     # avisa (log + tela) quando o ASR ao vivo está tantos s atrás do áudio


@dataclass(frozen=True)
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    segmenter: SegmenterConfig = field(default_factory=SegmenterConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    obsidian: ObsidianConfig = field(default_factory=ObsidianConfig)
    guardrails: GuardrailsConfig = field(default_factory=GuardrailsConfig)
    postprocess: PostprocessConfig = field(default_factory=PostprocessConfig)

    # --- caminhos derivados -------------------------------------------------
    @property
    def data_path(self) -> Path:
        return self._abs(self.paths.data_dir)

    @property
    def _obsidian_vault_root(self) -> Path | None:
        if not self.obsidian.vault_path:
            return None
        vault = Path(self.obsidian.vault_path).expanduser()
        return vault if vault.is_dir() else None

    @property
    def obsidian_meetings_path(self) -> Path | None:
        """`None` = vault não configurado (não é erro; exportador trata como tal).
        Config quebrada (vault_path setado mas inexistente) some por conta do
        exportador, que distingue isso de "desligado" via `ObsidianConfig.enabled`."""
        root = self._obsidian_vault_root
        return root / self.obsidian.meetings_folder if root else None

    @property
    def obsidian_dashboard_path(self) -> Path | None:
        """Caminho do `Dashboard.md`. Mesma regra de `None` que `obsidian_meetings_path`
        — vault ausente/inválido não é erro fatal, quem decide isso é o rebuilder."""
        root = self._obsidian_vault_root
        return root / self.obsidian.dashboard_folder / self.obsidian.dashboard_filename if root else None

    @property
    def obsidian_projetos_path(self) -> Path | None:
        """Caminho de `Projetos/` — só leitura (notas mantidas por você ou por
        outra ferramenta). Mesma regra de `None` de `obsidian_meetings_path`."""
        root = self._obsidian_vault_root
        return root / self.obsidian.projetos_folder if root else None

    @property
    def model_cache_path(self) -> Path:
        return self._abs(self.paths.model_cache_dir)

    @property
    def meetings_path(self) -> Path:
        return self.data_path / "meetings"

    @property
    def db_path(self) -> Path:
        return self.data_path / "ata.db"

    @property
    def logs_path(self) -> Path:
        return self._abs("./logs")

    @staticmethod
    def _abs(value: str) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()

    def ensure_dirs(self) -> None:
        for path in (self.data_path, self.meetings_path, self.model_cache_path, self.logs_path):
            path.mkdir(parents=True, exist_ok=True)


def _coerce(dc_type: type, raw: dict[str, Any]) -> dict[str, Any]:
    """Mantém só chaves conhecidas e converte tipos primitivos."""
    known = {f.name: f.type for f in fields(dc_type)}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in known:
            continue
        target = known[key]
        try:
            if target in ("int", int):
                out[key] = int(value)
            elif target in ("float", float):
                out[key] = float(value)
            elif target in ("bool", bool):
                out[key] = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
            else:
                out[key] = str(value)
        except (TypeError, ValueError):
            out[key] = value
    return out


def _from_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _from_env() -> dict[str, dict[str, Any]]:
    """ATA_ASR_BEAM_SIZE=2  ->  {"asr": {"beam_size": "2"}}  (primeira palavra = seção)."""
    sections = {"server", "paths", "asr", "segmenter", "ollama", "obsidian", "guardrails", "postprocess"}
    out: dict[str, dict[str, Any]] = {}
    for env_key, value in os.environ.items():
        if not env_key.startswith("ATA_"):
            continue
        rest = env_key[4:].lower()
        section, _, option = rest.partition("_")
        if section not in sections or not option:
            continue
        out.setdefault(section, {})[option] = value
    return out


def load_config(config_path: Path | None = None) -> Config:
    path = config_path or (PROJECT_ROOT / "config.toml")
    merged: dict[str, dict[str, Any]] = {}
    file_data = _from_file(path)
    env_data = _from_env()
    for section in ("server", "paths", "asr", "segmenter", "ollama", "obsidian", "guardrails", "postprocess"):
        section_raw: dict[str, Any] = {}
        section_raw.update(file_data.get(section, {}) or {})
        section_raw.update(env_data.get(section, {}) or {})
        merged[section] = section_raw

    return Config(
        server=ServerConfig(**_coerce(ServerConfig, merged["server"])),
        paths=PathsConfig(**_coerce(PathsConfig, merged["paths"])),
        asr=AsrConfig(**_coerce(AsrConfig, merged["asr"])),
        segmenter=SegmenterConfig(**_coerce(SegmenterConfig, merged["segmenter"])),
        ollama=OllamaConfig(**_coerce(OllamaConfig, merged["ollama"])),
        obsidian=ObsidianConfig(**_coerce(ObsidianConfig, merged["obsidian"])),
        guardrails=GuardrailsConfig(**_coerce(GuardrailsConfig, merged["guardrails"])),
        postprocess=PostprocessConfig(**_coerce(PostprocessConfig, merged["postprocess"])),
    )


assert is_dataclass(Config)
