# Template Design Specification — INS-C2-014 Insurance Claims Summary Generator

## Position in AgentCore Architecture

- **Agent Class**: `InsC2014Agent` (inherits `AgentBaseGraph` directly)
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |
- **Category**: Cat 2 — nested two-layer architecture
- **Pattern**: DocGeneration (generates structured claims summary documents)

### Three-Layer Separation

| Layer | Principle | Implementation |
|-------|-----------|---------------|
| State | Flat TypedDict composition | `State(AgentState)` — primitives + JSON-serializable only. No PII, no credentials, no Pydantic |
| Node | Framework inheritance (Template Method) | `FunctionNode.execute(self, state: AgentState) -> dict`; partial-dict return only |
| Graph | Composition (Builder) | `register_nodes()` only; `add_edges()` NOT overridden on outer graph |

---

## Cat 2 Two-Layer Architecture

```
Outer backbone (AgentBaseGraph — fixed; do NOT override add_edges()):
  START → initialize → pre_process → main → post_process → finalize → END
                                       ↓ (RETRY, max 3)
                                     pre_process

  main slot = ClaimsSummaryGraphNode(GraphNode)
    │
    └─ get_subgraph() → DomainWorkflowGraph(BaseGraph)  [inner graph]
         │
         └─ START
              → claims_intake_parse    (ClaimsIntakeParseNode)
              → claims_summary_generate (ClaimsSummaryGenerateNode)
              → summary_format         (SummaryFormatNode)
              → END
```

### File Layout

| File | Role |
|------|------|
| `src/graph/graph.py` | Outer graph — `InsC2014Agent(AgentBaseGraph)` + `ClaimsSummaryGraphNode(GraphNode)` |
| `src/graph/domain_workflow_graph.py` | Inner graph — `DomainWorkflowGraph(BaseGraph)` with full 7 ABC methods |
| `src/nodes/claims_intake_parse.py` | Domain node 1 — parse + validate claims intake; strip PII |
| `src/nodes/claims_summary_generate.py` | Domain node 2 — generate summary + 294条 disclosure fields |
| `src/nodes/summary_format.py` | Domain node 3 — format output (markdown / json); preserve 294条 fields |
| `src/nodes/pre_process_node.py` | Outer pre_process — claims intake envelope validation and injection screen |
| `src/nodes/post_process_node.py` | Outer post_process — the output gate (release conditions, below) |
| `src/graph/context_bridge.py` | Carries the caller's `input_context` across the outer→inner boundary |
| `src/services/caller_contract.py` | Validation primitives for caller data (bounds, inert alphabet, screens) |
| `src/services/service.py` | Runtime configuration and document assets (`ClaimsDocumentService`) |
| `src/schemas/state.py` | Flat TypedDict — all agent-specific fields |

---

## Node Configuration

### Outer Graph (AgentBaseGraph backbone)

| Slot | Node Class | Responsibility | Input State Fields | Output State Fields |
|------|-----------|----------------|-------------------|---------------------|
| initialize | InitializeNode (default) | Sets schema_version, session_id, trust_level | — | session_id, schema_version |
| pre_process | PreProcessNode | Envelope validation: type, length, encoding, JSON shape, claims-keyword recognition, injection screen | user_input | validated_input, status |
| main | ClaimsSummaryGraphNode | Wraps DomainWorkflowGraph; delegates full claims summary workflow | validated_input | result, decision_basis, disclosure_statement, status |
| post_process | PostProcessNode | Output gate: verifies the statutory fields are present, reproduced verbatim in the document, and that the document carries no credential-shaped value | result, decision_basis, disclosure_statement | status, error_log, formatted_output (`{"reason": "output_withheld"}` on refusal only) |
| finalize | FinalizeNode (default) | Builds response_metadata, total_time_ms | — | response_metadata |

### Inner Graph Domain Nodes (DomainWorkflowGraph)

