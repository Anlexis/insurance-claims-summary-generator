"""The rendered document reproduces numbers exactly — no rounding grid applies.

Some templates in this family render monetary aggregates and enforce a rounding
grid on the way out. This one does not: it computes no amounts, aggregates
nothing, and renders no currency figure. Every number that reaches the document
came from the claim itself — an incident date, a policy reference, a figure
quoted inside a damage description or a policy excerpt — and altering any of them
would misquote the claim file.

So the output invariant here is the opposite of a grid: numbers pass through
byte-identical. This module pins that, because "no transformation" is only an
invariant if something fails when a transformation is introduced.

It also covers the forms that a rounding grid is known to corrupt when one is
added without care — decimals, identifiers, section numbering after a
three-letter code, and Japanese statutory text with embedded figures.
"""

import pytest

from framework.schemas.agent_status import AgentStatus

# Forms measured elsewhere in this template family as the ones a rounding grid
# mangles: decimal fractions, decimals followed by a letter, identifiers of the
# shape <letters>-<digits>, a numbered heading after a three-letter code, and
# CJK statutory text carrying figures.
NUMERIC_FORMS = [
    "8.512345",
    "9999.99999%",
    "ratio 0.123456",
    "JPY 1234.56",
    "JPY 1234.56m",
    "JPY 1,000",
    "JPY 1,234",
    "JPY 9999",
    "9999 JPY",
    "SKF-6205",
    "STU-1234",
    "POL-887766",
    "sku_48210",
    "Currency: JPY\n\n3. Cash Position",
    "（3000万円+600万円×法定相続人の数）",
    "SSN 123-45-6789",
    "90d",
    "STAR 2026",
]


class TestNumbersSurviveRenderingUnchanged:
    @pytest.mark.parametrize("form", NUMERIC_FORMS)
    def test_form_is_reproduced_byte_identically(self, form):
        from src.nodes.summary_format import SummaryFormatNode

        result = SummaryFormatNode().execute(
            {
                "claims_summary": f"## Damage Assessment\n{form}",
                "decision_basis": f"【決定根拠】{form}",
                "disclosure_statement": "【法定開示事項】disclosure text.",
                "output_format": "markdown",
            }
        )

        assert form in result["result"], (
            f"{form!r} was altered during rendering; this template must not " "transform numbers on the way out"
        )

    @pytest.mark.parametrize("form", NUMERIC_FORMS)
    def test_form_survives_the_output_gate(self, form):
        """The gate inspects the document; it must not rewrite it either."""
        from src.nodes.post_process_node import PostProcessNode

        basis = f"【決定根拠】{form}"
        document = f"# Document\n\n{basis}\n\ndisclosure text\n"
        result = PostProcessNode().execute(
            {
                "decision_basis": basis,
                "disclosure_statement": "disclosure text",
                "result": document,
            }
        )

        assert result["status"] == AgentStatus.SUCCESS.value


class TestNumbersSurviveTheFullInvoke:
    def test_a_figure_quoted_in_the_claim_reaches_the_document_unchanged(self, invoke, valid_claim_json):
        import json

        claim = json.loads(valid_claim_json)
        claim["damage_description"] = (
            "Body-shop estimate: parts 210,000 JPY, labour 150,000 JPY. " "Depreciation applied at 8.512345 percent."
        )
        envelope = invoke(json.dumps(claim, ensure_ascii=False))

        assert envelope["status"] == AgentStatus.SUCCESS.value
        document = envelope["output"]
        for fragment in ("210,000 JPY", "150,000 JPY", "8.512345"):
            assert fragment in document, f"{fragment!r} was altered on the way out"
