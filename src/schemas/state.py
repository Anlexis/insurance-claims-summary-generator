"""AgentCore Platform v1.0"""

# State must be a flat TypedDict — never a Pydantic model. Graph checkpoints use
# msgpack serialization and model instances corrupt silently across a checkpoint
# round-trip. Extend AgentState with agent-specific fields only. Do NOT add
# credentials, secrets, or model instances.
#
# Personal-data constraint:
#   Claimant personal data (name, address, health-insurance number, contact
#   details) MUST NOT be stored in State. ClaimsIntakeParseNode removes it before
#   writing parsed_claims_data. Only anonymised reference identifiers may appear
#   in any State field.

from typing import Any, Optional

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Agent state for INS-C2-014 Insurance Claims Summary Generator.

    Extends AgentState (which provides user_input, input_context, status,
    session_id, node_history, error_log, formatted_output and the
    human-in-the-loop fields) with claims-specific fields.

    Statutory delivery fields:
        decision_basis and disclosure_statement flow from
        ClaimsSummaryGenerateNode through SummaryFormatNode → inner get_output()
        → outer merge_output() → the output gate in PostProcessNode. The gate
        refuses to release a document in which either field is missing or has
        been altered in transit.

    Personal-data constraint:
        No claimant personal data in State — only anonymised reference
        identifiers. ClaimsIntakeParseNode removes it before writing
        parsed_claims_data.
    """

    # ── Claims processing fields ──────────────────────────────────────────────

    # Written by PreProcessNode (input validation gate):
    validated_input: Optional[str]
    """Validated claims payload. Written by PreProcessNode after envelope
    validation. Contains the raw intake text (event report, medical certificate,
    etc.) with envelope-level validation applied. Never contains claimant
    personal data — the full removal happens in ClaimsIntakeParseNode."""

    # Written by ClaimsIntakeParseNode (inner domain node 1):
    parsed_claims_data: Optional[dict[str, Any]]
    """Structured parse output from ClaimsIntakeParseNode.
    Schema (all values are JSON-serializable primitives):
      claim_type: str             — normalised claim type
      incident_date: str          — ISO 8601 date string or descriptive text
      policy_number: str          — anonymised policy reference
      damage_description: str     — description of loss/damage, personal data removed
      coverage_rules_excerpt: str — relevant policy coverage excerpt (if available)
    Claimant name, address, and health-insurance number are removed before this
    dict is written. Only anonymised reference identifiers appear here."""

    # Written by ClaimsSummaryGenerateNode (inner domain node 2):
    claims_summary: Optional[str]
    """Generated narrative summary text (incident overview, damage assessment,
    coverage determination, next steps)."""

    decision_basis: Optional[str]
    """Written decision rationale delivered to the claimant.
    Required of the insurer alongside the coverage determination, and carried
    verbatim end to end: the output gate refuses to release a document in which
    this field is missing or has been altered."""

    disclosure_statement: Optional[str]
    """Statutory disclosure language delivered to the claimant.
    Carried verbatim end to end under the same rule as decision_basis."""

    # Written by SummaryFormatNode (inner domain node 3):
    result: Optional[object]
    """Final formatted document from SummaryFormatNode.
    - markdown mode: str (structured document with all required sections)
    - json mode: dict with keys claims_summary, decision_basis,
      disclosure_statement, next_steps
    Merged into outer state via ClaimsSummaryGraphNode.merge_output()."""

    # Caller-supplied options, validated in ClaimsIntakeParseNode:
    output_format: Optional[str]
    """Document format: "markdown" (default) or "json". Resolved from the
    caller's input_context when supplied, otherwise from config/config.yaml."""

    max_summary_chars: Optional[int]
    """Upper bound on the rendered document length, from the caller's
    input_context. Validated as finite and within range before use."""

    # Audit / tracing fields:
    trace_id: Optional[str]
    """Audit trail identifier for this claims processing session. Inherited from
    the invocation context in most cases; surfaced separately for downstream
    audit systems."""