| Node | Class | Responsibility | Input State Fields | Output State Fields |
|------|-------|----------------|-------------------|---------------------|
| claims_intake_parse | ClaimsIntakeParseNode | Parse 事故報告書/交通事故証明書/診断書; validate the schema and the caller's options; remove personal data (name/address/health-insurance number → anonymised marker) | validated_input, input_context | parsed_claims_data, output_format, max_summary_chars |
| claims_summary_generate | ClaimsSummaryGenerateNode | Derive the coverage determination and generate the narrative, the decision basis and the disclosure statement | parsed_claims_data, config | claims_summary, decision_basis, disclosure_statement |
| summary_format | SummaryFormatNode | Render the document (markdown/json), carrying decision_basis and disclosure_statement verbatim; a caller length bound applies to the narrative only | claims_summary, decision_basis, disclosure_statement, output_format, max_summary_chars | result, status |

---

## State Definition

```python
class State(AgentState):
    # Claims-specific fields
    validated_input: str          # validated claims payload (PreProcessNode)
    parsed_claims_data: dict      # structured parse output (ClaimsIntakeParseNode):
                                  #   claim_type, incident_date, policy_number,
                                  #   damage_description, coverage_rules_excerpt
    claims_summary: str           # generated narrative summary (ClaimsSummaryGenerateNode)
    decision_basis: str           # 294条 decision rationale — carried verbatim to delivery
    disclosure_statement: str     # 294条 statutory disclosure text — carried verbatim to delivery
    output_format: str            # document format: "markdown" | "json"
    max_summary_chars: int        # caller bound on the narrative section (validated)
    status: str                   # AgentStatus value
    trace_id: str                 # audit trail ID
    result: str | dict | None     # rendered document (SummaryFormatNode)
```

**State Constraints (mandatory):**
- Flat TypedDict only (primitives + JSON-serializable types)
- ⚠️ No claimant personal data (name, address, health-insurance number) in State beyond anonymised reference identifiers
- No JWT, API keys, credentials in State (checkpoint DB leakage)
- InvocationContext via `config["configurable"]` only (not in State)
- No Pydantic models, dataclass, arbitrary Python objects (msgpack incompatible)

---

## Output Gate — Release Conditions

**Why this gate exists.** The insurer must deliver a written decision basis and the statutory
disclosure text (保険業法294条, effective June 1 2026) alongside the coverage determination. A summary
that reached the claimant without them — or with them shortened or reworded — would read as a
compliant notice while not being one. That failure is worse than returning nothing, so delivery of
those two fields is treated as a release condition rather than as document content.

**What the gate checks**, in `PostProcessNode.execute()`:

1. `decision_basis` and `disclosure_statement` are present and non-empty in state;
2. the rendered document reproduces **both of them verbatim** — a Markdown document must contain the
   exact text, a JSON document must carry it as the identical value under the same key. This is what
   catches a field that was reformatted or truncated between generation and rendering, which
   presence alone cannot see. An unrecognised document shape fails closed;
3. the rendered document carries no credential-shaped value, checked with the framework's own
   `detect_credentials_in_value()` rather than a local pattern set — a narrower local set would pass
   a value the framework later raises on, and that raise discards the gate's own delta.

Any of the three failing returns status error, and the document is withheld.

**Where withholding happens.** `InsC2014Agent.get_output()` resolves the document only on success.
The framework default resolves it as `formatted_output or result` with no status check, which would
return the refused document alongside `status: error`; and because the backbone routes a non-success
main result straight to finalize, the gate does not even run on that path. Containment therefore
lives in `get_output()` and deliberately **only** there — clearing state in the gate as well would be
redundant and would make each layer individually unfalsifiable, since removing either one alone would
leave the boundary test green.

