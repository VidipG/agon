"""Tests for the trigger abstraction layer and SpecRef resolution."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agon.triggers.base import AnalysisScope, RunRequest, SpecRef
from agon.triggers.cli_trigger import CLITrigger
from agon.triggers.github_action import GitHubActionTrigger
from agon.triggers.spec_resolver import (
    FilesystemResolver,
    FileResolver,
    GitHubIssueResolver,
    JiraResolver,
    LinearResolver,
    OpenAPIResolver,
    SpecRefRegistry,
    URLResolver,
)


# ---------------------------------------------------------------------------
# CLITrigger
# ---------------------------------------------------------------------------


class TestCLITrigger:
    def test_basic_analyze_request(self, tmp_path: Path) -> None:
        request = CLITrigger(paths=[tmp_path], mode="analyze").parse()

        assert isinstance(request, RunRequest)
        assert request.mode == "analyze"
        assert request.scope.paths == [tmp_path]
        assert request.specs == []
        assert request.output_format == "terminal"
        assert request.iterate is False
        assert request.dry_run is False

    def test_diff_mode_with_base(self, tmp_path: Path) -> None:
        request = CLITrigger(paths=[tmp_path], mode="diff", git_base="main").parse()
        assert request.mode == "diff"
        assert request.scope.git_base == "main"

    def test_multiple_paths(self, tmp_path: Path) -> None:
        request = CLITrigger(paths=[tmp_path / "a", tmp_path / "b"]).parse()
        assert len(request.scope.paths) == 2

    def test_iterate_and_dry_run_flags(self, tmp_path: Path) -> None:
        request = CLITrigger(paths=[tmp_path], iterate=True, dry_run=True).parse()
        assert request.iterate is True
        assert request.dry_run is True

    def test_spec_strings_resolved_via_registry(self, tmp_path: Path) -> None:
        md_file = tmp_path / "requirements.md"
        md_file.write_text("# Spec")

        request = CLITrigger(
            paths=[tmp_path],
            specs=[str(md_file), "jira:PROJ-123"],
        ).parse()

        assert len(request.specs) == 2
        assert request.specs[0].kind == "file"
        assert request.specs[1].kind == "jira_ticket"

    def test_accepts_custom_registry(self, tmp_path: Path) -> None:
        """CLITrigger accepts an injected registry for testing and extension."""
        custom_registry = SpecRefRegistry()
        custom_registry.register(JiraResolver())

        request = CLITrigger(
            paths=[tmp_path],
            specs=["jira:PROJ-999"],
            registry=custom_registry,
        ).parse()

        assert request.specs[0].kind == "jira_ticket"


# ---------------------------------------------------------------------------
# SpecRefRegistry
# ---------------------------------------------------------------------------


class TestSpecRefRegistry:
    @pytest.fixture
    def registry(self) -> SpecRefRegistry:
        return SpecRefRegistry.default()

    # --- Prefix dispatch ---

    def test_jira_prefix(self, registry: SpecRefRegistry) -> None:
        ref = registry.resolve("jira:PROJ-123")
        assert ref.kind == "jira_ticket"
        assert ref.location == "PROJ-123"

    def test_linear_prefix(self, registry: SpecRefRegistry) -> None:
        ref = registry.resolve("linear:ENG-456")
        assert ref.kind == "linear_ticket"
        assert ref.location == "ENG-456"

    def test_gh_prefix(self, registry: SpecRefRegistry) -> None:
        ref = registry.resolve("gh:owner/repo#42")
        assert ref.kind == "github_issue"
        assert ref.location == "owner/repo#42"

    def test_openapi_prefix_file(self, registry: SpecRefRegistry) -> None:
        ref = registry.resolve("openapi:./api.yaml")
        assert ref.kind == "openapi_file"

    def test_openapi_prefix_url(self, registry: SpecRefRegistry) -> None:
        ref = registry.resolve("openapi:https://api.example.com/spec.yaml")
        assert ref.kind == "openapi_url"

    def test_file_prefix_forces_file_kind(self, registry: SpecRefRegistry) -> None:
        # file: prefix forces kind="file" even for .yaml extensions
        ref = registry.resolve("file:./spec.yaml")
        assert ref.kind == "file"
        assert ref.location == "./spec.yaml"

    def test_unknown_prefix_raises(self, registry: SpecRefRegistry) -> None:
        with pytest.raises(ValueError, match="Unknown spec prefix"):
            registry.resolve("notion:abc-123")

    def test_error_message_lists_known_prefixes(self, registry: SpecRefRegistry) -> None:
        with pytest.raises(ValueError, match="jira") as exc_info:
            registry.resolve("notion:abc-123")
        # The error should list known prefixes to help the user
        assert "linear" in str(exc_info.value)
        assert "openapi" in str(exc_info.value)

    # --- Bare-string dispatch ---

    def test_bare_http_url(self, registry: SpecRefRegistry) -> None:
        ref = registry.resolve("https://docs.example.com/requirements")
        assert ref.kind == "url"

    def test_bare_url_with_openapi_keyword(self, registry: SpecRefRegistry) -> None:
        ref = registry.resolve("https://api.example.com/openapi.json")
        assert ref.kind == "openapi_url"

    def test_bare_url_swagger_keyword(self, registry: SpecRefRegistry) -> None:
        ref = registry.resolve("https://api.example.com/swagger.yaml")
        assert ref.kind == "openapi_url"

    def test_bare_filesystem_markdown(self, registry: SpecRefRegistry, tmp_path: Path) -> None:
        f = tmp_path / "requirements.md"
        f.write_text("# Spec")
        ref = registry.resolve(str(f))
        assert ref.kind == "file"

    def test_bare_filesystem_yaml_is_plain_file(self, registry: SpecRefRegistry, tmp_path: Path) -> None:
        # A .yaml file on disk is just a file — the user must be explicit
        # about intent using the openapi: prefix if it's an OpenAPI spec.
        f = tmp_path / "api.yaml"
        f.write_text("openapi: 3.0")
        ref = registry.resolve(str(f))
        assert ref.kind == "file"

    def test_bare_filesystem_directory(self, registry: SpecRefRegistry, tmp_path: Path) -> None:
        ref = registry.resolve(str(tmp_path))
        assert ref.kind == "directory"

    def test_bare_jira_id_convenience(self, registry: SpecRefRegistry) -> None:
        ref = registry.resolve("PROJ-123")
        assert ref.kind == "jira_ticket"

    def test_bare_github_shortform_convenience(self, registry: SpecRefRegistry) -> None:
        ref = registry.resolve("owner/repo#42")
        assert ref.kind == "github_issue"

    def test_filesystem_takes_priority_over_jira_pattern(
        self, registry: SpecRefRegistry, tmp_path: Path
    ) -> None:
        """A file named PROJ-123.md must be treated as a file, not a Jira ticket."""
        ticket_file = tmp_path / "PROJ-123.md"
        ticket_file.write_text("# Spec")
        ref = registry.resolve(str(ticket_file))
        assert ref.kind == "file"

    def test_unresolvable_string_raises_with_suggestions(
        self, registry: SpecRefRegistry
    ) -> None:
        with pytest.raises(ValueError, match="Cannot resolve") as exc_info:
            registry.resolve("some-random-string-that-matches-nothing")
        # Error should contain actionable guidance
        assert "jira:" in str(exc_info.value)

    def test_empty_string_raises(self, registry: SpecRefRegistry) -> None:
        with pytest.raises(ValueError, match="Empty"):
            registry.resolve("")

    # --- Registry extensibility ---

    def test_custom_resolver_registered_and_dispatched(self) -> None:
        """Demonstrate adding a new spec type with zero changes to core logic."""
        from agon.triggers.base import SpecRef
        from agon.triggers.spec_resolver import SpecRefResolver

        class NotionResolver:
            prefix = "notion"

            def can_resolve(self, raw: str) -> bool:
                return False

            def resolve(self, raw: str) -> SpecRef:
                return SpecRef(kind="url", location=f"https://notion.so/{raw}")

        registry = SpecRefRegistry.default()
        registry.register(NotionResolver())

        ref = registry.resolve("notion:abc-123-def")
        assert ref.kind == "url"
        assert "notion.so" in ref.location


# ---------------------------------------------------------------------------
# GitHubActionTrigger
# ---------------------------------------------------------------------------


class TestGitHubActionTrigger:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ["INPUT_PATH", "INPUT_MODE", "INPUT_SPEC", "INPUT_BASE-REF",
                    "GITHUB_BASE_REF", "GITHUB_SHA"]:
            monkeypatch.delenv(var, raising=False)

        request = GitHubActionTrigger().parse()

        assert request.scope.paths == [Path(".")]
        assert request.mode == "diff"
        assert request.output_format == "sarif"
        assert request.specs == []

    def test_explicit_inputs(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("INPUT_PATH", str(tmp_path))
        monkeypatch.setenv("INPUT_MODE", "analyze")
        monkeypatch.setenv("INPUT_OUTPUT-FORMAT", "json")
        monkeypatch.setenv("INPUT_ITERATE", "true")
        monkeypatch.setenv("INPUT_BASE-REF", "main")
        monkeypatch.delenv("INPUT_SPEC", raising=False)

        request = GitHubActionTrigger().parse()

        assert request.scope.paths == [tmp_path]
        assert request.mode == "analyze"
        assert request.output_format == "json"
        assert request.iterate is True
        assert request.scope.git_base == "main"

    def test_base_ref_from_github_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("INPUT_BASE-REF", raising=False)
        monkeypatch.setenv("GITHUB_BASE_REF", "develop")

        request = GitHubActionTrigger().parse()
        assert request.scope.git_base == "develop"

    def test_multiline_specs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("INPUT_SPEC", "jira:PROJ-123\njira:PROJ-456")

        request = GitHubActionTrigger().parse()
        assert len(request.specs) == 2
        assert all(s.kind == "jira_ticket" for s in request.specs)
