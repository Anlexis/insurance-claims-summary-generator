"""Containment: a refused claims document is never returned to the caller.

The framework's default output resolution is `formatted_output or result`, with
no status check. Two paths in this agent end in a non-success status while
`result` already holds a fully rendered claims document:

  1. the output gate refuses the document (a statutory field went missing or was
     altered between generation and rendering); and
  2. the inner graph fails after rendering, in which case the backbone routes
     straight to finalize and the gate does not run at all.

Under the default resolution both paths return the complete document alongside
`status: error` — the refused document delivered anyway, which is precisely the
outcome the gate exists to prevent. InsC2014Agent.get_output() resolves the
document only on success.

The fault in these tests is injected on the DATA path, never on the gate itself:
patching the gate would test the patch rather than the agent.
"""

import pytest

from framework.schemas.agent_status import AgentStatus

# Text that must never appear in a non-success envelope.
RELEASED_MARKERS = ("決定根拠", "Decision Basis", "法定開示事項", "Incident Overview")


def _assert_nothing_released(envelope: dict) -> None:
    body = "" if envelope.get("output") is None else str(envelope["output"])
    for marker in RELEASED_MARKERS:
        assert marker not in body, (
            f"containment failure: {marker!r} was released in a " f"{envelope.get('status')!r} envelope"
        )
    assert "Traceback" not in body
    assert "/src/" not in body
    assert "error_log" not in envelope, "the internal channel must not be projected"


class TestRefusedDocumentIsWithheld:
    def test_gate_refusal_withholds_the_document(self, invoke, valid_claim_json, monkeypatch):
        """A statutory field lost in the outer mapping must not ship the document.

        The mapping between the inner graph's output and the outer state is the
        documented failure mode for these fields, so that is where the fault goes.
        """
        from src.graph.graph import ClaimsSummaryGraphNode

        original = ClaimsSummaryGraphNode.merge_output

        def dropping_merge(self, state, sub_result):
            delta = original(self, state, sub_result)
            delta["disclosure_statement"] = None
            return delta

        monkeypatch.setattr(ClaimsSummaryGraphNode, "merge_output", dropping_merge)
        envelope = invoke(valid_claim_json)

        assert envelope["status"] == AgentStatus.ERROR.value
        assert (
            "PostProcessNode" in envelope["node_history"]
        ), "this case must reach the gate — otherwise it is testing the other path"
        _assert_nothing_released(envelope)

    def test_failure_after_rendering_withholds_the_document(self, invoke, valid_claim_json, monkeypatch):
        """When the pipeline fails after rendering, the gate never runs.

        The backbone routes a non-success result from the main slot directly to
        finalize, so post_process is skipped entirely. Containment cannot depend
        on the gate on this path.
        """
        from src.graph.graph import ClaimsSummaryGraphNode

        original = ClaimsSummaryGraphNode.merge_output

        def failing_merge(self, state, sub_result):
            delta = original(self, state, sub_result)
            delta["status"] = AgentStatus.ERROR.value
            return delta

        monkeypatch.setattr(ClaimsSummaryGraphNode, "merge_output", failing_merge)
        envelope = invoke(valid_claim_json)

        assert envelope["status"] == AgentStatus.ERROR.value
        assert (
            "PostProcessNode" not in envelope["node_history"]
        ), "this case must bypass the gate — otherwise it duplicates the other path"
        _assert_nothing_released(envelope)

    def test_input_rejection_releases_nothing(self, invoke):
        envelope = invoke("not a claims document at all")

        assert envelope["status"] == AgentStatus.ERROR.value
        _assert_nothing_released(envelope)


class TestCleanPathControl:
    """The control that keeps the tests above honest.

    Without this, a gate that refused every request would satisfy every
    containment assertion in this module.
    """

    def test_a_legitimate_claim_still_returns_its_full_document(self, invoke, valid_claim_json):
        envelope = invoke(valid_claim_json)

        assert envelope["status"] == AgentStatus.SUCCESS.value
        document = envelope["output"]
        assert document, "a legitimate claim must return a document"
        for marker in RELEASED_MARKERS:
            assert marker in document, f"the released document must contain {marker!r}"
        assert "PostProcessNode" in envelope["node_history"]

    def test_a_legitimate_free_form_claim_still_returns_its_full_document(self, invoke, freeform_claim):
        envelope = invoke(freeform_claim)

        assert envelope["status"] == AgentStatus.SUCCESS.value
        assert "決定根拠" in envelope["output"]


BAD_CONTEXTS = [
    {"policy_reference": "not a valid reference!!"},
    {"max_summary_chars": "NaN"},
    {"output_format": "pdf"},
]


class TestRefusalMessagesCarryNoContent:
    """The refusal names the field internally; the caller gets a reason code only.

    The internal notice (error_log, the audit channel) must name the offending
    field and must never echo the rejected value. The caller-visible envelope
    carries neither: a closed-set reason, `output: None`, and no `error_log`.
    """

    @pytest.mark.parametrize("bad_context", BAD_CONTEXTS)
    def test_the_internal_notice_names_the_field_but_not_the_value(self, valid_claim_json, bad_context):
        from src.nodes.claims_intake_parse import ClaimsIntakeParseNode

        delta = ClaimsIntakeParseNode().execute({"validated_input": valid_claim_json, "input_context": bad_context})

        assert delta["status"] == AgentStatus.ERROR.value
        joined = " ".join(delta["error_log"])
        field = next(iter(bad_context))
        assert field in joined, "the refusal must name the offending field"
        rejected = str(next(iter(bad_context.values())))
        assert rejected not in joined, "the refusal must not echo the rejected value"

    @pytest.mark.parametrize("bad_context", BAD_CONTEXTS)
    def test_the_caller_receives_a_reason_code_and_no_refusal_text(self, invoke, valid_claim_json, bad_context):
        from src.services.caller_contract import REASON_WORKFLOW_FAILED

        envelope = invoke(valid_claim_json, bad_context)

        assert envelope["status"] == AgentStatus.ERROR.value
        assert envelope["output"] is None
        assert envelope["error"] == {"reason": REASON_WORKFLOW_FAILED}
        assert "error_log" not in envelope
        serialised = str(envelope)
        assert next(iter(bad_context)) not in serialised
        assert str(next(iter(bad_context.values()))) not in serialised