**What the caller sees on a non-success outcome.** `get_output()` publishes `output: null` and
`error: {"reason": <constant>}` — `output_withheld` when the gate ran and refused, `workflow_failed`
otherwise — and no `error_log` key. The reasons are the closed set `ERROR_REASONS` in
`src/services/caller_contract.py`, built by the single helper `_contain(reason)`; the gate writes that
envelope into `formatted_output` on refusal, and its `_extra_security_gate_output()` admits nothing else
in that slot. `error_log` is node-authored text — a refusal notice can name a caller-supplied JSON key, a
framework failure carries an exception's message — so it stays internal (state and the audit trail)
rather than being filtered on the way out: truncation, path stripping or credential-only redaction is not
a closed set. Any other value found in the `formatted_output` slot on a non-success run is replaced by
`workflow_failed`, never echoed.

**A note on the framework contract.** `_extra_security_gate_output()` is a hook the framework *calls*
on a node's own returned dict; it is not a filter a node calls on state, and the framework's default
output gate raises on credential patterns rather than stripping domain fields. There is consequently
nothing for a template to "preserve" fields from, and calling the hook directly runs the framework's
own no-op. The verbatim check above is what actually guarantees delivery. This template overrides the
hook for its documented purpose: asserting that the release boundary never returns document-bearing
keys.

**Field delivery chain:**
```
ClaimsSummaryGenerateNode (produces decision_basis, disclosure_statement)
  → SummaryFormatNode (writes both verbatim into result)
  → DomainWorkflowGraph.get_output() (emits both explicitly)
  → ClaimsSummaryGraphNode.merge_output() (maps both into outer state)
  → PostProcessNode (verifies presence + verbatim reproduction, then releases)
  → InsC2014Agent.get_output() (resolves the document on success only)
```

---

## Caller Data Contract

Callers supply the claim itself as `input`, and options through `input_context`. The consumed key set
is declared once, in `src/services/caller_contract.py`, and used by both boundaries: the HTTP adapter
**drops** every key outside it before `invoke()`, and `ClaimsIntakeParseNode` validates every key
inside it.

| Field | Rule |
|---|---|
| `output_format` | one of `markdown`, `json` |
| `policy_reference` | 1–32 characters from `A-Z a-z 0-9 _ -` — it renders into the document verbatim |
| `claim_type_hint` | one of the claim types declared in `config/config.yaml` |
| `coverage_rules_excerpt` | text, capped at `claims.max_coverage_excerpt_chars` |
| `max_summary_chars` | a finite number in [500, 50000]; bounds the narrative only, never the statutory fields |

Rules that apply across all of them:

- **Numbers fail closed.** `NaN` and the infinities parse cleanly through `float()` and arrive
  through raw JSON, and every comparison against them is False — an unchecked non-finite value
  silently disables the bound it was meant to enforce rather than tripping it. Booleans are rejected
  too, since `bool` subclasses `int` and would read as 0 or 1.
- **Refusals name the field, never the value.** No rejected value is echoed into an error message,
  a log line, or the response body.
- **Undeclared keys are dropped, not ignored.** An ignored key stays in state and is scanned by the
  framework's output gate on the very first node, so a credential-shaped value in one fails the whole
  request before any template code runs. The adapter also screens the surviving fields for credential
  shapes and returns 400 naming the field, because that request cannot succeed either way and an
  actionable refusal beats an opaque failure.
- **Injection screening covers chat-template control tokens as a class** (`<|…|>`, `[INST]`,
  `<<SYS>>`), not only directive phrases, and runs both on the raw string and after a markup strip:
  the raw pass catches tokens a strip would silently delete, the post-strip pass catches directives
  spliced with markup. Directive patterns are anchored to a line or sentence boundary so that real
  claims prose ("the insurer will act as a subrogee", "insert into the claim file") is not refused.

---

## Numeric Output Invariant

Some templates in this family render monetary aggregates and enforce a rounding grid on the way out.
**This one does not, and must not.** It computes no amounts, aggregates nothing and renders no
currency figure of its own. Every number in the delivered document came from the claim file — an
incident date, a policy reference, a figure quoted inside a damage description or a policy excerpt —
and rounding any of them would misquote the claim.

