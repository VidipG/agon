"""
Configuration loading and validation.

Configuration is read from .agon/config.toml (project-level) with environment
variable overrides. All settings have documented defaults so Agon works
out-of-the-box with zero configuration.

For multi-package monorepos, pass --config <path> to specify a different
config file. Root-level settings are inherited; per-package configs override.
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_CONFIG_PATHS = [
    Path(".agon/config.toml"),
    Path("agon.toml"),
]


class GeneralConfig(BaseModel):
    language: str = "python"
    test_command: str = "pytest"
    timeout_seconds: int = 30
    max_iterations: int = 3
    budget_limit_usd: float = 5.00


class ModelsConfig(BaseModel):
    eigentest_local: str = "qwen2.5-coder:7b"
    eigentest_frontier: str = "claude-sonnet-4-6"
    mutagen_local: str = "qwen2.5-coder:7b"
    mutagen_filter: str = "claude-haiku-4-5-20251001"
    spectre_generator: str = "gpt-4o"
    embedding: str = "voyage-code-3"


class CacheConfig(BaseModel):
    backend: Literal["lancedb", "qdrant"] = "lancedb"
    similarity_threshold: float = 0.88
    path: str = ".agon/cache"


class PriorityConfig(BaseModel):
    skip_patterns: list[str] = Field(default_factory=lambda: [
        "**/migrations/**",
        "**/generated/**",
        "**/vendor/**",
    ])
    critical_patterns: list[str] = Field(default_factory=lambda: [
        "**/auth/**",
        "**/crypto/**",
        "**/security/**",
    ])


class MutagenConfig(BaseModel):
    max_mutants_per_function: int = 20
    parallel_workers: int = 1  # Phase 1: serial; set >1 when sandbox is thread-safe
    skip_equivalent_detection: bool = False
    timeout_multiplier: float = 2.0
    # Glob patterns for files to exclude from mutation (applied against FunctionRef.file)
    skip_patterns: list[str] = Field(default_factory=lambda: [
        "**/__pycache__/**",
        "**/test_*.py",
        "**/tests/**",
        "**/conftest.py",
    ])


class SandboxConfig(BaseModel):
    backend: Literal["process", "container", "cloud"] = "process"
    timeout_multiplier: float = 2.0
    memory_limit_mb: int = 1024


class ObservabilityConfig(BaseModel):
    backend: Literal["sqlite", "otlp"] = "sqlite"
    path: str = ".agon/usage.db"


class CIConfig(BaseModel):
    fail_on: list[str] = Field(default_factory=lambda: ["critical", "high"])


class JiraConfig(BaseModel):
    url: str = ""
    email: str = ""
    # api_token is intentionally not stored in the file — use JIRA_API_TOKEN env var


class LinearConfig(BaseModel):
    pass  # api_key is intentionally not stored in the file — use LINEAR_API_KEY env var


class AgonConfig(BaseModel):
    """Root configuration object. Loaded from .agon/config.toml."""

    general: GeneralConfig = Field(default_factory=GeneralConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    priority: PriorityConfig = Field(default_factory=PriorityConfig)
    mutagen: MutagenConfig = Field(default_factory=MutagenConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    ci: CIConfig = Field(default_factory=CIConfig)
    jira: JiraConfig = Field(default_factory=JiraConfig)
    linear: LinearConfig = Field(default_factory=LinearConfig)


def load_config(config_path: Path | None = None) -> AgonConfig:
    """Load configuration from a TOML file.

    Search order:
    1. Explicit path (from --config CLI flag or trigger adapter)
    2. .agon/config.toml in the current directory
    3. agon.toml in the current directory
    4. Built-in defaults (no file required)
    """
    resolved = _find_config_file(config_path)
    if resolved is None:
        return AgonConfig()

    raw = tomllib.loads(resolved.read_text(encoding="utf-8"))

    # Populate JIRA_URL/JIRA_EMAIL from config file into JiraConfig
    # (api_token stays env-only for security)
    if jira_section := raw.get("jira", {}):
        raw["jira"] = jira_section

    return AgonConfig.model_validate(raw)


def _find_config_file(explicit: Path | None) -> Path | None:
    if explicit is not None:
        if not explicit.exists():
            raise FileNotFoundError(f"Config file not found: {explicit}")
        return explicit
    for candidate in _DEFAULT_CONFIG_PATHS:
        if candidate.exists():
            return candidate
    return None
