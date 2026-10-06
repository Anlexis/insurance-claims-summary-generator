"""AgentCore Platform v1.0"""

# Service layer: domain configuration and document assets.
# Must NOT contain business logic, routing, or credentials.
#
# Runtime tuning lives in config/config.yaml and the static manifest in
# config/agent.yaml. The manifest is flat and carries no nested `agent:` block,
# so a reader that walks `agent.config.*` finds nothing and silently returns
# defaults — the declared max_retry, timeout and claims limits would never reach
# the pipeline. Every runtime value is therefore read from config/config.yaml
# through this module, which is the only place that knows where the file lives.

from __future__ import annotations

import functools
import logging
import pathlib
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# src/services/service.py -> src/services -> src -> repository root
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_CONFIG_PATH = _REPO_ROOT / "config" / "config.yaml"

# Fallbacks used only when config/config.yaml is unreadable. They mirror the
# values committed in that file so behaviour does not change silently if the
# file is missing; a warning is logged so the condition is visible.
_DEFAULT_MAX_COVERAGE_EXCERPT_CHARS = 2000
_DEFAULT_OUTPUT_FORMAT = "markdown"
_DEFAULT_DISCLOSURE_TEMPLATE = "prompts/disclosure_294.j2"


@functools.lru_cache(maxsize=1)
def load_runtime_config() -> dict[str, Any]:
    """Read config/config.yaml. Returns {} (with a warning) if it cannot be read."""
    try:
        with _CONFIG_PATH.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle)
    except FileNotFoundError:
        logger.warning("runtime configuration not found at %s; using built-in defaults", _CONFIG_PATH)
        return {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("runtime configuration at %s could not be read (%s); using built-in defaults", _CONFIG_PATH, exc)
        return {}
    if not isinstance(loaded, dict):
        logger.warning("runtime configuration at %s is not a mapping; using built-in defaults", _CONFIG_PATH)
        return {}
    return loaded


class ClaimsDocumentService:
    """Domain service: resolves configured limits and loads document assets.

    Instantiated by the nodes that need it. The configuration mapping is
    injected so a caller (or a test) can supply its own without touching the
    committed file; when omitted, config/config.yaml is used.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config: dict[str, Any] = config if config is not None else load_runtime_config()

    # ── Configured limits ────────────────────────────────────────────────────

    def max_coverage_excerpt_chars(self) -> int:
        """Character cap applied to the policy-coverage excerpt carried into the summary."""
        claims = self._config.get("claims")
        if isinstance(claims, dict):
            value = claims.get("max_coverage_excerpt_chars")
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
        return _DEFAULT_MAX_COVERAGE_EXCERPT_CHARS

    def recognized_claim_types(self) -> frozenset[str]:
        """Claim types the configuration declares as recognised."""
        claims = self._config.get("claims")
        if isinstance(claims, dict):
            declared = claims.get("recognized_claim_types")
            if isinstance(declared, list):
                values = {str(item).strip().lower() for item in declared if str(item).strip()}
                if values:
                    return frozenset(values)
        return frozenset({"auto", "medical", "property", "liability", "travel"})

    def default_output_format(self) -> str:
        """Configured document format: "markdown" or "json"."""
        output = self._config.get("output")
        if isinstance(output, dict):
            value = output.get("format")
            if isinstance(value, str) and value.strip().lower() in ("markdown", "json"):
                return value.strip().lower()
        return _DEFAULT_OUTPUT_FORMAT

    # ── Document assets ──────────────────────────────────────────────────────

    def disclosure_template_path(self) -> str:
        """Configured path to the statutory disclosure text."""
        claims = self._config.get("claims")
        if isinstance(claims, dict):
            value = claims.get("disclosure_template_path")
            if isinstance(value, str) and value.strip():
                return value.strip()
        return _DEFAULT_DISCLOSURE_TEMPLATE

    def load_disclosure_statement(self, fallback: str) -> str:
        """Load the statutory disclosure text, falling back to the built-in wording.

        The disclosure statement is a delivery obligation, so an unreadable
        template must not produce an empty field: the caller-supplied fallback is
        the same statutory wording held in code.
        """
        path = _REPO_ROOT / self.disclosure_template_path()
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("disclosure template at %s could not be read (%s); using the built-in text", path, exc)
            return fallback
        return text or fallback
