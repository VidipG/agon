"""CLI trigger: translate Typer command arguments into a RunRequest."""
from __future__ import annotations

from pathlib import Path

from .base import AnalysisScope, RunRequest, SpecRef
from .spec_resolver import SpecRefRegistry


class CLITrigger:
    """Constructs a RunRequest from CLI arguments.

    Spec location strings are resolved via SpecRefRegistry. To add support
    for a new spec source type, register a new SpecRefResolver — no changes
    needed here.

    Supported --spec formats (examples):
      ./requirements.md          file path (auto-detected)
      ./specs/                   directory (auto-detected)
      ./api.yaml                 OpenAPI file (auto-detected by extension)
      https://api.example.com/openapi.yaml   OpenAPI URL (auto-detected)
      jira:PROJ-123              Jira ticket (explicit prefix)
      PROJ-123                   Jira ticket (bare-ID convenience)
      linear:ENG-456             Linear issue (explicit prefix)
      gh:owner/repo#42           GitHub issue (explicit prefix)
      owner/repo#42              GitHub issue (bare shortform convenience)
      openapi:https://api.../spec  OpenAPI URL (explicit prefix)
      file:./future-spec.md      Force file treatment (explicit prefix)
    """

    def __init__(
        self,
        paths: list[Path],
        *,
        mode: str = "analyze",
        specs: list[str] | None = None,
        git_base: str | None = None,
        config: Path | None = None,
        output: str = "terminal",
        iterate: bool = False,
        dry_run: bool = False,
        functions: list[str] | None = None,
        report_path: Path | None = None,
        cache_path: Path | None = None,
        fail_under: float | None = None,
        registry: SpecRefRegistry | None = None,
    ) -> None:
        self._paths = paths
        self._mode = mode
        self._specs = specs or []
        self._git_base = git_base
        self._config = config
        self._output = output
        self._iterate = iterate
        self._dry_run = dry_run
        self._functions = functions or []
        self._report_path = report_path
        self._cache_path = cache_path
        self._fail_under = fail_under
        self._registry = registry or SpecRefRegistry.default()

    def parse(self) -> RunRequest:
        scope = AnalysisScope(
            paths=self._paths,
            functions=self._functions,
            git_base=self._git_base,
        )
        spec_refs = [self._registry.resolve(loc) for loc in self._specs]
        return RunRequest(
            scope=scope,
            specs=spec_refs,
            config_path=self._config,
            mode=self._mode,  # type: ignore[arg-type]
            output_format=self._output,  # type: ignore[arg-type]
            iterate=self._iterate,
            dry_run=self._dry_run,
            report_path=self._report_path,
            cache_path=self._cache_path,
            fail_under=self._fail_under,
        )
