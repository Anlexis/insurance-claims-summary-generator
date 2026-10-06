"""The caller-visible error is a closed set: constants this template chose, nothing else.

Every non-success outcome of an invoke reaches the caller as
``error: {"reason": <constant>}`` with ``output: None`` and no ``error_log`` key.

``error_log`` is node-authored text: a refusal notice names a field, which for a
JSON intake can be a caller-supplied key; an inner-graph failure carries an
exception's message and a traceback; a trust denial carries the framework's
wording. A filter over such text — truncation, path stripping, credential
redaction — is not a closed set. So the log stays in state as the internal
channel and is never projected.

The sentinel below is assembled at runtime and is deliberately NOT
credential-shaped: a redaction-only "fix" would pass it through, and the
framework's own output scan (which runs on every node delta) does not stop it
from reaching state. These tests must fail unless the text is absent because it
was never projected.
"""

import json
from typing import Any, Iterator

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext, TrustLevel
from src.nodes.post_process_node import PostProcessNode
from src.services.caller_contract import ERROR_REASONS, REASON_OUTPUT_WITHHELD, REASON_WORKFLOW_FAILED, _contain

ENVELOPE_KEYS = {"output", "status", "trace_id", "correlation_id", "node_history"}
ERROR_ENVELOPE_KEYS = ENVELOPE_KEYS | {"error"}


def _sentinel() -> str:
    """An error_log line an upstream failure could plausibly write, assembled at runtime."""
    name = " ".join(["A.", "Tanaka"])
    email = "@".join(["a.tanaka", "example.test"])
    reference = "-".join(["CLM", "2026", "0917", "XK"])
    return "boom: upstream said {'customer': '" + name + "', 'email': '" + email + "', 'ref': '" + reference + "'}"


SENTINEL = _sentinel()
SENTINEL_FRAGMENTS = (SENTINEL, "Tanaka", "example.test", "CLM-2026-0917-XK")

# A structured intake whose caller-chosen key lands in the refusal notice. The
# control token is JSON-escaped so the raw text passes both the framework's S-2
# screen and this template's raw-string screen; the JSON parse decodes it and
# the per-field screen refuses it, naming the field it was under.
KEYED_INJECTION = (
    '{"claim_type": "auto", "incident_date": "2026-06-15", "Zq_sentinel_key_7": "<\\u007cim_start\\u007c>"}'
)

BASIS = "【決定根拠】auto claim covered under section 4"
DISCLOSURE = "【法定開示事項】disclosure text"


