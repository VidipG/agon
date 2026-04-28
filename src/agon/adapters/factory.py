"""
Factory for instantiating LanguageAdapters.
"""
from __future__ import annotations

from ..config import AgonConfig
from .base import LanguageAdapter


def resolve_adapter(cfg: AgonConfig) -> LanguageAdapter:
    """Instantiate the correct LanguageAdapter for the configured language."""
    language = cfg.general.language.lower()
    
    if language == "python":
        from .python import PythonAdapter
        return PythonAdapter()
    
    # Placeholder for future languages
    # if language == "typescript":
    #     from .typescript import TypeScriptAdapter
    #     return TypeScriptAdapter()

    raise ValueError(
        f"Unsupported language: {language!r}. "
        "Currently supported languages: python"
    )
