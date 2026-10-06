# Test Specification — INS-C2-014 Insurance Claims Summary Generator

## Overview

This document specifies the test cases (TC) and proof-of-boundary (PB) tests for INS-C2-014.

**Template:** INS-C2-014 Insurance Claims Summary Generator
**Pattern:** Cat 2 DocGen (nested two-layer graph)
**Framework:** AgentCore SDK 1.0.2 (the version the pipeline installs)

---

## Test Cases (TC)

### TC-01: Auto Claim — Valid structured intake

**Objective:** Verify that a valid auto claim structured intake produces a complete summary with all sections including decision_basis and disclosure_statement.

**Input:**
```json
{
  "claim_type": "auto",
  "incident_date": "2026-06-10",
  "policy_number": "POL-987654",
  "damage_description": "Vehicle rear-ended at intersection; airbags deployed; rear bumper and trunk damaged.",
  "coverage_rules_excerpt": "Collision coverage applies to damage resulting from impact with another vehicle."
}
```

**Expected outcome:**
- `status` = SUCCESS
- `claims_summary` is a non-empty string containing Incident Overview, Damage Assessment, Coverage Determination, Next Steps sections
- `decision_basis` is a non-empty string containing written coverage rationale (294条)
- `disclosure_statement` is a non-empty string containing statutory disclosure text (294条)
- `result` is a non-empty string (markdown) or dict (json) containing all required sections
- No claimant PII in any State field

**Node path:** PreProcessNode → ClaimsIntakeParseNode → ClaimsSummaryGenerateNode → SummaryFormatNode

---

### TC-02: Medical Claim — Diagnosis certificate only (no traffic certificate)

**Objective:** Verify that a medical claim with only a 診断書 (no 交通事故証明書) is processed with graceful handling of the missing document type.

**Input:**
```
診断書
事故発生日: 2026-05-15
診断名: 腰椎捻挫
治療期間: 6週間

医療保険請求
入院: 3日間
外来治療: 12回
```

**Expected outcome:**
- `status` = SUCCESS
- `claim_type` in `parsed_claims_data` = "medical"
- `incident_date` extracted correctly (2026-05-15)
- `claims_summary` contains all required sections
- `decision_basis` and `disclosure_statement` are non-empty
- No claimant PII in `parsed_claims_data` or downstream fields
- Missing 交通事故証明書 handled gracefully (no ERROR from its absence)

---

### TC-03: Markdown output format

**Objective:** Verify that `output_format=markdown` produces properly structured markdown output with all required sections.

**Input:** Valid auto claim JSON (same as TC-01)
**Config:** `output_format: markdown` (default)

**Expected outcome:**
- `result` is a string (not dict)
- `result` contains all section headers: `## Incident Overview`, `## Damage Assessment`, `## Coverage Determination`, `## 決定根拠 / Decision Basis`, `## 法定開示事項 / Statutory Disclosure Statement`, `## Next Steps`
- `decision_basis` content appears in the `## 決定根拠` section
- `disclosure_statement` content appears in the `## 法定開示事項` section

---

### TC-04: JSON output format

**Objective:** Verify that `output_format=json` produces valid JSON output with all required keys.

**Input:** Valid auto claim JSON (same as TC-01)
**Config:** `output_format: json`

**Expected outcome:**
- `result` is a dict (not string)
- `result` contains keys: `claims_summary`, `decision_basis`, `disclosure_statement`, `next_steps`, `compliance_note`
- `result["decision_basis"]` is non-empty
- `result["disclosure_statement"]` is non-empty
- Output is JSON-serializable (`json.dumps(result)` succeeds)

---

### TC-05: Partial claims data — graceful degradation

**Objective:** Verify that a minimal intake (only claim_type and incident_date) produces a summary with appropriate notes for missing fields.

**Input:**
```json
{
  "claim_type": "property",
  "incident_date": "2026-06-01"
}
```

**Expected outcome:**
- `status` = SUCCESS
- `claim_type` = "property"
- `parsed_claims_data["policy_number"]` = "unknown" (default for missing)
- `parsed_claims_data["damage_description"]` is empty string or default
- `claims_summary` generated successfully (no crash on missing fields)
- `decision_basis` and `disclosure_statement` are non-empty

---

## Proof-of-Boundary Tests (PB)

### PB-01: statutory delivery

**Objective:** verify that `decision_basis` and `disclosure_statement` reach the claimant unaltered,
and that the output gate refuses to release a document in which either is missing or has been
changed in transit.

**Why presence alone is not the test.** A field that was reformatted, re-wrapped or truncated between
generation and rendering is still present and non-empty. The gate therefore checks that the rendered
document reproduces both fields **verbatim**: a Markdown document must contain the exact text, a JSON
document must carry it as the identical value under the same key.