def _strings(value: Any) -> Iterator[str]:
    """Every string reachable in a value — mapping keys and values, nested containers."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _strings(item)
    elif value is not None:
        yield str(value)


def _assert_absent(mapping: dict, *fragments: str) -> None:
    found = list(_strings(mapping))
    for fragment in fragments:
        assert not any(fragment in text for text in found), f"{fragment!r} reached the caller: {found}"


def _assert_closed(envelope: dict, reason: str) -> None:
    assert envelope["status"] != AgentStatus.SUCCESS.value
    assert envelope["output"] is None
    assert envelope["error"] == {"reason": reason}
    assert envelope["error"]["reason"] in ERROR_REASONS
    assert "error_log" not in envelope
    assert set(envelope) == ERROR_ENVELOPE_KEYS


class TestTheWalkSeesNestedText:
    """The probe that keeps every absence assertion below honest."""

    def test_nested_values_and_keys_are_walked(self):
        nested = {"a": [{"b": ("x", {"c": SENTINEL})}], SENTINEL: 1, "n": [None, 42]}
        found = list(_strings(nested))
        assert sum(1 for text in found if SENTINEL in text) == 2
        assert "42" in found


class TestContainHelper:
    @pytest.mark.parametrize("reason", sorted(ERROR_REASONS))
    def test_builds_a_truthy_single_key_envelope_for_each_declared_reason(self, reason):
        envelope = _contain(reason)
        assert envelope == {"reason": reason}
        assert envelope, "a falsy envelope would re-open the framework's `formatted_output or result` fallback"

    def test_a_reason_outside_the_set_is_refused_without_being_echoed(self):
        with pytest.raises(ValueError) as exc:
            _contain(SENTINEL)
        assert SENTINEL not in str(exc.value)


# ── The invoke envelope, driven directly ──────────────────────────────────────


def _terminal_state(**overrides: Any) -> dict:
    """State as finalize leaves it after a non-success run: the log and a rendered document in place."""
    state: dict[str, Any] = {
        "status": AgentStatus.ERROR.value,
        "error_log": [SENTINEL, "PreProcessNode: user_input.Zq_sentinel_key_7: contains a chat-template control token"],
        "result": "# 保険金請求サマリー\n\n" + BASIS + "\n" + SENTINEL,
        "decision_basis": BASIS,
        "disclosure_statement": DISCLOSURE,
        "trace_id": "trace-1",
        "correlation_id": "corr-1",
        "node_history": ["InitializeNode", "PreProcessNode", "ClaimsSummaryGraphNode", "FinalizeNode"],
    }
    state.update(overrides)
    return state


GATE_HISTORY = ["InitializeNode", "PreProcessNode", "ClaimsSummaryGraphNode", "PostProcessNode", "FinalizeNode"]

TRUST_DENIAL = "[PreProcessNode] S-1 trust gate denied: required=VERIFIED_EXTERNAL, caller=ANONYMOUS"

NON_SUCCESS_STATES = [
    pytest.param(_terminal_state(), REASON_WORKFLOW_FAILED, id="inner-error-routed-to-finalize"),
    pytest.param(
        _terminal_state(formatted_output=_contain(REASON_OUTPUT_WITHHELD), node_history=GATE_HISTORY),
        REASON_OUTPUT_WITHHELD,
        id="gate-refusal",
    ),
    pytest.param(_terminal_state(status=AgentStatus.TIMEOUT.value), REASON_WORKFLOW_FAILED, id="timeout"),
    pytest.param(_terminal_state(status=AgentStatus.CANCELLED.value), REASON_WORKFLOW_FAILED, id="cancelled"),
    pytest.param(_terminal_state(error_log=[TRUST_DENIAL], result=None), REASON_WORKFLOW_FAILED, id="trust-denied"),
    pytest.param(_terminal_state(formatted_output=SENTINEL), REASON_WORKFLOW_FAILED, id="formatted_output-is-text"),
    pytest.param(_terminal_state(formatted_output={"reason": SENTINEL}), REASON_WORKFLOW_FAILED, id="foreign-reason"),
    pytest.param(_terminal_state(formatted_output={"reason": 42}), REASON_WORKFLOW_FAILED, id="non-string-reason"),
    pytest.param(_terminal_state(formatted_output={}), REASON_WORKFLOW_FAILED, id="empty-formatted_output"),
    pytest.param(
        _terminal_state(formatted_output={"reason": REASON_OUTPUT_WITHHELD, "detail": SENTINEL}),
        REASON_OUTPUT_WITHHELD,
        id="extra-keys-are-not-projected",
    ),
]


class TestInvokeEnvelope:
    @pytest.mark.parametrize("state, expected_reason", NON_SUCCESS_STATES)
    def test_non_success_carries_a_closed_set_reason_and_nothing_else(self, agent, state, expected_reason):
        envelope = agent.get_output(state)

        _assert_closed(envelope, expected_reason)
        assert envelope["status"] == state["status"]
        assert envelope["node_history"] == state["node_history"]

    @pytest.mark.parametrize("state, expected_reason", NON_SUCCESS_STATES)
    def test_nothing_from_error_log_or_state_reaches_the_caller(self, agent, state, expected_reason):
        envelope = agent.get_output(state)

        _assert_absent(
            envelope,
            *SENTINEL_FRAGMENTS,
            BASIS,
            "Zq_sentinel_key_7",
            "control token",
            "trust gate",
            "ANONYMOUS",
            "PreProcessNode:",
        )

    def test_the_success_envelope_is_the_base_one(self, agent):
        state = _terminal_state(status=AgentStatus.SUCCESS.value, error_log=[], result="# document")

        envelope = agent.get_output(state)

        assert set(envelope) == ENVELOPE_KEYS
        assert envelope["output"] == "# document"
        assert envelope["status"] == AgentStatus.SUCCESS.value


# ── The gate's refusal delta ──────────────────────────────────────────────────


def _document(basis: str = BASIS, disclosure: str = DISCLOSURE) -> str:
    return f"# Document\n\n{basis}\n\n{disclosure}\n"


def _credential_shaped() -> str:
    # Assembled so the committed file never carries the literal.
    return "Bearer " + "abcdefghijklmnop" + "1234567890"


def _gate_state(**overrides: Any) -> dict:
    state: dict[str, Any] = {
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "status": AgentStatus.SUCCESS.value,
        "decision_basis": BASIS,
        "disclosure_statement": DISCLOSURE,
        "result": _document(),
        "error_log": [SENTINEL],
        "correlation_id": "corr-gate",
    }
    state.update(overrides)
    return state


GATE_REFUSALS = [
    pytest.param({"decision_basis": None}, id="decision_basis-missing"),
    pytest.param({"disclosure_statement": ""}, id="disclosure_statement-missing"),
    pytest.param({"decision_basis": None, "disclosure_statement": None}, id="both-statutory-fields-missing"),
    pytest.param({"result": None}, id="document-missing"),
    pytest.param({"result": _document(basis="【決定根拠】altered")}, id="statutory-field-altered"),
    pytest.param({"result": _document() + "\n" + _credential_shaped()}, id="credential-in-document"),
    pytest.param({"result": {"claims_summary": "x", "decision_basis": BASIS}}, id="json-document-missing-a-field"),
    pytest.param({"result": 12345}, id="unrecognised-document-shape"),
]


class TestGateRefusalDelta:
    @pytest.mark.parametrize("drive", ["execute", "framework-pipeline"])
    @pytest.mark.parametrize("overrides", GATE_REFUSALS)
    def test_every_refusal_carries_the_closed_set_envelope(self, overrides, drive):
        node = PostProcessNode()
        state = _gate_state(**overrides)

        delta = node.execute(state) if drive == "execute" else node(state)

        assert delta["status"] == AgentStatus.ERROR.value
        assert delta["formatted_output"] == _contain(REASON_OUTPUT_WITHHELD)
        assert delta["formatted_output"], "the envelope must stay truthy"
        assert len(delta["error_log"]) == 1, "the inner entries are not re-emitted (the reducer appends)"
        assert delta["error_log"][0].startswith("PostProcessNode:")

    @pytest.mark.parametrize("drive", ["execute", "framework-pipeline"])
    @pytest.mark.parametrize("overrides", GATE_REFUSALS)
    def test_the_delta_carries_no_document_text_and_nothing_from_error_log(self, overrides, drive):
        node = PostProcessNode()
        state = _gate_state(**overrides)

        delta = node.execute(state) if drive == "execute" else node(state)

        _assert_absent(delta, *SENTINEL_FRAGMENTS, BASIS, DISCLOSURE, "Bearer")

    def test_a_released_document_writes_no_envelope(self):
        delta = PostProcessNode().execute(_gate_state())

        assert delta == {"status": AgentStatus.SUCCESS.value}


class TestGateAdmitsNothingElseInTheEnvelopeSlot:
    def test_the_refusal_delta_passes_the_nodes_own_gate(self):
        node = PostProcessNode()
        delta = node._refuse("document_missing", "the claims document cannot be released: no rendered document")

        assert node._extra_security_gate_output(dict(delta)) == delta

    @pytest.mark.parametrize(
        "delta",
        [
            pytest.param({"status": "error", "formatted_output": {"reason": SENTINEL}}, id="foreign-reason"),
            pytest.param({"status": "error", "formatted_output": SENTINEL}, id="free-text"),
            pytest.param({"status": "error", "formatted_output": {}}, id="falsy-envelope"),
            pytest.param({"status": "error", "formatted_output": None}, id="none"),
            pytest.param(
                {"status": "error", "formatted_output": {"reason": REASON_OUTPUT_WITHHELD, "document": BASIS}},
                id="envelope-with-a-document-key",
            ),
            pytest.param(
                {"status": "error", "formatted_output": {"reason": REASON_WORKFLOW_FAILED}},
                id="a-reason-this-node-does-not-own",
            ),
        ],
    )
    def test_anything_else_is_refused_without_being_echoed(self, delta):
        with pytest.raises(ValueError) as exc:
            PostProcessNode()._extra_security_gate_output(delta)
        assert SENTINEL not in str(exc.value)
        assert BASIS not in str(exc.value)


# ── Every path through a real invoke ──────────────────────────────────────────


class TestEveryInvokePathIsClosedSet:
    def test_a_refused_intake(self, invoke):
        envelope = invoke("not a claims document at all")

        _assert_closed(envelope, REASON_WORKFLOW_FAILED)
        _assert_absent(envelope, "PreProcessNode:", "claims document", "keywords")

    def test_a_rejected_caller_option(self, invoke, valid_claim_json):
        envelope = invoke(valid_claim_json, {"output_format": "pdf"})

        _assert_closed(envelope, REASON_WORKFLOW_FAILED)
        _assert_absent(envelope, "output_format", "pdf", "ClaimsIntakeParseNode:")

    def test_a_caller_supplied_key_in_a_refusal_notice_is_not_projected(self, invoke):
        from src.nodes.pre_process_node import PreProcessNode

        # The channel is real: the refusal notice names the caller's key.
        notice = PreProcessNode().execute({"user_input": KEYED_INJECTION})
        assert notice["status"] == AgentStatus.ERROR.value
        assert "Zq_sentinel_key_7" in notice["error_log"][0]

        envelope = invoke(KEYED_INJECTION)

        _assert_closed(envelope, REASON_WORKFLOW_FAILED)
        _assert_absent(envelope, "Zq_sentinel_key_7", "control token", "PreProcessNode:")

    def test_an_inner_failure_after_rendering_bypasses_the_gate(self, invoke, valid_claim_json, monkeypatch):
        from src.graph.graph import ClaimsSummaryGraphNode

        original = ClaimsSummaryGraphNode.merge_output

        def failing_merge(self, state, sub_result):
            delta = original(self, state, sub_result)
            delta["status"] = AgentStatus.ERROR.value
            delta["error_log"] = [SENTINEL]
            delta["result"] = "# 保険金請求サマリー\n\n" + BASIS + "\n" + SENTINEL
            return delta

        monkeypatch.setattr(ClaimsSummaryGraphNode, "merge_output", failing_merge)
        envelope = invoke(valid_claim_json)

        assert "PostProcessNode" not in envelope["node_history"], "this case must bypass the gate"
        _assert_closed(envelope, REASON_WORKFLOW_FAILED)
        _assert_absent(envelope, *SENTINEL_FRAGMENTS, BASIS, "決定根拠")

    def test_a_gate_refusal(self, invoke, valid_claim_json, monkeypatch):
        from src.graph.graph import ClaimsSummaryGraphNode

        original = ClaimsSummaryGraphNode.merge_output

        def dropping_merge(self, state, sub_result):
            delta = original(self, state, sub_result)
            delta["disclosure_statement"] = None
            delta["error_log"] = [SENTINEL]
            return delta

        monkeypatch.setattr(ClaimsSummaryGraphNode, "merge_output", dropping_merge)
        envelope = invoke(valid_claim_json)

        assert "PostProcessNode" in envelope["node_history"], "this case must reach the gate"
        _assert_closed(envelope, REASON_OUTPUT_WITHHELD)
        _assert_absent(envelope, *SENTINEL_FRAGMENTS, "決定根拠", "disclosure_statement", "PostProcessNode:")

    def test_a_trust_denial(self, agent, valid_claim_json):
        ctx = InvocationContext(session_id="anonymous-caller", caller_trust_level=TrustLevel.ANONYMOUS)

        envelope = agent.invoke(valid_claim_json, ctx=ctx, input_context={})

        _assert_closed(envelope, REASON_WORKFLOW_FAILED)
        _assert_absent(envelope, "trust gate", "ANONYMOUS", "VERIFIED_EXTERNAL")

    def test_the_success_envelope_is_unchanged(self, invoke, valid_claim_json):
        envelope = invoke(valid_claim_json)

        assert envelope["status"] == AgentStatus.SUCCESS.value
        assert set(envelope) == ENVELOPE_KEYS
        assert envelope["output"]
        assert "決定根拠" in envelope["output"]

    def test_the_serialised_body_carries_no_sentinel(self, invoke, valid_claim_json, monkeypatch):
        """The blunt check on top of the walk: the JSON a transport would send."""
        from src.graph.graph import ClaimsSummaryGraphNode

        original = ClaimsSummaryGraphNode.merge_output

        def failing_merge(self, state, sub_result):
            delta = original(self, state, sub_result)
            delta["status"] = AgentStatus.ERROR.value
            delta["error_log"] = [SENTINEL]
            return delta

        monkeypatch.setattr(ClaimsSummaryGraphNode, "merge_output", failing_merge)
        body = json.dumps(invoke(valid_claim_json), ensure_ascii=False, default=str)

        for fragment in SENTINEL_FRAGMENTS:
            assert fragment not in body
