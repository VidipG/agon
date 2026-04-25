"""Tests for the OpenAPI spec source."""
from __future__ import annotations

from pathlib import Path

import pytest
import respx
import httpx

from agon.specs.openapi_source import OpenAPISpecSource
from agon.triggers.base import SpecRef


@pytest.fixture
def source() -> OpenAPISpecSource:
    return OpenAPISpecSource()


class TestOpenAPISpecSource:
    def test_can_handle_openapi_file(self, source: OpenAPISpecSource) -> None:
        assert source.can_handle(SpecRef(kind="openapi_file", location="/path/spec.yaml")) is True

    def test_can_handle_openapi_url(self, source: OpenAPISpecSource) -> None:
        assert source.can_handle(SpecRef(kind="openapi_url", location="https://example.com/openapi.yaml")) is True

    def test_cannot_handle_other_kinds(self, source: OpenAPISpecSource) -> None:
        for kind in ("file", "directory", "jira_ticket", "linear_ticket", "url"):
            assert source.can_handle(SpecRef(kind=kind, location="x")) is False  # type: ignore

    async def test_load_yaml_file(
        self, source: OpenAPISpecSource, specs_fixtures_dir: Path
    ) -> None:
        ref = SpecRef(kind="openapi_file", location=str(specs_fixtures_dir / "openapi.yaml"))
        doc = await source.fetch(ref)

        assert doc.format == "openapi"
        assert doc.title == "Payment API"
        assert doc.structured_data is not None
        assert "paths" in doc.structured_data
        assert "/payments" in doc.structured_data["paths"]

    async def test_markdown_content_contains_endpoints(
        self, source: OpenAPISpecSource, specs_fixtures_dir: Path
    ) -> None:
        ref = SpecRef(kind="openapi_file", location=str(specs_fixtures_dir / "openapi.yaml"))
        doc = await source.fetch(ref)

        assert "POST /payments" in doc.content
        assert "GET /payments/{id}" in doc.content

    async def test_markdown_content_contains_status_codes(
        self, source: OpenAPISpecSource, specs_fixtures_dir: Path
    ) -> None:
        ref = SpecRef(kind="openapi_file", location=str(specs_fixtures_dir / "openapi.yaml"))
        doc = await source.fetch(ref)

        assert "200" in doc.content
        assert "400" in doc.content
        assert "500" in doc.content

    async def test_metadata_fields(
        self, source: OpenAPISpecSource, specs_fixtures_dir: Path
    ) -> None:
        ref = SpecRef(kind="openapi_file", location=str(specs_fixtures_dir / "openapi.yaml"))
        doc = await source.fetch(ref)

        assert doc.metadata["endpoint_count"] == 2
        assert doc.metadata["api_version"] == "1.0.0"

    async def test_load_json_file(self, source: OpenAPISpecSource, tmp_path: Path) -> None:
        import json
        spec = {
            "openapi": "3.0.3",
            "info": {"title": "Test API", "version": "2.0"},
            "paths": {"/test": {"get": {"responses": {"200": {"description": "ok"}}}}}
        }
        json_file = tmp_path / "spec.json"
        json_file.write_text(json.dumps(spec))

        ref = SpecRef(kind="openapi_file", location=str(json_file))
        doc = await source.fetch(ref)

        assert doc.title == "Test API"
        assert doc.structured_data is not None

    async def test_raises_on_missing_file(self, source: OpenAPISpecSource, tmp_path: Path) -> None:
        ref = SpecRef(kind="openapi_file", location=str(tmp_path / "missing.yaml"))
        with pytest.raises(FileNotFoundError):
            await source.fetch(ref)

    async def test_raises_on_invalid_yaml(self, source: OpenAPISpecSource, tmp_path: Path) -> None:
        bad_file = tmp_path / "bad.yaml"
        bad_file.write_text("{{{{ this is not valid yaml !!!!")
        ref = SpecRef(kind="openapi_file", location=str(bad_file))
        with pytest.raises(ValueError, match="Failed to parse"):
            await source.fetch(ref)

    @respx.mock
    async def test_load_from_url(self, source: OpenAPISpecSource) -> None:
        spec_yaml = """
openapi: "3.0.3"
info:
  title: Remote API
  version: "1.0"
paths:
  /hello:
    get:
      responses:
        "200":
          description: Hello
"""
        url = "https://api.example.com/openapi.yaml"
        respx.get(url).mock(return_value=httpx.Response(200, text=spec_yaml))

        ref = SpecRef(kind="openapi_url", location=url)
        doc = await source.fetch(ref)

        assert doc.title == "Remote API"
        assert doc.structured_data is not None

    @respx.mock
    async def test_url_http_error_propagates(self, source: OpenAPISpecSource) -> None:
        url = "https://api.example.com/openapi.yaml"
        respx.get(url).mock(return_value=httpx.Response(404))

        ref = SpecRef(kind="openapi_url", location=url)
        with pytest.raises(httpx.HTTPStatusError):
            await source.fetch(ref)
