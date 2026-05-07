"""
Unit tests for git_scope helpers.

These tests use a real temporary git repository so we can verify the actual
git integration without mocking subprocess.  Each test builds the minimal repo
state it needs and tears it down via tmp_path.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agon.triggers.git_scope import changed_files, filter_to_scope


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def _setup_repo(tmp_path: Path) -> Path:
    """Initialise a git repo with an initial commit."""
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@agon.dev")
    _git(tmp_path, "config", "user.name", "Agon Test")
    (tmp_path / "base.py").write_text("def foo(): return 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "initial")
    return tmp_path


# ---------------------------------------------------------------------------
# changed_files
# ---------------------------------------------------------------------------


class TestChangedFiles:
    def test_no_changes_returns_empty(self, tmp_path: Path):
        repo = _setup_repo(tmp_path)
        # Nothing changed since initial commit; uncommitted diff is empty
        result = changed_files(repo, base_ref=None)
        assert result == []

    def test_uncommitted_change_detected(self, tmp_path: Path):
        repo = _setup_repo(tmp_path)
        (repo / "new.py").write_text("x = 1\n")
        _git(repo, "add", "new.py")
        result = changed_files(repo, base_ref=None)
        assert any(p.name == "new.py" for p in result)

    def test_committed_change_detected_with_base(self, tmp_path: Path):
        repo = _setup_repo(tmp_path)
        # Create a second commit that adds a file
        (repo / "added.py").write_text("def bar(): return 2\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "add bar")
        result = changed_files(repo, base_ref="HEAD~1")
        assert any(p.name == "added.py" for p in result)

    def test_deleted_file_not_returned(self, tmp_path: Path):
        repo = _setup_repo(tmp_path)
        _git(repo, "rm", "base.py")
        _git(repo, "commit", "-m", "remove base")
        result = changed_files(repo, base_ref="HEAD~1")
        # base.py was deleted; it no longer exists on disk
        assert not any(p.name == "base.py" for p in result)

    def test_returns_absolute_paths(self, tmp_path: Path):
        repo = _setup_repo(tmp_path)
        (repo / "mod.py").write_text("x = 2\n")
        _git(repo, "add", ".")
        result = changed_files(repo, base_ref=None)
        for p in result:
            assert p.is_absolute()

    def test_git_not_available_returns_empty(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("PATH", "")  # break PATH so git can't be found
        # Should not raise; returns empty list
        result = changed_files(tmp_path, base_ref=None)
        assert result == []


# ---------------------------------------------------------------------------
# filter_to_scope
# ---------------------------------------------------------------------------


class TestFilterToScope:
    def test_matches_files_under_scope_dir(self, tmp_path: Path):
        src = tmp_path / "src"
        src.mkdir()
        f = src / "lib.py"
        f.write_text("")
        result = filter_to_scope([f], scope_paths=[src], adapter_extensions=(".py",))
        assert f in result

    def test_excludes_files_outside_scope(self, tmp_path: Path):
        inside = tmp_path / "src" / "lib.py"
        inside.parent.mkdir()
        inside.write_text("")
        outside = tmp_path / "other" / "stuff.py"
        outside.parent.mkdir()
        outside.write_text("")
        result = filter_to_scope(
            [inside, outside],
            scope_paths=[tmp_path / "src"],
            adapter_extensions=(".py",),
        )
        assert outside not in result
        assert inside in result

    def test_excludes_non_source_extensions(self, tmp_path: Path):
        py_file = tmp_path / "lib.py"
        txt_file = tmp_path / "README.txt"
        py_file.write_text("")
        txt_file.write_text("")
        result = filter_to_scope(
            [py_file, txt_file],
            scope_paths=[tmp_path],
            adapter_extensions=(".py",),
        )
        assert txt_file not in result
        assert py_file in result

    def test_falls_back_to_scope_paths_when_nothing_matches(self, tmp_path: Path):
        """If the filtered list would be empty, return original scope_paths."""
        scope = [tmp_path / "src"]
        changed = [tmp_path / "docs" / "README.md"]
        result = filter_to_scope(changed, scope_paths=scope, adapter_extensions=(".py",))
        assert result == scope

    def test_exact_file_scope_match(self, tmp_path: Path):
        f = tmp_path / "lib.py"
        f.write_text("")
        result = filter_to_scope([f], scope_paths=[f], adapter_extensions=(".py",))
        assert f in result
