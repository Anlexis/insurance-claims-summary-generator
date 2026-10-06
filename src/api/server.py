"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# For platform-level routing, the gateway calls agent.invoke() directly.

import json
import os
import secrets
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory

from src.graph.graph import InsC2014Agent
from src.services.caller_contract import CONTEXT_KEYS, find_credential_field
from src.services.service import load_runtime_config

app = FastAPI(title="Agent")

# The registry loads config/config.yaml and passes it as Graph(config=...); the
# standalone server mirrors that exactly, so declared runtime parameters are live
# in both deployments rather than only in one.
agent = InsC2014Agent(config=load_runtime_config())
agent.compile()
# Namespace / agent_name match the manifest values.
agent.provision_secrets(secrets_factory(namespace="ins", agent_name="InsC2014Agent"))

# Upper bound on the serialized input_context (bytes). The graph enforces
# per-field bounds (identifier alphabet, finite numeric ranges, excerpt caps);
# this is the coarse adapter guard against an oversized payload reaching the
# graph at all.
_MAX_INPUT_CONTEXT_BYTES = 262_144


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    # Caller options, validated field-by-field inside the graph
    # (ClaimsIntakeParseNode): output_format, policy_reference, claim_type_hint,
    # coverage_rules_excerpt, max_summary_chars.
    input_context: dict[str, Any] | None = None


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> Any:
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Standalone-deployment caller auth: when INVOKE_AUTH_TOKEN is set on the
    # server environment, callers that no upstream middleware vouched for (still
    # ANONYMOUS) must present it as a Bearer token and run at VERIFIED_EXTERNAL.
    # Middleware-established trust is never demoted.
    #
    # Required here specifically: every node in this agent declares
    # required_trust_level = VERIFIED_EXTERNAL, and nothing else sets
    # request.state.trust_level in the standalone deployment. Without this
    # boundary every deployed invoke arrives ANONYMOUS, the trust gate denies it,
    # and the agent returns status="error" for well-formed requests.
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would 500 instead of a clean 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — do not reveal whether the token was
            # absent, malformed, or simply wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL

    supplied_context = req.input_context or {}
    if supplied_context and len(json.dumps(supplied_context, default=str)) > _MAX_INPUT_CONTEXT_BYTES:
        raise HTTPException(status_code=413, detail="input_context exceeds the maximum allowed size.")

    # Drop keys this template does not consume. Validators IGNORE undeclared
    # keys, and ignoring is not stripping: an undeclared key stays in state,
    # reaches the first node's returned result, and is scanned there by the
    # framework's output gate. A credential-shaped value in such a key therefore
    # fails the FIRST node — before any template code runs — with a traceback and
    # no usable error. Dropping them here is what makes the declared contract the
    # actual contract.
    input_context = {k: v for k, v in supplied_context.items() if k in CONTEXT_KEYS}

    # Screen what remains for credential shapes, using the framework's own
    # detector so this refusal set matches the framework's block set exactly.
    # The request cannot succeed either way — the framework would refuse it
    # opaquely a moment later — so converting it into a 400 that names the field
    # is strictly better for the caller. 400 rather than 422: pydantic owns 422
    # and returns a list of error objects there, so reusing it would make client
    # handling ambiguous.
    offending = find_credential_field(input_context)
    if offending is not None:
        raise HTTPException(
            status_code=400,
            detail=(f"input_context.{offending} contains a credential-shaped value. " "Remove it and resubmit."),
        )

    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        return agent.invoke(req.input, ctx=ctx, input_context=input_context)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent": "InsC2014Agent"}
