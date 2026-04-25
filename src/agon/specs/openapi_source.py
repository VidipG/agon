"""
OpenAPI / Swagger specification source.

Handles:
  kind="openapi_file" — path to a local YAML or JSON file
  kind="openapi_url"  — URL to a live OpenAPI spec endpoint

The spec is parsed into structured_data (the full dict) so that
eigentest's spec_extractor can perform mechanical extraction of
endpoint contracts (status codes, request/response schemas, required
fields) without re-parsing the raw text.

Content is also serialized to a markdown summary so the LLM chain
can process it alongside the structured data.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ..triggers.base import SpecRef
from .base import SpecDocument

_HANDLED_KINDS = {"openapi_file", "openapi_url"}


class OpenAPISpecSource:
    """Parses OpenAPI / Swagger specs from local files or URLs."""

    def can_handle(self, ref: SpecRef) -> bool:
        return ref.kind in _HANDLED_KINDS

    async def fetch(self, ref: SpecRef) -> SpecDocument:
        if ref.kind == "openapi_file":
            raw, path_str = self._read_file(ref.location)
            source_label = path_str
        else:
            raw, source_label = await self._fetch_url(ref.location)

        spec_dict = self._parse(raw, ref.location)
        content = self._to_markdown(spec_dict, source_label)
        title = spec_dict.get("info", {}).get("title", "OpenAPI Specification")

        return SpecDocument(
            source_ref=ref,
            title=title,
            content=content,
            format="openapi",
            structured_data=spec_dict,
            metadata={
                "openapi_version": spec_dict.get("openapi") or spec_dict.get("swagger", "unknown"),
                "api_version": spec_dict.get("info", {}).get("version", "unknown"),
                "endpoint_count": len(spec_dict.get("paths", {})),
                "source": source_label,
            },
            fetched_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def _read_file(location: str) -> tuple[str, str]:
        path = Path(location)
        if not path.exists():
            raise FileNotFoundError(f"OpenAPI spec file not found: {path}")
        return path.read_text(encoding="utf-8"), str(path)

    @staticmethod
    async def _fetch_url(url: str) -> tuple[str, str]:
        import httpx

        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            response = await client.get(url)
            response.raise_for_status()
        return response.text, url

    @staticmethod
    def _parse(raw: str, location: str) -> dict[str, Any]:
        loc_lower = location.lower()
        try:
            if loc_lower.endswith(".json") or (raw.strip().startswith("{")):
                return json.loads(raw)
            return yaml.safe_load(raw)
        except Exception as exc:
            raise ValueError(f"Failed to parse OpenAPI spec from {location!r}: {exc}") from exc

    @staticmethod
    def _to_markdown(spec: dict[str, Any], source: str) -> str:
        """Produce a human-readable markdown summary of the OpenAPI spec.

        This summary is what the LLM chain receives. The full structured_data
        is available for mechanical extraction by spec_extractor.
        """
        info = spec.get("info", {})
        lines: list[str] = [
            f"# {info.get('title', 'API Specification')}",
            "",
            f"**Version:** {info.get('version', 'unknown')}  ",
            f"**Source:** {source}",
        ]

        if description := info.get("description", ""):
            lines += ["", description]

        paths: dict[str, Any] = spec.get("paths", {})
        if paths:
            lines += ["", "## Endpoints", ""]
            for path, methods in sorted(paths.items()):
                for method, operation in methods.items():
                    if not isinstance(operation, dict):
                        continue
                    summary = operation.get("summary", "")
                    op_id = operation.get("operationId", "")
                    header = f"### `{method.upper()} {path}`"
                    if summary:
                        header += f" — {summary}"
                    lines.append(header)
                    if op_id:
                        lines.append(f"operationId: `{op_id}`")

                    # Request body
                    if rb := operation.get("requestBody", {}):
                        required = rb.get("required", False)
                        lines.append(f"Request body: {'required' if required else 'optional'}")

                    # Parameters
                    params = operation.get("parameters", [])
                    if params:
                        required_params = [p["name"] for p in params if p.get("required")]
                        if required_params:
                            lines.append(f"Required parameters: {', '.join(required_params)}")

                    # Responses
                    responses: dict[str, Any] = operation.get("responses", {})
                    if responses:
                        status_codes = sorted(responses.keys())
                        resp_parts: list[str] = []
                        for code in status_codes:
                            desc = responses[code].get("description", "")
                            resp_parts.append(f"`{code}` {desc}".strip())
                        lines.append(f"Responses: {' | '.join(resp_parts)}")
                    lines.append("")

        components = spec.get("components", spec.get("definitions", {}))
        if components:
            schema_names = list(
                components.get("schemas", components).keys()
                if "schemas" in components else components.keys()
            )
            if schema_names:
                lines += ["## Schemas", ""]
                lines.append(", ".join(f"`{n}`" for n in sorted(schema_names)))
                lines.append("")

        return "\n".join(lines)
