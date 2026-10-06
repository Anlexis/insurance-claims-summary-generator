"""PB-03: malformed claims intake is rejected explicitly, never processed silently.

A claims document that cannot be parsed must produce a refusal the caller can
act on, not a summary built from whatever could be salvaged. The refusal is
explicit (status error, a message naming the problem) and leaves no partial
claims data behind for a later step to mistake for a real parse.
"""

import pytest

from framework.schemas.agent_status import AgentStatus

DOMAIN_FIELDS = (
    "validated_input",
    "parsed_claims_data",
    "claims_summary",
    "decision_basis",
    "disclosure_statement",
    "result",
)


def _assert_rejected(result: dict) -> None:
    assert (
        result.get("status") == AgentStatus.ERROR.value
    ), f"malformed intake must be rejected explicitly, got {result.get('status')!r}"
    assert result.get("error_log"), "a rejection must explain itself"


class TestEnvelopeRejection:
    """The envelope check refuses input that is not a usable claims document."""

    @pytest.mark.parametrize(
        "payload, label",
        [
            ("payload with \x00 null bytes and 事故 content", "binary data"),
            ("", "empty string"),
            ("   \n\t  ", "whitespace only"),
            ('["array", "not", "object"]', "JSON array rather than an object"),
            ('{"claim_type": "auto",', "syntactically invalid JSON"),
            ("The quick brown fox jumps over the lazy dog.", "text with no claims content"),
        ],
    )
    def test_invalid_envelope_is_rejected(self, payload, label):
        from src.nodes.pre_process_node import PreProcessNode

        result = PreProcessNode().execute({"user_input": payload})

        _assert_rejected(result)
        assert (
            "validated_input" not in result
        ), f"{label}: validated_input must not be written when the envelope is refused"

    def test_oversized_input_is_rejected(self):
        from src.nodes.pre_process_node import PreProcessNode

        result = PreProcessNode().execute({"user_input": "事故 " * 20_000})

        _assert_rejected(result)
        assert "maximum length" in result["error_log"][0]

    def test_rejection_does_not_echo_the_payload(self):
        """A malformed JSON refusal must not quote the input back.

        The JSON decoder's own message includes the offending fragment, so the
        refusal is written rather than forwarded.
        """
        from src.nodes.pre_process_node import PreProcessNode

        secret = "CONFIDENTIAL-CLAIM-DETAIL"
        result = PreProcessNode().execute({"user_input": '{"a": "' + secret + '",'})

        _assert_rejected(result)
        assert secret not in " ".join(result["error_log"])

    def test_a_valid_envelope_is_accepted(self):
        """The control: the checks above must not refuse a real claims document."""
        from src.nodes.pre_process_node import PreProcessNode

        result = PreProcessNode().execute({"user_input": "事故報告書: 2026-06-10 に自動車事故が発生しました。"})

        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["validated_input"]


class TestSchemaRejection:
    """The parse step refuses a structurally invalid claim."""

    @pytest.mark.parametrize(
        "payload",
        [
            '{"incident_date": "2026-06-10"}',
            '{"claim_type": "auto"}',
            '{"claim_type": "", "incident_date": "2026-06-10"}',
            '{"claim_type": "auto", "incident_date": ""}',
            '{"claim_type": "auto", "incident_date": "2026-06-10", "policy_number": "has space"}',
        ],
    )
    def test_invalid_structured_claim_is_rejected(self, payload):
        from src.nodes.claims_intake_parse import ClaimsIntakeParseNode

        result = ClaimsIntakeParseNode().execute({"validated_input": payload})

        _assert_rejected(result)
        assert "parsed_claims_data" not in result

    @pytest.mark.parametrize(
        "payload",
        [
            "保険の書類ですが、種別も日付も記載がありません。",
            "This claim report contains no recognisable incident date.",
        ],
    )
    def test_unparseable_freeform_claim_is_rejected(self, payload):
        from src.nodes.claims_intake_parse import ClaimsIntakeParseNode

        result = ClaimsIntakeParseNode().execute({"validated_input": payload})

        _assert_rejected(result)
        assert "parsed_claims_data" not in result


class TestNoPartialStateOnRejection:
    def test_rejection_writes_no_domain_fields(self):
        from src.nodes.pre_process_node import PreProcessNode

        result = PreProcessNode().execute({"user_input": '{"malformed":'})

        _assert_rejected(result)
        for field in DOMAIN_FIELDS:
            assert field not in result, (
                f"a refused intake must not write {field!r} — a later step could " "mistake it for a successful parse"
            )

    def test_rejected_intake_produces_no_document_end_to_end(self, invoke):
        envelope = invoke("The quick brown fox jumps over the lazy dog.")

        assert envelope["status"] == AgentStatus.ERROR.value
        assert envelope["output"] is None
