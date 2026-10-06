"""PB-01: statutory delivery — the decision basis and disclosure reach the claimant.

The insurer must deliver a written decision basis and the statutory disclosure
text alongside the coverage determination. This module proves the two fields
survive the whole pipeline unmodified and that the output gate refuses to release
a document in which either has gone missing or been altered.

Both directions matter and both are asserted here: a document that legitimately
carries the fields is released in full, and a document that does not is refused.
A gate that only ever refused would satisfy the first half of that pair and be
useless.
"""

import pytest

from framework.schemas.agent_status import AgentStatus

DECISION_BASIS_HEADING = "決定根拠 / Decision Basis"
DISCLOSURE_HEADING = "法定開示事項"


class TestStatutoryFieldsGenerated:
    """The generation step produces both delivery fields from a parsed claim."""

    def test_generate_produces_both_statutory_fields(self):
        from src.nodes.claims_summary_generate import ClaimsSummaryGenerateNode

        result = ClaimsSummaryGenerateNode().execute(
            {
                "parsed_claims_data": {
                    "claim_type": "auto",
                    "incident_date": "2026-06-15",
                    "policy_number": "POL-887766",
                    "damage_description": "Rear bumper damage.",
                    "coverage_rules_excerpt": "Collision damage is covered.",
                }
            }
        )

        assert result["decision_basis"], "decision_basis must be generated"
        assert result["disclosure_statement"], "disclosure_statement must be generated"
        assert "保険業法第294条" in result["decision_basis"]
        assert "保険業法第294条" in result["disclosure_statement"]

    def test_generate_refuses_without_parsed_claim(self):
        from src.nodes.claims_summary_generate import ClaimsSummaryGenerateNode

        result = ClaimsSummaryGenerateNode().execute({})

        assert result["status"] == AgentStatus.ERROR.value
        assert "decision_basis" not in result
        assert "disclosure_statement" not in result


class TestRenderCarriesFieldsVerbatim:
    """The rendering step reproduces both fields exactly, in both formats."""

    @pytest.mark.parametrize("output_format", ["markdown", "json"])
    def test_render_preserves_fields_verbatim(self, output_format):
        from src.nodes.summary_format import SummaryFormatNode

        basis = "【決定根拠】coverage applies under section 4."
        disclosure = "【法定開示事項】statutory disclosure text."
        result = SummaryFormatNode().execute(
            {
                "claims_summary": "## Incident Overview\nSummary body.",
                "decision_basis": basis,
                "disclosure_statement": disclosure,
                "output_format": output_format,
            }
        )

        document = result["result"]
        if output_format == "json":
            assert document["decision_basis"] == basis
            assert document["disclosure_statement"] == disclosure
        else:
            assert basis in document
            assert disclosure in document

    def test_caller_length_bound_never_shortens_statutory_fields(self):
        """A caller-supplied length bound applies to the narrative only.

        The bound is a rendering preference; the statutory fields are a delivery
        obligation. If the bound could reach them, a caller could request a
        document that looks compliant and is not.
        """
        from src.nodes.summary_format import SummaryFormatNode

        basis = "【決定根拠】" + "B" * 800
        disclosure = "【法定開示事項】" + "D" * 800
        result = SummaryFormatNode().execute(
            {
                "claims_summary": "N" * 5000,
                "decision_basis": basis,
                "disclosure_statement": disclosure,
                "output_format": "markdown",
                "max_summary_chars": 500,
            }
        )

        document = result["result"]
        assert basis in document, "the decision basis must survive the narrative bound"
        assert disclosure in document, "the disclosure must survive the narrative bound"
        assert "truncated to the requested length" in document


class TestOutputGateReleaseDecision:
    """The gate releases a compliant document and refuses a non-compliant one."""

    def test_gate_releases_when_fields_present_and_verbatim(self):
        from src.nodes.post_process_node import PostProcessNode

        basis, disclosure = "basis text", "disclosure text"
        result = PostProcessNode().execute(
            {
                "decision_basis": basis,
                "disclosure_statement": disclosure,
                "result": f"# Document\n\n{basis}\n\n{disclosure}\n",
            }
        )

        assert result["status"] == AgentStatus.SUCCESS.value
        assert "error_log" not in result

    @pytest.mark.parametrize("missing_field", ["decision_basis", "disclosure_statement"])
    def test_gate_refuses_when_a_statutory_field_is_missing(self, missing_field):
        from src.nodes.post_process_node import PostProcessNode

        state = {
            "decision_basis": "basis text",
            "disclosure_statement": "disclosure text",
            "result": "# Document\n\nbasis text\n\ndisclosure text\n",
        }
        state[missing_field] = ""

        result = PostProcessNode().execute(state)

        assert result["status"] == AgentStatus.ERROR.value
        assert missing_field in result["error_log"][0]

    def test_gate_refuses_when_a_field_was_altered_in_transit(self):
        """Presence is not enough — the rendered document must match the source.

        A step that reformatted, re-wrapped or truncated one of the fields would
        leave it present and non-empty while changing what the claimant receives.
        """
        from src.nodes.post_process_node import PostProcessNode

        result = PostProcessNode().execute(
            {
                "decision_basis": "the full decision basis as generated",
                "disclosure_statement": "disclosure text",
                "result": "# Document\n\nthe full decision basis as gener...\n\ndisclosure text\n",
            }
        )

        assert result["status"] == AgentStatus.ERROR.value
        assert "decision_basis" in result["error_log"][0]

    def test_gate_refuses_an_unrecognised_document_shape(self):
        """An unknown document shape fails closed rather than being assumed valid."""
        from src.nodes.post_process_node import PostProcessNode

        result = PostProcessNode().execute(
            {
                "decision_basis": "basis text",
                "disclosure_statement": "disclosure text",
                "result": ["not", "a", "document"],
            }
        )

        assert result["status"] == AgentStatus.ERROR.value

    def test_gate_never_returns_document_bearing_keys(self):
        """The release boundary must not carry content onto the refusal path."""
        from src.nodes.post_process_node import PostProcessNode

        node = PostProcessNode()
        assert node._extra_security_gate_output({"status": "success"}) == {"status": "success"}
        with pytest.raises(ValueError, match="document-bearing keys"):
            node._extra_security_gate_output({"status": "success", "result": "leaked"})


class TestStatutoryDeliveryEndToEnd:
    """The full invoke path delivers both fields inside the returned document."""

    def test_invoke_delivers_both_statutory_sections(self, invoke, valid_claim_json):
        envelope = invoke(valid_claim_json)

        assert envelope["status"] == AgentStatus.SUCCESS.value
        document = envelope["output"]
        assert DECISION_BASIS_HEADING in document
        assert DISCLOSURE_HEADING in document
        assert "PostProcessNode" in envelope["node_history"], "the release decision must be taken at the output gate"

    def test_invoke_json_format_delivers_both_fields(self, invoke, valid_claim_json):
        envelope = invoke(valid_claim_json, {"output_format": "json"})

        assert envelope["status"] == AgentStatus.SUCCESS.value
        document = envelope["output"]
        assert document["decision_basis"]
        assert document["disclosure_statement"]
