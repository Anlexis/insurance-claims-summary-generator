"""AgentCore Platform v1.0"""

# INS-C2-014 — ClaimsSummaryGenerateNode
# Domain node 2: generate the structured claims summary and the two statutory
# delivery fields.
#
# Returns only changed state keys (partial dict).
#
# Statutory delivery fields:
#   This node produces both
#     decision_basis       — written rationale for the coverage determination
#     disclosure_statement — statutory disclosure text delivered to the claimant
#   Both are carried verbatim to the output gate, which refuses to release a
#   document in which either is missing or has been altered.
#
# Generation:
#   The summary is synthesised deterministically from the parsed claim, which is
#   what the shipped manifest declares (generation_mode: deterministic) and what
#   makes every output reproducible and testable without a model backend. The
#   section structure it produces is specified in prompts/claims_summary.j2; that
#   file is the starting point for adapting this template to a model-backed
#   generation step, and the integration point is marked in the code below.
#
# Intermediate data:
#   Raw intake documents and intermediate reasoning are not re-emitted by this
#   node. Only the three generated fields are returned.

import logging
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from framework.utils.audit_logger import emit_trace_event

from src.services.service import ClaimsDocumentService

logger = logging.getLogger(__name__)

# ── Statutory disclosure fallback ─────────────────────────────────────────────
# The authoritative text ships in prompts/disclosure_294.j2 and is loaded through
# the document service. This copy is the fallback used when that file cannot be
# read: the disclosure statement is a delivery obligation, so an unreadable asset
# must not silently produce an empty field.
_DEFAULT_DISCLOSURE_STATEMENT = (
    "【保険業法第294条に基づく法定開示事項】\n"
    "本保険金の支払決定については、保険業法第294条の規定に基づき、以下の事項を開示します。\n"
    "1. 支払決定の根拠：本通知に記載の「決定根拠」欄に記載のとおりです。\n"
    "2. 異議申立ての権利：本決定に不服がある場合は、保険会社の苦情処理窓口に対して"
    "異議申立てを行うことができます。\n"
    "3. 金融庁への申出：保険会社の対応に不満がある場合は、金融サービス利用者相談室に"
    "ご相談いただくことができます。\n"
    "[Statutory Disclosure — Insurance Business Act Article 294]\n"
    "This determination is disclosed pursuant to Article 294 of the Insurance Business Act."
)

# ── Coverage determination signals ────────────────────────────────────────────
# Rule-based signals read from the policy-coverage excerpt. Exclusion signals are
# checked first so an excerpt carrying both reads as "review required" rather
# than as covered.

_COVERAGE_POSITIVE_SIGNALS = [
    "covered",
    "eligible",
    "applies",
    "within coverage",
    "補償対象",
    "支払い対象",
    "適用",
    "補填",
]

_COVERAGE_EXCLUSION_SIGNALS = [
    "excluded",
    "not covered",
    "exclusion",
    "免責",
    "支払い対象外",
    "対象外",
    "除外",
]


def _determine_coverage(parsed_data: Dict[str, Any]) -> str:
    """Derive the coverage status from the supplied policy excerpt.

    Deterministic by design: the same claim and the same excerpt always produce
    the same determination, which is what makes the result auditable.
    """
    coverage_excerpt = (parsed_data.get("coverage_rules_excerpt") or "").lower()
    claim_type = parsed_data.get("claim_type", "")

    for signal in _COVERAGE_EXCLUSION_SIGNALS:
        if signal in coverage_excerpt:
            return "potentially excluded — review required"

    for signal in _COVERAGE_POSITIVE_SIGNALS:
        if signal in coverage_excerpt:
            return f"covered under {claim_type} policy terms"

    if not coverage_excerpt:
        return f"coverage eligibility for {claim_type} claim pending policy review"

    return f"coverage applicable subject to policy terms review for {claim_type} claim"


def _build_claims_summary(parsed_data: Dict[str, Any], coverage_status: str) -> str:
    """Build the structured claims summary narrative.

    Model-backed generation replaces this function: render
    prompts/claims_summary.j2 with parsed_data, send it as the system prompt, and
    return the response. The section headings below and the ones in that template
    are the same set on purpose, so the output gate and the boundary tests do not
    change when the generation step is swapped.
    """
    claim_type = parsed_data.get("claim_type", "unknown")
    incident_date = parsed_data.get("incident_date", "not specified")
    policy_number = parsed_data.get("policy_number", "unknown")
    damage_desc = parsed_data.get("damage_description") or "Damage description not provided."

    lines: List[str] = [
        "## Incident Overview",
        f"Claim Type: {claim_type.upper()}",
        f"Incident Date: {incident_date}",
        f"Policy Number: {policy_number}",
        "",
        "## Damage Assessment",
        damage_desc,
        "",
        "## Coverage Determination",
        coverage_status,
        "",
        "## Next Steps",
        "1. Claims handler to review and validate the determination.",
        "2. Contact claimant within the statutory notification period.",
        "3. Provide written notification with Decision Basis and Disclosure Statement.",
    ]
    return "\n".join(lines)


