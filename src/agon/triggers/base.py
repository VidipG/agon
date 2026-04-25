"""
Trigger abstraction layer.

All Agon entry points (CLI, GitHub Actions, CI environments, future API)
translate their native input format into a RunRequest. The pipeline engine
only speaks RunRequest — it has no knowledge of how it was invoked.

Pattern: Ports and Adapters (Hexagonal Architecture)
  Port:     RunRequest  — the domain's language for "please do this analysis"
  Adapters: CLITrigger, GitHubActionTrigger, CIEnvironmentTrigger, ...

Adding a new trigger source is a single new file implementing TriggerAdapter
with no changes to the pipeline engine.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel


class AnalysisScope(BaseModel):
    """Defines what code Agon should analyze.

    Describes the *target* of analysis — paths, optional function filters, and
    the git ref for diff mode. Skip/include patterns are policy and live in
    AgonConfig.priority (PriorityConfig), applied by the pipeline engine when
    it resolves this scope against the loaded configuration.
    """

    paths: list[Path]           # files or directories to analyze
    functions: list[str] = []   # optional: limit to specific function name patterns
    git_base: str | None = None # diff mode: base ref (branch, SHA, or tag)


class SpecRef(BaseModel):
    """A reference to a single specification source.

    kind controls how location is interpreted:
      file          — path to a single document (.md, .txt, .rst)
      directory     — path to a directory; all spec documents are loaded
      openapi_file  — path to an OpenAPI YAML or JSON file
      openapi_url   — URL to a live OpenAPI spec endpoint
      jira_ticket   — Jira ticket ID (e.g. "PROJ-123") or full Jira URL
      linear_ticket — Linear issue ID or URL
      github_issue  — GitHub issue in "owner/repo#123" form or full URL
      url           — arbitrary URL fetched and treated as markdown/text
    """

    kind: Literal[
        "file",
        "directory",
        "openapi_file",
        "openapi_url",
        "jira_ticket",
        "linear_ticket",
        "github_issue",
        "url",
    ]
    location: str  # path, URL, or ticket ID — interpretation depends on kind


class RunRequest(BaseModel):
    """Unified input contract for all Agon pipeline invocations.

    Created by a TriggerAdapter from the native trigger format.
    The pipeline engine only accepts RunRequest — it never reads argv,
    environment variables, or HTTP requests directly.
    """

    scope: AnalysisScope
    specs: list[SpecRef] = []
    config_path: Path | None = None
    mode: Literal[
        "analyze",      # full pipeline: eigentest → mutagen → spectre
        "eigentest",    # invariant inference only
        "mutagen",      # mutation testing only (accepts invariant file or uses built-ins)
        "spectre",      # counterexample generation only (accepts mutation report)
        "diff",         # analyze only functions changed vs git_base
        "bootstrap",    # generate initial test file for unannotated code
    ] = "analyze"
    output_format: Literal["terminal", "json", "sarif", "markdown"] = "terminal"
    iterate: bool = False       # enable closed feedback loop (multiple passes)
    dry_run: bool = False       # plan what would run without executing


class TriggerAdapter(Protocol):
    """Convert a trigger-specific input format into a RunRequest."""

    def parse(self) -> RunRequest: ...