**Scenarios**

| Scenario | Expectation |
|---|---|
| Valid auto claim, markdown | Document released; both statutory sections present |
| Valid auto claim, json | Document released; both fields present as top-level keys |
| Caller supplies `max_summary_chars` | Narrative is bounded; both statutory fields are untouched |
| A statutory field is empty in state | Refused; the message names the missing field |
| A statutory field appears altered in the document | Refused; the message names the altered field |
| Document is of an unrecognised shape | Refused (fails closed) |

**Fail condition:** a document is released while either field is absent from state or is not
reproduced verbatim in the document.

**Implementation:** `tests/proof_of_boundary/test_294_preservation.py`

---

### PB-01b: output containment

**Objective:** verify that a refused document is never returned to the caller.

**Why this is a separate test.** The framework resolves the delivered document as
`formatted_output or result` with no status check, and the backbone routes a non-success main result
straight to finalize — so the gate does not run at all on that path. Both routes end with a fully
rendered document sitting in state alongside `status: error`.

**Scenarios**

| Scenario | Expectation |
|---|---|
| A statutory field is lost in the outer mapping (gate runs and refuses) | Status error, `output` is None, no statutory text in the envelope |
| The pipeline fails after rendering (gate is bypassed) | Status error, `output` is None, no statutory text in the envelope |
| A valid claim (the control) | Status success, full document returned, gate present in `node_history` |

The control is required: without it, a gate that refused every request would satisfy every other
assertion in this section.

**Implementation:** `tests/proof_of_boundary/test_output_containment.py`

---

### PB-01c: closed-set error envelope

**Objective:** verify that the caller-visible error carries only constants this template chose —
`error: {"reason": r}` with `r ∈ ERROR_REASONS`, `output: null`, and no `error_log` key — on every
non-success path, and that nothing written to `error_log` or left in state reaches the envelope.

**Scenarios**

| Scenario | Expectation |
|---|---|
| Refused intake (`pre_process`) | `workflow_failed`; no refusal text in the body |
| Rejected caller option (`input_context`) | `workflow_failed`; neither the field name nor the value in the body |
| Caller-supplied JSON key named in a refusal notice | the key is in the internal notice and absent from the body |
| Inner failure after rendering (gate bypassed) | `workflow_failed`; sentinel absent; no statutory text |
| Gate refusal (each of its conditions, direct and through the framework pipeline) | delta carries `formatted_output = {"reason": "output_withheld"}` (truthy), one internal `error_log` line, no document text; invoke → `output_withheld` |
| Trust-gate denial | `workflow_failed`; the framework's wording absent |
| `timeout` / `cancelled` status | `workflow_failed` |
| Free text, a foreign reason, a non-string reason or extra keys in `formatted_output` | replaced by / reduced to a closed-set reason, never echoed |
| The gate's own output hook | admits only the closed-set refusal envelope in `formatted_output` |
| Success (the control) | the base envelope, no `error`, full document |

A sentinel line — a name, an email and a claim reference, assembled at runtime and deliberately not
credential-shaped — is seeded into `error_log` and `result` and asserted absent from every nested key
and value of the envelope: through `get_output()` directly, through `agent.invoke()`, and through the
real ASGI `/invoke`. Restoring the original `get_output()` (which forwarded `error_log` through a
marker filter) fails these tests.

**Implementation:** `tests/unit/test_error_envelope_closed_set.py`,
`tests/integration/test_invoke_e2e.py::TestErrorEnvelopeIsClosedSet`,
`tests/proof_of_boundary/test_output_containment.py::TestRefusalMessagesCarryNoContent`

---

### PB-02: APPI PII Gate

**Objective:** Verify that claimant PII (name, address, NHI number) injected in the intake input does NOT appear in `parsed_claims_data`, `claims_summary`, `decision_basis`, or the final `result` output.

**Test scenario:** Pass the following PII-laden intake to the full pipeline:
```
事故報告書
氏名: 山田 太郎
住所: 東京都渋谷区XX X-X-X
被保険者番号: 12345678-90

事故発生日: 2026-06-10
事故内容: 自動車事故による損害を報告します。
保険金額の請求をお願いします。
```

**Pass criteria:**
1. `state["parsed_claims_data"]` does NOT contain "山田 太郎", "渋谷区", or "12345678-90"
2. `state["claims_summary"]` does NOT contain the claimant name, address, or NHI number
3. `state["decision_basis"]` does NOT contain PII values
4. Final `result` (formatted output) does NOT contain PII values
5. PII fields are replaced with `[REDACTED-PII]` (or stripped entirely)

**Fail condition:** Any PII value appears verbatim in any downstream State field or the final output.

**Implementation:** `tests/proof_of_boundary/test_pii_gate.py`

---