The invariant here is therefore the absence of a transformation: numbers pass through byte-identical.
`tests/proof_of_boundary/test_output_invariant.py` pins it, including the forms a rounding grid is
known to corrupt when one is introduced carelessly — decimal fractions, decimals followed by a
letter, identifiers shaped `<letters>-<digits>`, a numbered heading following a three-letter code,
and Japanese statutory text carrying figures.

---

## Data Flow

```
user_input (raw claims payload: 事故報告書 / 交通事故証明書 / 診断書 text)
  │
  ▼ PreProcessNode [outer pre_process]
validated_input (schema-checked; rejects malformed intake)
  │
  ▼ ClaimsSummaryGraphNode.extract_input() → DomainWorkflowGraph.invoke()
    │
    ▼ ClaimsIntakeParseNode [inner domain node 1]
    parsed_claims_data {
      claim_type, incident_date, policy_number,
      damage_description, coverage_rules_excerpt
    }
    ⚠️ claimant name/address/NHI → stripped; anonymised IDs only
    │
    ▼ ClaimsSummaryGenerateNode [inner domain node 2]
    claims_summary (narrative)
    decision_basis  ← 294条 field; MUST survive to final output
    disclosure_statement ← 294条 field; MUST survive to final output
    │
    ▼ SummaryFormatNode [inner domain node 3]
    result (markdown or JSON; preserves decision_basis + disclosure_statement verbatim)
    │
    ▼ DomainWorkflowGraph.get_output()
    sub_result { claims_summary, decision_basis, disclosure_statement, status, trace_id }
    │
    ▼ ClaimsSummaryGraphNode.merge_output()
    outer state delta { result, decision_basis, disclosure_statement, status }
    │
    ▼ PostProcessNode._extra_security_gate_output() [outer post_process]
    (injection/credential blocked; 294条 fields preserved)
    │
    ▼ FinalizeNode → response delivered
```

---

## Security Implementation

| Concern | Implementation | Notes |
|---------|---------------|-------|
| Caller authentication | `INVOKE_AUTH_TOKEN` bearer check in `src/api/server.py` | Establishes VERIFIED_EXTERNAL for callers no upstream middleware vouched for. Without it every standalone request arrives ANONYMOUS and the trust gate denies it |
| Trust enforcement | `required_trust_level: TrustLevel.VERIFIED_EXTERNAL` | Declared on the agent and on every node; enforced by the framework before `execute()` |
| Input validation | `PreProcessNode` + `ClaimsIntakeParseNode` | Envelope checks, per-field bounds, inert alphabet for rendered references, finite numeric parsing |
| Injection screening | `src/services/caller_contract.py` | Control tokens as a class plus anchored directive patterns; raw and post-strip passes; applied in the node that owns the caller contract, not left to the framework gate alone |
| Personal-data removal | `ClaimsIntakeParseNode` | Name, address, health-insurance number, contact details replaced before anything is written to state |
| Credential screening | `detect_credentials_in_value()` | At the adapter on `input_context`, and at the output gate on the rendered document |
| Output gate | `PostProcessNode` + `InsC2014Agent.get_output()` | Release conditions above; the document is withheld on any non-success outcome |
| Audit trail | `emit_trace_event()` | A domain event from every boundary node, including each refusal |

---

## Framework Utilization

### Shared Components Used
- [x] `AgentBaseGraph` — outer graph base (direct framework inheritance)
- [x] `BaseGraph` — inner domain workflow graph
- [x] `GraphNode` — `ClaimsSummaryGraphNode` subclass in `main` slot
- [x] `FunctionNode` — all three domain nodes + pre/post process nodes
- [x] `AgentState` — base state TypedDict
- [x] `AgentStatus` — status enum (SUCCESS / ERROR)
- [x] `TrustLevel` — `VERIFIED_EXTERNAL` (claims handlers are authenticated external parties)
- [x] `detect_credentials_in_value()` — the framework's credential detector, used by both the adapter screen and the output gate
- [x] `emit_trace_event()` — domain audit events from every boundary node
- [x] `_extra_security_gate_output()` — the domain output hook, overridden on `PostProcessNode`

