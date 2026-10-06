"""AgentCore Platform v1.0"""

# INS-C2-014 — PreProcessNode (outer graph pre_process slot)
# Input validation: claims intake envelope check.
# Rejects malformed or structurally invalid claims intake before the inner graph runs.
#
# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return status value strings — never raw enum constants
#  - Never import from mediator/, api/, or other agents

import json
import logging
from typing import Any, ClassVar, Dict, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from framework.utils.audit_logger import emit_trace_event

from src.services.caller_contract import CallerContractError, screen_injection

logger = logging.getLogger(__name__)

# Maximum allowed input length (characters) for the claims intake payload.
# Prevents resource exhaustion from excessively large inputs.
_MAX_INPUT_LENGTH = 50_000

# Free-form intake must look like a claims document. These are recognition
# keywords, not a security control — the security controls are the length cap,
# the binary check and the injection screen below.
_CLAIMS_KEYWORDS = frozenset(
    {
        # Japanese
        "事故",
        "保険",
        "請求",
        "報告書",
        "診断書",
        "損害",
        "証明書",
        "交通",
        "医療",
        "約款",
        "保険金",
        "賠償",
        # English
        "claim",
        "accident",
        "damage",
        "insurance",
        "policy",
        "incident",
        "loss",
        "injury",
        "medical",
        "report",
    }
)


class PreProcessNode(FunctionNode):
    """Validate the claims intake envelope before the domain pipeline runs.

    Accepts:
        user_input: str — raw claims intake (structured JSON or free-form text)

    Returns partial dict:
        validated_input: str — validated claims payload
        status: str          — success

    On validation failure:
        error_log: list[str] — descriptive validation errors, naming the problem
                               but never echoing the rejected content
        status: str          — error
        (no validated_input written — the inner graph will not run)

    Validation rules:
        1. user_input must be a non-empty string
        2. Length must not exceed _MAX_INPUT_LENGTH characters
        3. Must not contain null bytes (binary payload check)
        4. JSON inputs must parse to an object
        5. Plain-text inputs must contain at least one claims keyword
        6. The payload must not carry chat-template control tokens or
           instruction-override directives

    This node does not remove personal data — that is ClaimsIntakeParseNode's
    job. It validates the structural envelope only.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _reject(self, reason: str, detail: str) -> dict[str, Any]:
        """Emit the refusal audit event and return the error delta.

        `detail` describes the problem; it never contains caller content.
        """
        emit_trace_event(
            "claims_intake_rejected",
            {"node": self.__class__.__name__, "reason": reason},
            {},
        )
        logger.warning("PreProcessNode: claims intake rejected (%s)", reason)
        return {
            "status": AgentStatus.ERROR.value,
            "error_log": [f"PreProcessNode: {detail}"],
            # The runner surfaces `formatted_output or result` as `output`. A reason left only in
            # error_log reaches no one: the terminal result carries just `status`, and get_output()
            # does not copy error_log out of the graph -- the caller sees a blank spinner.
            "formatted_output": "Request could not be completed. " + (f"PreProcessNode: {detail}"),
        }

    def execute(self, state: AgentState, config: Optional[Dict[str, Any]] = None) -> dict[str, Any]:
        user_input = state.get("user_input", "")

        # ── Rule 1: non-empty string ──────────────────────────────────────────
        if not user_input or not isinstance(user_input, str):
            return self._reject(
                "missing_or_not_a_string",
                "user_input is missing or not a string. Claims intake must be a "
                "non-empty string (structured JSON or free-form text).",
            )

        stripped = user_input.strip()
        if not stripped:
            return self._reject(
                "empty_after_strip",
                "user_input is empty after stripping whitespace.",
            )

        # ── Rule 2: length cap ────────────────────────────────────────────────
        if len(stripped) > _MAX_INPUT_LENGTH:
            return self._reject(
                "over_length",
                f"user_input exceeds the maximum length of {_MAX_INPUT_LENGTH} "
                f"characters (received {len(stripped)}). Truncate the claims "
                "intake before submission.",
            )

        # ── Rule 3: binary / non-text detection ───────────────────────────────
        null_count = stripped.count("\x00")
        if null_count > 0:
            return self._reject(
                "binary_payload",
                f"user_input contains binary data ({null_count} null bytes). "
                "Claims intake must be plain UTF-8 text.",
            )

        # ── Rule 6: injection screen ──────────────────────────────────────────
        # Run before the format-specific checks so a hostile payload is refused
        # on its own terms rather than as a malformed document. The screen is
        # applied here, in the node that owns the caller contract, rather than
        # relying on the framework gate alone: where that gate is absent or
        # configured off the payload would otherwise reach the answer path and
        # return success — a fail-open on the one decision that must not fail open.
        try:
            screen_injection(stripped, field="user_input")
        except CallerContractError as exc:
            return self._reject("injection_screen", str(exc))

        # ── Rule 4: JSON structural check ─────────────────────────────────────
        looks_like_json = stripped.startswith(("{", "["))
        if looks_like_json:
            try:
                parsed = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                # The decoder's message quotes the offending input, so it is
                # deliberately not forwarded to the caller.
                return self._reject(
                    "malformed_json",
                    "user_input starts as JSON but is not valid JSON. Ensure the "
                    "claims intake is a well-formed JSON object or plain text.",
                )
            if not isinstance(parsed, dict):
                return self._reject(
                    "json_not_an_object",
                    "JSON input must be an object. Structured claims intake must "
                    "be a JSON object, not an array or scalar.",
                )
            # Field-level validation happens in ClaimsIntakeParseNode.
            try:
                screen_injection(parsed, field="user_input")
            except CallerContractError as exc:
                return self._reject("injection_screen", str(exc))
        else:
            # ── Rule 5: plain-text claims keyword check ───────────────────────
            lower = stripped.lower()
            if not any(kw in lower or kw in stripped for kw in _CLAIMS_KEYWORDS):
                return self._reject(
                    "not_a_claims_document",
                    "user_input does not appear to be a claims document. Expected "
                    "claims-related keywords in the text (for example 事故, 保険, "
                    "診断書, claim, accident, damage, insurance).",
                )

        emit_trace_event(
            "claims_intake_validated",
            {
                "node": self.__class__.__name__,
                "length": len(stripped),
                "format": "json" if looks_like_json else "freeform",
            },
            state,
        )
        logger.info(
            "PreProcessNode: validated claims intake (length=%d, format=%s)",
            len(stripped),
            "json" if looks_like_json else "freeform",
        )

        return {
            "validated_input": stripped,
            "status": AgentStatus.SUCCESS.value,
        }
