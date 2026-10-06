"""Shared fixtures for the INS-C2-014 test suite."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# The canonical well-formed claim used across the suite. Kept identical to
# deploy/invoke_payload.json so the deployment smoke check exercises the same
# request shape the tests assert on.
VALID_CLAIM = {
    "claim_type": "auto",
    "incident_date": "2026-06-15",
    "policy_number": "POL-887766",
    "damage_description": (
        "Rear bumper, trunk lid and left tail-lamp assembly damaged in a "
        "stop-light collision at the Shinjuku 3-chome intersection. Both "
        "vehicles remained drivable and no airbag deployed."
    ),
    "coverage_rules_excerpt": (
        "Section 4: collision damage to the insured vehicle is covered where the "
        "insured party was stationary at the time of impact."
    ),
}

VALID_CLAIM_JSON = json.dumps(VALID_CLAIM, ensure_ascii=False)

# A free-form Japanese intake, the other supported input shape.
FREEFORM_CLAIM = (
    "事故報告書\n"
    "事故発生日: 2026-06-10\n"
    "証券番号: POL-554433\n"
    "事故内容: 自動車事故による損害を報告します。交差点で追突を受けました。\n"
)


@pytest.fixture
def valid_claim_json() -> str:
    """A well-formed structured claims intake payload."""
    return VALID_CLAIM_JSON


@pytest.fixture
def freeform_claim() -> str:
    """A well-formed free-form claims intake document."""
    return FREEFORM_CLAIM


@pytest.fixture
def agent():
    """A compiled agent instance loaded with the repository's runtime config."""
    from src.graph.graph import InsC2014Agent
    from src.services.service import load_runtime_config

    instance = InsC2014Agent(config=load_runtime_config())
    instance.compile()
    return instance


@pytest.fixture
def invoke(agent):
    """Invoke the compiled agent as a trusted caller and return the envelope."""
    from framework.schemas.invocation_context import InvocationContext, TrustLevel

    def _invoke(user_input: str, input_context: dict | None = None) -> dict:
        ctx = InvocationContext(
            session_id="test-session",
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        )
        return agent.invoke(user_input, ctx=ctx, input_context=input_context or {})

    return _invoke