def _build_decision_basis(parsed_data: Dict[str, Any], coverage_status: str) -> str:
    """Build the written decision basis delivered to the claimant.

    This is the rationale for the coverage determination that the insurer must
    provide in writing alongside the decision.
    """
    claim_type = parsed_data.get("claim_type", "unknown")
    incident_date = parsed_data.get("incident_date", "not specified")
    coverage_excerpt = parsed_data.get("coverage_rules_excerpt") or ""

    basis_lines: List[str] = [
        "【決定根拠 / Decision Basis — 保険業法第294条】",
        "",
        f"対象請求種別: {claim_type}",
        f"事故発生日: {incident_date}",
        f"支払判断: {coverage_status}",
        "",
    ]

    if coverage_excerpt:
        basis_lines.append("適用条項 / Applicable Policy Provisions:")
        basis_lines.append(coverage_excerpt[:500])
        basis_lines.append("")

    basis_lines.extend(
        [
            "この決定は、ご提出いただいた書類および保険約款の各条項を総合的に検討した結果です。",
            "This determination is based on a comprehensive review of the submitted documents "
            "and applicable policy provisions.",
        ]
    )

    return "\n".join(basis_lines)


class ClaimsSummaryGenerateNode(FunctionNode):
    """Generate the claims summary and the two statutory delivery fields.

    Input state keys (from ClaimsIntakeParseNode):
        parsed_claims_data: dict — structured, personal-data-free claims data

    Output state keys (partial dict):
        claims_summary: str       — narrative summary (Incident Overview, Damage
                                    Assessment, Coverage Determination, Next Steps)
        decision_basis: str       — written decision rationale
        disclosure_statement: str — statutory disclosure text

    decision_basis and disclosure_statement travel verbatim through
    SummaryFormatNode → the inner graph's get_output() → the outer graph's
    merge_output() → the output gate. Neither may be modified in transit.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState, config: Optional[Dict[str, Any]] = None) -> dict[str, Any]:
        parsed_data = state.get("parsed_claims_data")
        if not parsed_data or not isinstance(parsed_data, dict):
            emit_trace_event(
                "claims_summary_generation_skipped",
                {"node": self.__class__.__name__, "reason": "missing_parsed_claims_data"},
                {},
            )
            logger.warning("ClaimsSummaryGenerateNode: parsed_claims_data missing or invalid")
            return {
                "error_log": [
                    "ClaimsSummaryGenerateNode: parsed_claims_data is missing. " "ClaimsIntakeParseNode must run first."
                ],
                "status": AgentStatus.ERROR.value,
                # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                # error_log reaches no one: the terminal result carries just `status`, and get_output()
                # does not copy error_log out of the graph -- the caller sees a blank spinner.
                "formatted_output": "Request could not be completed. "
                + ("ClaimsSummaryGenerateNode: parsed_claims_data is missing. ClaimsIntakeParseNode must run first."),
            }

        service = ClaimsDocumentService((config or {}).get("configurable", {}).get("agent_config"))

        coverage_status = _determine_coverage(parsed_data)
        claims_summary = _build_claims_summary(parsed_data, coverage_status)
        decision_basis = _build_decision_basis(parsed_data, coverage_status)
        disclosure_statement = service.load_disclosure_statement(_DEFAULT_DISCLOSURE_STATEMENT)

        emit_trace_event(
            "claims_summary_generated",
            {
                "node": self.__class__.__name__,
                "claim_type": parsed_data.get("claim_type", "unknown"),
                "decision_basis_chars": len(decision_basis),
                "disclosure_chars": len(disclosure_statement),
            },
            state,
        )
        logger.info(
            "ClaimsSummaryGenerateNode: generated summary for claim_type=%s "
            "(decision_basis=%d chars, disclosure=%d chars)",
            parsed_data.get("claim_type", "unknown"),
            len(decision_basis),
            len(disclosure_statement),
        )

        return {
            "claims_summary": claims_summary,
            "decision_basis": decision_basis,
            "disclosure_statement": disclosure_statement,
        }