### PB-03: Malformed Intake Schema — Explicit Rejection

**Objective:** Verify that structurally invalid claims documents are rejected in `pre_process` with an appropriate error response (no silent failure, no partial state write).

**Test scenarios:**

**Scenario A: Binary / null-byte data**
- Input: string containing null bytes (`"\x00\x00\x00binary data\x00\x00"`)
- Expected: `status=ERROR`, `error_log` contains message about binary data

**Scenario B: Empty string**
- Input: `""`
- Expected: `status=ERROR`, no `validated_input` written

**Scenario C: JSON array (not a dict)**
- Input: `'["not", "a", "dict"]'`
- Expected: `status=ERROR`, `error_log` contains message about JSON object requirement

**Scenario D: No claims keywords (plain unrelated text)**
- Input: `"The weather is nice today. Sunshine expected."`
- Expected: `status=ERROR`, no `claims_summary` generated

**Common pass criteria for A–D:**
1. `status` = ERROR
2. `error_log` is non-empty with a descriptive message
3. No `claims_summary` in State (pipeline did not proceed to generate)
4. No `decision_basis` or `disclosure_statement` produced
5. No partial claims processing occurred

**Implementation:** `tests/proof_of_boundary/test_malformed_intake.py`

---

## Test Infrastructure Notes

### Framework availability
- The framework wheel is installed by the pipeline; the suite imports `framework.*` directly.
- No test requires a live model backend — the generation step is deterministic by design, which is
  what makes every assertion in this document reproducible.

### Test levels
- **Node level:** instantiate the node and call `execute(state, config=None)` with a plain dict.
  Used where the assertion is about one node's contract.
- **Graph level:** `invoke()` the compiled agent through the `invoke` fixture in `tests/conftest.py`.
  Required for anything involving state that crosses the outer→inner boundary — the caller context
  bridge and containment cannot be proven at node level.
- **Entry-point level:** drive the ASGI application with `TestClient`. Required for authentication,
  the size cap, the dropping of undeclared keys and the adapter credential screen, none of which the
  graph sees.

### Fixtures
`tests/conftest.py` holds the canonical valid claim, a free-form Japanese intake, a compiled `agent`
and an `invoke` helper that calls it as a trusted caller.

### Gate requirements
- `gate-stub-check`: every test file must carry at least one real assertion
- `gate-dep-pinning`: every dependency exact-pinned
- `gate-audit-trace-check`: every boundary node emits a domain audit event
- the test job must pass

## Coverage Summary

| Test ID | Category | Target | Implementation |
|---------|----------|--------|----------------|
| TC-01 | Happy path | Auto claim, structured intake | test_294_preservation.py, test_invoke_e2e.py |
| TC-02 | Edge case | Free-form Japanese intake | test_pii_gate.py, test_output_containment.py |
| TC-03 | Output format | Markdown mode | test_294_preservation.py |
| TC-04 | Output format | JSON mode | test_294_preservation.py |
| TC-05 | Edge case | Partial / minimal intake | test_malformed_intake.py |
| PB-01 | Boundary | Statutory delivery, verbatim | test_294_preservation.py |
| PB-01b | Boundary | Output containment on refusal | test_output_containment.py |
| PB-01c | Boundary | Closed-set error envelope | test_error_envelope_closed_set.py, test_invoke_e2e.py, test_output_containment.py |
| PB-02 | Boundary | Personal-data removal | test_pii_gate.py |
| PB-03 | Boundary | Malformed intake rejection | test_malformed_intake.py |
| PB-04 | Boundary | Caller contract: finite, bounded, inert, fail-closed | test_caller_contract.py |
| PB-05 | Boundary | Numbers reproduced byte-identically | test_output_invariant.py |
| PB-06 | Boundary | Node invoke order and trust denial | test_pb_invoke_order.py |
| PB-07 | Boundary | Interrupt propagation (not enabled; skips) | test_pb7_hitl_interrupt_propagation.py |
| E2E | Entry point | Auth, size cap, key dropping, credential screen | test_invoke_e2e.py |

---

## Framework Compliance Tests

| TC-ID | Test | Expected Result |
|-------|------|----------------|
| TC-01 | State contract: flat TypedDict | Type check pass, no Pydantic/dataclass |
| TC-02 | Invalid input is refused | Status error, no document produced |
| TC-03 | No JWT/Credential in State | `__init_subclass__` detection pass |
| TC-04 | InvocationContext via configurable only | Direct access raises error |
| TC-05 | Domain audit events emitted | Every boundary node emits one, refusals included |
| TC-06 | Default input gate non-bypassable | Overriding it raises at class definition |
| TC-07 | Default output gate non-bypassable | Overriding it raises at class definition |
| TC-08 | `required_trust_level` enforced | Insufficient trust → refused |
