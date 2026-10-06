"""End-to-end tests through the real HTTP entry point.

These drive the ASGI application rather than the graph, so they cover the parts
only the adapter owns: caller authentication and the trust level it establishes,
the size cap, the dropping of keys outside the declared contract, and the
credential screen that turns an otherwise opaque framework failure into a
refusal the caller can act on.
"""

import json
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from src.services.caller_contract import ERROR_REASONS, REASON_OUTPUT_WITHHELD, REASON_WORKFLOW_FAILED

AUTH_TOKEN = "test-invoke-token"

# The request shape used for deployment smoke checks. Kept identical to
# deploy/invoke_payload.json.
_VALID_PAYLOAD = {
    "input": json.dumps(
        {
            "claim_type": "auto",
            "incident_date": "2026-06-15",
            "policy_number": "POL-887766",
            "damage_description": (
                "Rear bumper, trunk lid and left tail-lamp assembly damaged in a "
                "stop-light collision at the Shinjuku 3-chome intersection. Both "
                "vehicles remained drivable and no airbag deployed."
            ),
            "coverage_rules_excerpt": (
                "Section 4: collision damage to the insured vehicle is covered where "
                "the insured party was stationary at the time of impact."
            ),
        },
        ensure_ascii=False,
    ),
    "session_id": "signoff-ins-c2-014-001",
    "input_context": {
        "output_format": "markdown",
        "policy_reference": "POL_887766",
        "max_summary_chars": 4000,
    },
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", AUTH_TOKEN)
    import src.api.server as server

    return TestClient(server.app)


def _post(client, payload, token=AUTH_TOKEN):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/invoke", json=payload, headers=headers)


class TestHealth:
    def test_health_reports_ok(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


class TestAuthentication:
    def test_valid_token_is_accepted_and_the_claim_is_processed(self, client):
        """The clean path through the real entry point: a full document comes back."""
        response = _post(client, _VALID_PAYLOAD)

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "success", body.get("error")
        document = body["output"]
        assert "決定根拠 / Decision Basis" in document
        assert "法定開示事項" in document
        assert "POL_887766" in document

    @pytest.mark.parametrize("token", [None, "", "wrong-token"])
    def test_missing_or_wrong_token_is_refused(self, client, token):
        response = _post(client, _VALID_PAYLOAD, token=token)
        assert response.status_code == 401

    def test_the_refusal_does_not_say_why(self, client):
        """Absent, malformed and wrong tokens must be indistinguishable."""
        bodies = {_post(client, _VALID_PAYLOAD, token=t).json()["detail"] for t in (None, "", "wrong-token")}
        assert len(bodies) == 1


class TestPayloadLimits:
    def test_oversized_context_is_refused(self, client):
        payload = dict(_VALID_PAYLOAD)
        payload["input_context"] = {"coverage_rules_excerpt": "x" * 300_000}

        response = _post(client, payload)
        assert response.status_code == 413


class TestUndeclaredKeysAreDropped:
    def test_a_key_outside_the_contract_does_not_reach_the_graph(self, client):
        """Undeclared keys are dropped, not ignored.

        An ignored key stays in state and is scanned by the framework's output
        gate on the very first node, so a credential-shaped value in one fails
        the request before any template code runs. Dropping is what makes the
        declared contract the actual contract.
        """
        payload = dict(_VALID_PAYLOAD)
        payload["input_context"] = dict(_VALID_PAYLOAD["input_context"])
        payload["input_context"]["conversation_history"] = (
            "earlier message containing Bearer abcdefghijklmnop1234567890"
        )

        response = _post(client, payload)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "success", (
            "an undeclared key must be dropped, not carried into state where the "
            "framework gate fails the whole request"
        )
        assert "Bearer abcdefghijklmnop1234567890" not in json.dumps(body)


class TestCredentialScreen:
    @pytest.mark.parametrize(
        "value",
        [
            "Bearer abcdefghijklmnop1234567890",
            "sk-abcdefghijklmnopqrstuvwxyz012345",
            "AKIAABCDEFGHIJKLMNOP",
        ],
    )
    def test_credential_in_a_declared_field_is_refused_readably(self, client, value):
        payload = dict(_VALID_PAYLOAD)
        payload["input_context"] = {"coverage_rules_excerpt": value}

        response = _post(client, payload)

        assert response.status_code == 400, "400 rather than 422 — pydantic owns 422 and returns a different body shape"
        detail = response.json()["detail"]
        assert "coverage_rules_excerpt" in detail, "the refusal must name the field"
        assert value not in detail, "the refusal must not echo the value"

    def test_ordinary_domain_text_on_the_same_field_still_passes(self, client):
        """The control: the screen must not refuse real policy excerpts."""
        payload = dict(_VALID_PAYLOAD)
        payload["input_context"] = {
            "coverage_rules_excerpt": (
                "Section 4 covers collision damage. See endorsement 12 and "
                "policy POL-887766 for the applicable deductible."
            ),
        }

        response = _post(client, payload)

        assert response.status_code == 200
        assert response.json()["status"] == "success"


class TestValidationRejectionThroughTheEntryPoint:
    @pytest.mark.parametrize(
        "bad_context",
        [
            {"max_summary_chars": "NaN"},
            {"policy_reference": "has space"},
            {"output_format": "pdf"},
        ],
    )
    def test_invalid_option_is_rejected_and_no_document_is_returned(self, client, bad_context):
        payload = dict(_VALID_PAYLOAD)
        payload["input_context"] = bad_context

        response = _post(client, payload)

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "error"
        assert body["output"] is None

    def test_a_malformed_claim_is_rejected_and_no_document_is_returned(self, client):
        payload = dict(_VALID_PAYLOAD)
        payload["input"] = "The quick brown fox jumps over the lazy dog."
        payload["input_context"] = {}

        response = _post(client, payload)

        body = response.json()
        assert body["status"] == "error"
        assert body["output"] is None


def _sentinel() -> str:
    """An error_log line an upstream failure could write; assembled at runtime, not credential-shaped."""
    name = " ".join(["A.", "Tanaka"])
    email = "@".join(["a.tanaka", "example.test"])
    reference = "-".join(["CLM", "2026", "0917", "XK"])
    return "boom: upstream said {'customer': '" + name + "', 'email': '" + email + "', 'ref': '" + reference + "'}"


SENTINEL = _sentinel()
SENTINEL_FRAGMENTS = (SENTINEL, "Tanaka", "example.test", "CLM-2026-0917-XK")

# A caller-chosen JSON key that the per-field injection screen names in its
# refusal notice; the control token is JSON-escaped so it passes the raw screens.
KEYED_INJECTION = (
    '{"claim_type": "auto", "incident_date": "2026-06-15", "Zq_sentinel_key_7": "<\\u007cim_start\\u007c>"}'
)

ERROR_ENVELOPE_KEYS = {"output", "error", "status", "trace_id", "correlation_id", "node_history"}


def _strings(value: Any) -> Iterator[str]:
    """Every string reachable in a body — mapping keys and values, nested containers."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)
    elif value is not None:
        yield str(value)


class TestErrorEnvelopeIsClosedSet:
    """Through the real entry point, every non-success body is closed-set.

    `output` is null, `error` is `{"reason": <constant>}`, and there is no
    `error_log` key. The sentinel is seeded into `error_log` and `result` by the
    inner-graph mapping — the channel the invoke envelope used to publish — and
    asserted absent from every nested key and value of the body.
    """

    @staticmethod
    def _assert_closed(body: dict, reason: str) -> None:
        assert body["status"] == "error"
        assert body["output"] is None
        assert body["error"] == {"reason": reason}
        assert body["error"]["reason"] in ERROR_REASONS
        assert "error_log" not in body
        assert set(body) == ERROR_ENVELOPE_KEYS

    @staticmethod
    def _assert_absent(body: dict, *fragments: str) -> None:
        found = list(_strings(body))
        for fragment in fragments:
            assert not any(fragment in text for text in found), f"{fragment!r} reached the caller: {found}"

    def test_a_refused_intake(self, client):
        payload = dict(_VALID_PAYLOAD)
        payload["input"] = "The quick brown fox jumps over the lazy dog."
        payload["input_context"] = {}

        body = _post(client, payload).json()

        self._assert_closed(body, REASON_WORKFLOW_FAILED)
        self._assert_absent(body, "PreProcessNode:", "claims document", "keywords")

    def test_a_rejected_caller_field(self, client):
        payload = dict(_VALID_PAYLOAD)
        payload["input_context"] = {"output_format": "pdf"}

        body = _post(client, payload).json()

        self._assert_closed(body, REASON_WORKFLOW_FAILED)
        self._assert_absent(body, "output_format", "pdf", "ClaimsIntakeParseNode:")

    def test_a_caller_supplied_key_in_a_refusal_notice_is_not_projected(self, client):
        payload = dict(_VALID_PAYLOAD)
        payload["input"] = KEYED_INJECTION
        payload["input_context"] = {}

        body = _post(client, payload).json()

        self._assert_closed(body, REASON_WORKFLOW_FAILED)
        self._assert_absent(body, "Zq_sentinel_key_7", "control token", "PreProcessNode:")

    def test_an_inner_failure_after_rendering_bypasses_the_gate(self, client, monkeypatch):
        from src.graph.graph import ClaimsSummaryGraphNode

        original = ClaimsSummaryGraphNode.merge_output

        def failing_merge(self, state, sub_result):
            delta = original(self, state, sub_result)
            delta["status"] = "error"
            delta["error_log"] = [SENTINEL]
            delta["result"] = "# 保険金請求サマリー\n\n決定根拠\n" + SENTINEL
            return delta

        monkeypatch.setattr(ClaimsSummaryGraphNode, "merge_output", failing_merge)
        response = _post(client, _VALID_PAYLOAD)

        assert response.status_code == 200
        body = response.json()
        assert "PostProcessNode" not in body["node_history"], "this case must bypass the gate"
        self._assert_closed(body, REASON_WORKFLOW_FAILED)
        self._assert_absent(body, *SENTINEL_FRAGMENTS, "決定根拠")
        for fragment in SENTINEL_FRAGMENTS:
            assert fragment not in response.text

    def test_a_gate_refusal(self, client, monkeypatch):
        from src.graph.graph import ClaimsSummaryGraphNode

        original = ClaimsSummaryGraphNode.merge_output

        def dropping_merge(self, state, sub_result):
            delta = original(self, state, sub_result)
            delta["disclosure_statement"] = None
            delta["error_log"] = [SENTINEL]
            return delta

        monkeypatch.setattr(ClaimsSummaryGraphNode, "merge_output", dropping_merge)
        response = _post(client, _VALID_PAYLOAD)

        assert response.status_code == 200
        body = response.json()
        assert "PostProcessNode" in body["node_history"], "this case must reach the gate"
        self._assert_closed(body, REASON_OUTPUT_WITHHELD)
        self._assert_absent(body, *SENTINEL_FRAGMENTS, "決定根拠", "disclosure_statement", "PostProcessNode:")
        for fragment in SENTINEL_FRAGMENTS:
            assert fragment not in response.text

    def test_the_success_body_is_unchanged(self, client):
        body = _post(client, _VALID_PAYLOAD).json()

        assert body["status"] == "success"
        assert set(body) == {"output", "status", "trace_id", "correlation_id", "node_history"}
        assert "決定根拠 / Decision Basis" in body["output"]


class TestDeploymentPayloadStaysInSync:
    def test_invoke_payload_matches_the_tested_request(self):
        """The deployment smoke payload must be the request this suite proves works."""
        import pathlib

        path = pathlib.Path(__file__).resolve().parents[2] / "deploy" / "invoke_payload.json"
        assert json.loads(path.read_text(encoding="utf-8")) == _VALID_PAYLOAD
