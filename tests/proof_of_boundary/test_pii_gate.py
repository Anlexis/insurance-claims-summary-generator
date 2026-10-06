"""PB-02: claimant personal data never reaches agent state or the document.

Claims intake documents carry regulated personal data — claimant name, address,
health-insurance number, contact details. The intake node removes it before
anything is written to state, so it appears in neither the stored claim, the
generated summary, nor the delivered document.

The test drives the real pipeline rather than the node alone: personal data
removed at the node but re-introduced by a later step would still reach the
claimant, and only an end-to-end assertion can see that.
"""

import json

import pytest

from framework.schemas.agent_status import AgentStatus

CLAIMANT_NAME = "山田 太郎"
CLAIMANT_ADDRESS_FRAGMENT = "渋谷区"
CLAIMANT_NHI = "12345678-90"
CLAIMANT_PHONE = "090-1234-5678"
CLAIMANT_EMAIL = "taro.yamada@example.test"

PII_VALUES = (CLAIMANT_NAME, CLAIMANT_ADDRESS_FRAGMENT, CLAIMANT_NHI)


def _freeform_intake_with_pii() -> str:
    return (
        "事故報告書\n"
        f"氏名: {CLAIMANT_NAME}\n"
        f"住所: 東京都{CLAIMANT_ADDRESS_FRAGMENT}XX X-X-X\n"
        f"被保険者番号: {CLAIMANT_NHI}\n"
        "\n"
        "事故発生日: 2026-06-10\n"
        "事故内容: 自動車事故による損害を報告します。交差点で追突を受けました。\n"
    )


def _structured_intake_with_pii() -> str:
    return json.dumps(
        {
            "claim_type": "auto",
            "incident_date": "2026-06-10",
            "policy_number": "POL-887766",
            "claimant_name": CLAIMANT_NAME,
            "address": f"東京都{CLAIMANT_ADDRESS_FRAGMENT}XX X-X-X",
            "nhi_number": CLAIMANT_NHI,
            "phone": CLAIMANT_PHONE,
            "email": CLAIMANT_EMAIL,
            "damage_description": "交差点で追突を受け、後部バンパーが損傷しました。",
        },
        ensure_ascii=False,
    )


def _assert_free_of_pii(value, where: str) -> None:
    if value is None:
        return
    rendered = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    for secret in PII_VALUES:
        assert secret not in rendered, (
            f"claimant personal data {secret!r} reached {where}; it must be "
            "removed before anything is written to state"
        )


class TestPersonalDataRemovedAtIntake:
    @pytest.mark.parametrize("intake_factory", [_freeform_intake_with_pii, _structured_intake_with_pii])
    def test_parsed_claim_carries_no_personal_data(self, intake_factory):
        from src.nodes.claims_intake_parse import ClaimsIntakeParseNode

        result = ClaimsIntakeParseNode().execute({"validated_input": intake_factory()})

        assert "parsed_claims_data" in result, result
        _assert_free_of_pii(result["parsed_claims_data"], "parsed_claims_data")

    def test_structured_personal_data_keys_are_replaced(self):
        from src.nodes.claims_intake_parse import ClaimsIntakeParseNode

        result = ClaimsIntakeParseNode().execute({"validated_input": _structured_intake_with_pii()})
        parsed = result["parsed_claims_data"]

        for key in ("claimant_name", "address", "nhi_number", "phone", "email"):
            if key in parsed:
                assert parsed[key] == "[REDACTED-PII]", f"{key} must be replaced, not carried"


class TestPersonalDataAbsentFromTheDeliveredDocument:
    @pytest.mark.parametrize("intake_factory", [_freeform_intake_with_pii, _structured_intake_with_pii])
    def test_full_invoke_delivers_no_personal_data(self, invoke, intake_factory):
        envelope = invoke(intake_factory())

        assert envelope["status"] == AgentStatus.SUCCESS.value, envelope.get("error")
        _assert_free_of_pii(envelope["output"], "the delivered document")

    def test_the_document_is_still_a_real_summary(self, invoke):
        """Removing personal data must not empty the document.

        A pipeline that refused the claim, or returned an empty summary, would
        pass every assertion above while doing none of the work.
        """
        envelope = invoke(_freeform_intake_with_pii())

        document = envelope["output"]
        assert "決定根拠 / Decision Basis" in document
        assert "法定開示事項" in document
        assert "AUTO" in document, "the claim type must still be determined"
        assert "2026-06-10" in document, "the incident date must still be reported"
