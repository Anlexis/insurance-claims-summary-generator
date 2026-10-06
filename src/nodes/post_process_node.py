"""AgentCore Platform v1.0"""

# INS-C2-014 — PostProcessNode (outer graph post_process slot)
# The output gate: the last check before a claims document is released.
#
# What this node enforces, and why it is the whole point of the template:
#   The insurer must deliver a written decision basis and the statutory
#   disclosure text alongside the coverage determination. A summary that reached
#   the claimant WITHOUT them, or with them altered, would be worse than no
#   summary at all — it would look like a compliant notice and would not be one.
#   So the gate treats those two fields as a release condition, not as content:
#
#     1. both fields are present and non-empty in state;
#     2. the rendered document carries both of them VERBATIM — proving nothing
#        shortened, reformatted or dropped them between generation and rendering;
#     3. the rendered document carries no credential-shaped value.
#
#   Any of the three failing returns ERROR, and the document is withheld from
#   the caller. Withholding happens in InsC2014Agent.get_output(), which resolves
#   the delivered document only on success — see the note there on why
#   containment lives in exactly one place. The refusal delta carries the
#   closed-set envelope _contain(REASON_OUTPUT_WITHHELD) in formatted_output so
#   get_output() can name the gate as the reason; WHICH condition failed goes to
#   error_log (internal) and the audit event, never to the caller.
#
# Note on the framework contract: _extra_security_gate_output() is a hook the
# framework CALLS on this node's own returned dict, not a filter a node calls on
# state. The framework's default output gate raises on credential patterns; it
# never strips domain fields, so there is nothing for a template to "preserve" it
# from. The verbatim check below is what actually guarantees delivery.

import logging
from typing import Any, ClassVar, Dict, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from framework.security.credential_detector import detect_credentials_in_value
from framework.utils.audit_logger import emit_trace_event

from src.services.caller_contract import REASON_OUTPUT_WITHHELD, _contain

logger = logging.getLogger(__name__)

# Fields of the rendered document that must reproduce the state field verbatim.
_STATUTORY_FIELDS = ("decision_basis", "disclosure_statement")


class PostProcessNode(FunctionNode):
    """Output gate for the claims summary document.

    Input state keys:
        result: str | dict        — the rendered document from SummaryFormatNode
        decision_basis: str       — written decision rationale
        disclosure_statement: str — statutory disclosure text

    Output state keys (partial dict):
        status: str            — success, or error when the document is refused
        error_log: list        — populated on refusal, naming the failing
                                 condition (internal channel; not projected)
        formatted_output: dict — on refusal only: {"reason": "output_withheld"},
                                 the closed-set envelope get_output() publishes

    The node never returns document text: the document stays in state and is
    resolved for delivery by the graph's get_output(). That keeps the refusal
    path from carrying the very content it is refusing.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _refuse(self, reason: str, detail: str) -> dict[str, Any]:
        """Emit the refusal audit event and return the error delta.

        `detail` names the failing condition and the field. It never contains the
        document, the field contents, or a matched value.
        """
        emit_trace_event(
            "claims_document_refused",
            {"node": self.__class__.__name__, "reason": reason},
            {},
        )
        logger.error("PostProcessNode: claims document refused (%s)", reason)
        return {
            "status": AgentStatus.ERROR.value,
            "error_log": [f"PostProcessNode: {detail}"],
            "formatted_output": _contain(REASON_OUTPUT_WITHHELD),
        }

    def execute(self, state: AgentState, config: Optional[Dict[str, Any]] = None) -> dict[str, Any]:
        decision_basis = state.get("decision_basis")
        disclosure_statement = state.get("disclosure_statement")
        document = state.get("result")

        # ── 1. Both statutory fields present ─────────────────────────────────
        missing = [
            name
            for name, value in (
                ("decision_basis", decision_basis),
                ("disclosure_statement", disclosure_statement),
            )
            if not value
        ]
        if missing:
            return self._refuse(
                "statutory_field_missing",
                f"the claims document cannot be released: required fields {missing} "
                "are missing. The decision basis and the statutory disclosure must "
                "both be delivered with the determination.",
            )

        if not document:
            return self._refuse(
                "document_missing",
                "the claims document cannot be released: no rendered document was " "produced by the formatting step.",
            )

        # ── 2. Both fields reproduced verbatim in the rendered document ──────
        altered = self._fields_not_verbatim(
            document,
            {
                "decision_basis": str(decision_basis),
                "disclosure_statement": str(disclosure_statement),
            },
        )
        if altered:
            return self._refuse(
                "statutory_field_altered",
                f"the claims document cannot be released: fields {altered} do not "
                "appear verbatim in the rendered document. These fields must be "
                "delivered exactly as generated.",
            )

        # ── 3. No credential-shaped value in the rendered document ───────────
        # Uses the framework's own detector rather than a local pattern set. A
        # narrower local set would let a value through here that the framework
        # then raises on further along, and that raise discards this node's delta
        # entirely — a detector gap is a containment bypass, not a smaller net.
        if detect_credentials_in_value(document):
            return self._refuse(
                "credential_in_document",
                "the claims document cannot be released: it contains a "
                "credential-shaped value. Remove it from the source documents "
                "before resubmitting.",
            )

        emit_trace_event(
            "claims_document_released",
            {
                "node": self.__class__.__name__,
                "decision_basis_chars": len(str(decision_basis)),
                "disclosure_chars": len(str(disclosure_statement)),
                "document_format": "json" if isinstance(document, dict) else "markdown",
            },
            state,
        )
        logger.info(
            "PostProcessNode: claims document released (decision_basis=%d chars, " "disclosure=%d chars)",
            len(str(decision_basis)),
            len(str(disclosure_statement)),
        )

        return {"status": AgentStatus.SUCCESS.value}

    @staticmethod
    def _fields_not_verbatim(document: Any, fields: Dict[str, str]) -> list[str]:
        """Return the names of statutory fields the document does not reproduce exactly.

        Markdown documents must CONTAIN the field text; JSON documents must carry
        it as the identical value under the same key. An unrecognised document
        shape fails closed — every field is reported as unverifiable rather than
        assumed present.
        """
        if isinstance(document, str):
            return [name for name, value in fields.items() if value not in document]
        if isinstance(document, dict):
            return [name for name, value in fields.items() if document.get(name) != value]
        return sorted(fields)

    def _extra_security_gate_output(self, result: dict[str, Any]) -> dict[str, Any]:
        """Domain output check on this node's own returned delta.

        The framework calls this after its default credential scan. This node is
        the release boundary, so the invariant asserted here is that the gate
        itself never carries document text: the delta must be status, error_log
        and — on refusal — the closed-set envelope, and nothing else. A future
        edit that started returning the document from this node, or free text in
        the formatted_output slot, would move content onto the refusal path,
        where the graph's get_output() publishes the reason it finds there.
        """
        unexpected = sorted(set(result) - {"status", "error_log", "formatted_output", "node_history", "execution_time"})
        if unexpected:
            raise ValueError(f"PostProcessNode must not return document-bearing keys: {unexpected}")
        if "formatted_output" in result and result["formatted_output"] != _contain(REASON_OUTPUT_WITHHELD):
            raise ValueError("PostProcessNode may only return the closed-set refusal envelope in formatted_output")
        return result
