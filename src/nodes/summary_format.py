"""AgentCore Platform v1.0"""

# INS-C2-014 — SummaryFormatNode
# Domain node 3: render the generated claims summary in the configured format,
# carrying the statutory fields verbatim.
#
# Returns only changed state keys (partial dict).
#
# Statutory field preservation:
#   decision_basis and disclosure_statement are written into the rendered
#   document unchanged. This node must not modify, abbreviate or truncate them —
#   including when the caller supplies a document length bound, which applies to
#   the narrative summary only. The output gate downstream compares the rendered
#   document against the state fields and refuses the document if they diverge.

import logging
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from framework.utils.audit_logger import emit_trace_event

from src.services.service import ClaimsDocumentService

logger = logging.getLogger(__name__)

_TRUNCATION_NOTICE = "\n\n*[Narrative summary truncated to the requested length.]*"


def _resolve_output_format(state: AgentState, service: ClaimsDocumentService) -> str:
    """Resolve the document format: caller option first, then configuration."""
    state_format = state.get("output_format")
    if isinstance(state_format, str):
        candidate = state_format.strip().lower()
        if candidate in ("markdown", "json"):
            return candidate
    return service.default_output_format()


def _bound_narrative(claims_summary: str, max_chars: Optional[int]) -> str:
    """Apply the caller's length bound to the narrative summary only.

    The bound never reaches decision_basis or disclosure_statement: those are
    delivery obligations carried verbatim, and a caller-supplied length must not
    be able to shorten them.
    """
    if not max_chars or len(claims_summary) <= max_chars:
        return claims_summary
    keep = max(0, max_chars - len(_TRUNCATION_NOTICE))
    return claims_summary[:keep] + _TRUNCATION_NOTICE


def _format_as_markdown(
    claims_summary: str,
    decision_basis: str,
    disclosure_statement: str,
) -> str:
    """Render the claims summary as a structured Markdown document.

    decision_basis and disclosure_statement are included verbatim.
    """
    lines: List[str] = [
        "# 保険金請求サマリー / Insurance Claims Summary",
        "",
        claims_summary,
        "",
        "---",
        "",
        "## 決定根拠 / Decision Basis",
        "*(保険業法第294条準拠 — 書面による決定根拠 / Pursuant to Insurance Business Act Art.294)*",
        "",
        decision_basis,
        "",
        "---",
        "",
        "## 法定開示事項 / Statutory Disclosure Statement",
        "*(保険業法第294条準拠 / Pursuant to Insurance Business Act Art.294)*",
        "",
        disclosure_statement,
        "",
        "---",
        "",
        "*本サマリーは保険業法第294条に基づき作成されました。最終的な保険金支払決定は、"
        "権限を有する査定担当者がレビューおよび承認を行います。*",
        "*This summary is prepared pursuant to Insurance Business Act Article 294. "
        "The final claims determination is subject to review and approval by authorized claims handlers.*",
    ]
    return "\n".join(lines)


def _format_as_json(
    claims_summary: str,
    decision_basis: str,
    disclosure_statement: str,
) -> Dict[str, Any]:
    """Render the claims summary as a structured JSON object.

    decision_basis and disclosure_statement are included verbatim as top-level
    keys.
    """
    return {
        "claims_summary": claims_summary,
        "decision_basis": decision_basis,
        "disclosure_statement": disclosure_statement,
        "next_steps": [
            "Claims handler to review and validate the determination.",
            "Contact claimant within the statutory notification period.",
            "Provide written notification with Decision Basis and Disclosure Statement.",
        ],
        "compliance_note": (
            "This document complies with 保険業法第294条 (Insurance Business Act Art.294). "
            "decision_basis and disclosure_statement are mandatory statutory fields "
            "and must be delivered to the claimant verbatim."
        ),
    }


class SummaryFormatNode(FunctionNode):
    """Render the generated claims summary in the configured format.

    Reads the format from the caller's validated options or from
    config/config.yaml. Supports "markdown" (default) and "json".

    Input state keys:
        claims_summary: str       — narrative summary
        decision_basis: str       — decision rationale, carried verbatim
        disclosure_statement: str — statutory disclosure, carried verbatim
        output_format: str        — "markdown" | "json"
        max_summary_chars: int    — optional bound on the narrative section

    Output state keys (partial dict):
        result: str | dict — the rendered document
        status: str        — success
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState, config: Optional[Dict[str, Any]] = None) -> dict[str, Any]:
        claims_summary = state.get("claims_summary")
        decision_basis = state.get("decision_basis")
        disclosure_statement = state.get("disclosure_statement")

        missing_fields = [
            name
            for name, value in (
                ("claims_summary", claims_summary),
                ("decision_basis", decision_basis),
                ("disclosure_statement", disclosure_statement),
            )
            if not value
        ]

        if missing_fields:
            emit_trace_event(
                "claims_render_skipped",
                {"node": self.__class__.__name__, "missing": missing_fields},
                {},
            )
            logger.warning("SummaryFormatNode: missing required fields: %s", missing_fields)
            return {
                "error_log": [
                    f"SummaryFormatNode: missing required upstream fields {missing_fields}. "
                    "ClaimsSummaryGenerateNode must run first."
                ],
                "status": AgentStatus.ERROR.value,
                # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                # error_log reaches no one: the terminal result carries just `status`, and get_output()
                # does not copy error_log out of the graph -- the caller sees a blank spinner.
                "formatted_output": "Request could not be completed. "
                + (
                    f"SummaryFormatNode: missing required upstream fields {missing_fields}. ClaimsSummaryGenerateNode must run first."
                ),
            }

        service = ClaimsDocumentService((config or {}).get("configurable", {}).get("agent_config"))
        output_format = _resolve_output_format(state, service)

        max_chars = state.get("max_summary_chars")
        narrative = _bound_narrative(str(claims_summary), max_chars if isinstance(max_chars, int) else None)

        if output_format == "json":
            result: Any = _format_as_json(
                claims_summary=narrative,
                decision_basis=str(decision_basis),
                disclosure_statement=str(disclosure_statement),
            )
        else:
            result = _format_as_markdown(
                claims_summary=narrative,
                decision_basis=str(decision_basis),
                disclosure_statement=str(disclosure_statement),
            )

        emit_trace_event(
            "claims_summary_rendered",
            {
                "node": self.__class__.__name__,
                "output_format": output_format,
                "narrative_truncated": narrative != claims_summary,
            },
            state,
        )
        logger.info(
            "SummaryFormatNode: rendered as %s (narrative_truncated=%s)",
            output_format,
            narrative != claims_summary,
        )

        return {
            "result": result,
            "status": AgentStatus.SUCCESS.value,
        }