### Composition Pattern
- **Pattern**: `GraphNode` subgraph composition (Cat 2 nested)
- **Composition target**: `ClaimsSummaryGraphNode.get_subgraph()` → `DomainWorkflowGraph`
- **Error propagation strategy**: `error_strategy = "handle"` with an `on_subgraph_error()` override.
  The outcome is the same as re-raising — status is error and no document is released — but re-raising
  replaces the inner refusal reason with an implementation-detail string, so the caller cannot tell a
  malformed claim from a rejected option. Handling it keeps the reason intact.

---

## Config + Prompts

### `config/agent.yaml` — the manifest

The manifest is **flat**: the registry reads every key at root level, and a nested `agent:` block
would be invisible to it. It carries identity and compile-time declarations only.

```yaml
id: "INS-C2-014"
name: "Insurance Claims Summary Generator"
namespace: "ins"
category: "Cat 2"
generation_mode: "deterministic"
industry: "INS"
class: "src.graph.graph.InsC2014Agent"
required_trust_level: "VERIFIED_EXTERNAL"
requires:
  secrets: []          # this template calls no ctx.secrets.require()
  extras: []           # it constructs no model client
```

`requires.secrets` and `requires.extras` are both empty because they are derived from the code:
declaring a secret or an extra that is not actually used and provisioned makes the agent fail at
compile time.

### `config/config.yaml` — runtime parameters

Runtime tuning is a separate file, loaded by the registry and passed as `Graph(config=...)`. The
standalone server loads the same file so both deployments behave identically.

```yaml
max_retry: 3
timeout_s: 60
output:
  format: "markdown"          # default; a caller may override per request
claims:
  disclosure_template_path: "prompts/disclosure_294.j2"
  max_coverage_excerpt_chars: 2000
  recognized_claim_types: ["auto", "medical", "property", "liability", "travel"]
```

Every key here has a reader. `ClaimsDocumentService` (`src/services/service.py`) is the only module
that knows where the file lives, and `ClaimsSummaryGraphNode._parent_config()` forwards it into the
inner graph — without that forwarding the inner nodes fall back to their built-in defaults and the
declared values have no effect on any run.

### `prompts/claims_summary.j2`

Jinja2 system prompt generating structured output with all required sections:
- Incident Overview
- Damage Assessment
- Coverage Determination
- Decision Basis (`decision_basis` field — 294条 written rationale)
- Disclosure Statement (`disclosure_statement` field — 294条 statutory text for claimant)
- Next Steps

Template variables: `{{ claim_type }}`, `{{ incident_date }}`, `{{ policy_number }}`,
`{{ damage_description }}`, `{{ coverage_rules_excerpt }}`

---

## Import Isolation Confirmation
- [x] Template imports no platform internals
- [x] All imports are from `framework.*`, `shared.*`, declared dependencies, or the standard library

---

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| Base class | AgentBaseGraph | AutonomousBaseGraph | AgentBaseGraph | Fixed pipeline; no autonomous loop needed |
| Output gate design | Presence check only | Presence + verbatim reproduction + credential scan | Presence + verbatim + credential scan | Presence alone cannot see a field that was reformatted or truncated in transit, which is the failure that would ship a non-compliant notice looking like a compliant one |
| Composition pattern | Flat Cat-1 slots | GraphNode + inner BaseGraph (Cat 2 nested) | Cat 2 nested | Multi-step domain workflow (parse → generate → format) requires inner graph isolation |
| PII handling | Store anonymised IDs in State | Never write claimant PII to State | Never write PII | APPI compliance; PII stripped at parse node before State write |
| Output format | markdown only | markdown + json | markdown + json | JSON mode for programmatic downstream consumption; markdown for human review |
| Trust level | ANONYMOUS | VERIFIED_EXTERNAL | VERIFIED_EXTERNAL | Claims data is sensitive; handlers must be authenticated |
