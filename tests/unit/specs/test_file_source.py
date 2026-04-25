"""Tests for the file-based spec source."""
from __future__ import annotations

from pathlib import Path

import pytest

from agon.specs.file_source import FileSpecSource
from agon.triggers.base import SpecRef


@pytest.fixture
def source() -> FileSpecSource:
    return FileSpecSource()


class TestFileSpecSource:
    def test_can_handle_file_kind(self, source: FileSpecSource) -> None:
        ref = SpecRef(kind="file", location="/some/path.md")
        assert source.can_handle(ref) is True

    def test_can_handle_directory_kind(self, source: FileSpecSource) -> None:
        ref = SpecRef(kind="directory", location="/some/dir")
        assert source.can_handle(ref) is True

    def test_cannot_handle_other_kinds(self, source: FileSpecSource) -> None:
        for kind in ("openapi_file", "openapi_url", "jira_ticket", "linear_ticket", "url"):
            ref = SpecRef(kind=kind, location="anything")  # type: ignore
            assert source.can_handle(ref) is False

    async def test_load_markdown_file(
        self, source: FileSpecSource, specs_fixtures_dir: Path
    ) -> None:
        ref = SpecRef(kind="file", location=str(specs_fixtures_dir / "requirements.md"))
        doc = await source.fetch(ref)

        assert doc.format == "markdown"
        assert "Payment Processing" in doc.title or "Requirements" in doc.title
        assert "process_payment" in doc.content
        assert doc.fetched_at is not None

    async def test_load_markdown_sets_correct_format(
        self, source: FileSpecSource, tmp_path: Path
    ) -> None:
        md_file = tmp_path / "spec.md"
        md_file.write_text("# Spec\nSome requirements.")

        doc = await source.fetch(SpecRef(kind="file", location=str(md_file)))
        assert doc.format == "markdown"

    async def test_load_txt_sets_plaintext_format(
        self, source: FileSpecSource, tmp_path: Path
    ) -> None:
        txt_file = tmp_path / "notes.txt"
        txt_file.write_text("Some plain text notes.")

        doc = await source.fetch(SpecRef(kind="file", location=str(txt_file)))
        assert doc.format == "plaintext"

    async def test_raises_on_missing_file(self, source: FileSpecSource, tmp_path: Path) -> None:
        ref = SpecRef(kind="file", location=str(tmp_path / "nonexistent.md"))
        with pytest.raises(FileNotFoundError, match="not found"):
            await source.fetch(ref)

    async def test_raises_on_unsupported_extension(
        self, source: FileSpecSource, tmp_path: Path
    ) -> None:
        yaml_file = tmp_path / "spec.yaml"
        yaml_file.write_text("openapi: 3.0")

        ref = SpecRef(kind="file", location=str(yaml_file))
        with pytest.raises(ValueError, match="openapi_file"):
            await source.fetch(ref)

    async def test_load_directory(
        self, source: FileSpecSource, specs_fixtures_dir: Path
    ) -> None:
        ref = SpecRef(kind="directory", location=str(specs_fixtures_dir))
        doc = await source.fetch(ref)

        assert doc.format == "markdown"
        assert doc.metadata["file_count"] >= 1
        assert "requirements.md" in doc.content or "Requirements" in doc.content

    async def test_directory_includes_per_file_sections(
        self, source: FileSpecSource, specs_fixtures_dir: Path
    ) -> None:
        """structured_data must contain per-file sections for eigentest chunking."""
        ref = SpecRef(kind="directory", location=str(specs_fixtures_dir))
        doc = await source.fetch(ref)

        assert doc.structured_data is not None
        sections = doc.structured_data["sections"]
        assert len(sections) >= 1
        assert all("file" in s and "content" in s and "char_count" in s for s in sections)
        assert doc.structured_data["total_char_count"] == len(doc.content)

    async def test_directory_combines_multiple_files(
        self, source: FileSpecSource, tmp_path: Path
    ) -> None:
        (tmp_path / "a.md").write_text("# Section A\nContent A.")
        (tmp_path / "b.md").write_text("# Section B\nContent B.")
        (tmp_path / "c.txt").write_text("Plain text section.")

        ref = SpecRef(kind="directory", location=str(tmp_path))
        doc = await source.fetch(ref)

        assert "Content A" in doc.content
        assert "Content B" in doc.content
        assert "Plain text section" in doc.content
        assert doc.metadata["file_count"] == 3
        # Per-file sections for chunked LLM processing
        assert doc.structured_data is not None
        assert len(doc.structured_data["sections"]) == 3

    async def test_directory_raises_on_empty(
        self, source: FileSpecSource, tmp_path: Path
    ) -> None:
        ref = SpecRef(kind="directory", location=str(tmp_path))
        with pytest.raises(ValueError, match="No spec files found"):
            await source.fetch(ref)

    async def test_directory_raises_on_missing(
        self, source: FileSpecSource, tmp_path: Path
    ) -> None:
        ref = SpecRef(kind="directory", location=str(tmp_path / "nonexistent"))
        with pytest.raises(FileNotFoundError):
            await source.fetch(ref)
