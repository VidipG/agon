"""
GitHub Actions trigger: read INPUT_* environment variables → RunRequest.

GitHub Actions passes action inputs as environment variables with the prefix
INPUT_ and uppercased names (e.g., input "base-ref" → INPUT_BASE-REF).

This trigger also reads standard GitHub Actions context variables
(GITHUB_BASE_REF, GITHUB_SHA, etc.) to provide sensible defaults.

Corresponding action.yml inputs this trigger expects:
  path:          Files or directories to analyze. Newline-separated for multiple.
                 Default: '.'
  mode:          Analysis mode. Default: 'diff' (analyze only changed code).
  spec:          Spec sources. Newline-separated list of files, URLs, ticket IDs.
  base-ref:      Base git ref for diff mode. Default: PR base branch or 'main'.
  output-format: Output format. Default: 'sarif' (renders in GitHub Code Scanning).
  iterate:       Enable feedback loop. 'true' or 'false'. Default: 'false'.
  config:        Path to config file. Default: auto-detect .agon/config.toml.
"""
from __future__ import annotations

import os
from pathlib import Path

from .base import RunRequest
from .cli_trigger import CLITrigger


class GitHubActionTrigger:
    """Constructs a RunRequest from GitHub Actions environment variables."""

    def parse(self) -> RunRequest:
        paths = self._parse_paths()
        specs = self._parse_specs()
        mode = os.environ.get("INPUT_MODE", "diff").strip()
        output = os.environ.get("INPUT_OUTPUT-FORMAT", "sarif").strip()
        iterate = os.environ.get("INPUT_ITERATE", "false").strip().lower() == "true"
        config_str = os.environ.get("INPUT_CONFIG", "").strip()
        base_ref = self._resolve_base_ref()

        return CLITrigger(
            paths=paths,
            mode=mode,
            specs=specs,
            git_base=base_ref,
            config=Path(config_str) if config_str else None,
            output=output,
            iterate=iterate,
        ).parse()

    @staticmethod
    def _parse_paths() -> list[Path]:
        raw = os.environ.get("INPUT_PATH", ".").strip()
        parts = [p.strip() for p in raw.split("\n") if p.strip()]
        return [Path(p) for p in parts] if parts else [Path(".")]

    @staticmethod
    def _parse_specs() -> list[str]:
        raw = os.environ.get("INPUT_SPEC", "").strip()
        return [s.strip() for s in raw.split("\n") if s.strip()]

    @staticmethod
    def _resolve_base_ref() -> str | None:
        explicit = os.environ.get("INPUT_BASE-REF", "").strip()
        if explicit:
            return explicit
        # Standard GH Actions context variables
        return (
            os.environ.get("GITHUB_BASE_REF")   # present on pull_request events
            or os.environ.get("GITHUB_SHA")      # fallback: current commit SHA
            or None
        )
