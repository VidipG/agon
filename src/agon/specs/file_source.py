"""
File-based specification source.

Handles:
  kind="file"      — single spec document (.md, .txt, .rst, .pdf)
  kind="directory" — directory scan; loads all spec documents recursively

Supported file extensions:
  .md / .markdown  → format "markdown"
  .txt / .rst      → format "plaintext"

Files with extensions .yaml, .yml, .json are intentionally excluded here;
OpenAPISpecSource handles those to enable mechanical extraction of the schema.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..triggers.base import SpecRef
from .base import SpecDocument, SpecSource

_TEXT_EXTENSIONS = {".md", ".markdown", ".txt", ".rst"}
_MARKDOWN_EXTENSIONS = {".md", ".markdown"}

_HANDLED_KINDS = {"file", "directory"}


class FileSpecSource:
    """Reads spec documents from the local filesystem."""

    def can_handle(self, ref: SpecRef) -> bool:
        return ref.kind in _HANDLED_KINDS

    async def fetch(self, ref: SpecRef) -> SpecDocument:
        if ref.kind == "directory":
            return await self._load_directory(ref)
        return await self._load_file(ref)

    async def _load_file(self, ref: SpecRef) -> SpecDocument:
        path = Path(ref.location)
        if not path.exists():
            raise FileNotFoundError(f"Spec file not found: {path}")
        if not path.is_file():
            raise ValueError(f"Expected a file, got a directory: {path}. Use kind='directory'.")
        if path.suffix.lower() not in _TEXT_EXTENSIONS:
            raise ValueError(
                f"Unsupported spec file extension: {path.suffix!r}. "
                f"Supported: {sorted(_TEXT_EXTENSIONS)}. "
                f"For OpenAPI YAML/JSON, use kind='openapi_file'."
            )

        content = path.read_text(encoding="utf-8")
        fmt = "markdown" if path.suffix.lower() in _MARKDOWN_EXTENSIONS else "plaintext"
        return SpecDocument(
            source_ref=ref,
            title=path.stem.replace("_", " ").replace("-", " ").title(),
            content=content,
            format=fmt,
            metadata={"path": str(path), "size_bytes": path.stat().st_size},
            fetched_at=datetime.now(timezone.utc),
        )

    async def _load_directory(self, ref: SpecRef) -> SpecDocument:
        """Load all spec files in a directory.

        content          — full concatenation (used as fallback or for small dirs)
        structured_data  — per-file sections so eigentest can chunk large dirs
                           without hitting LLM context limits

        structured_data shape:
          {
            "sections": [
              {"file": "auth/requirements.md", "content": "...", "char_count": 1234},
              ...
            ],
            "total_char_count": 9999,
          }

        eigentest strategy: if total_char_count exceeds the model's context limit,
        iterate over sections individually rather than passing the combined content.
        """
        directory = Path(ref.location)
        if not directory.exists():
            raise FileNotFoundError(f"Spec directory not found: {directory}")
        if not directory.is_dir():
            raise ValueError(f"Expected a directory, got a file: {directory}. Use kind='file'.")

        files = sorted(
            f for f in directory.rglob("*")
            if f.is_file() and f.suffix.lower() in _TEXT_EXTENSIONS
        )
        if not files:
            raise ValueError(
                f"No spec files found in {directory}. "
                f"Expected files with extensions: {sorted(_TEXT_EXTENSIONS)}"
            )

        combined_parts: list[str] = []
        sections: list[dict] = []
        for file in files:
            relative = str(file.relative_to(directory))
            file_content = file.read_text(encoding="utf-8")
            combined_parts.append(f"## {relative}\n\n{file_content}")
            sections.append({
                "file": relative,
                "content": file_content,
                "char_count": len(file_content),
            })

        combined = "\n\n---\n\n".join(combined_parts)
        return SpecDocument(
            source_ref=ref,
            title=directory.name.replace("_", " ").replace("-", " ").title(),
            content=combined,
            format="markdown",
            structured_data={
                "sections": sections,
                "total_char_count": len(combined),
            },
            metadata={
                "directory": str(directory),
                "file_count": len(files),
                "files": [s["file"] for s in sections],
            },
            fetched_at=datetime.now(timezone.utc),
        )
