"""AgentCore Platform v1.0"""

# INS-C2-014 — ClaimsIntakeParseNode
# Domain node 1: parse the raw claims intake payload (事故報告書 / 交通事故証明書 /
# 診断書), validate it, remove regulated personal data, and write
# parsed_claims_data to State.
#
# Returns only changed state keys (partial dict).
#
# Personal-data handling:
#   Claimant name, address, health-insurance number and contact details MUST NOT
#   be written into State. They are replaced with an anonymised marker before the
#   parsed_claims_data dict is emitted.
#
# Caller options:
#   This node also validates the caller's input_context — the channel through
#   which a caller supplies the policy-coverage excerpt, the policy reference and
#   the rendering options. Every field is bounded explicitly; a value that fails
#   validation is refused by name and never echoed back.

import json
import logging
import re
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from framework.utils.audit_logger import emit_trace_event

from src.services.caller_contract import (
    CONTEXT_KEYS,
    CallerContractError,
    finite_in_range,
    inert_identifier,
    one_of,
    screen_injection,
)
from src.services.service import ClaimsDocumentService

logger = logging.getLogger(__name__)

# ── Personal-data detection and removal ───────────────────────────────────────

# Name patterns (heuristics; a production deployment would use named-entity
# recognition over these).
_PII_NAME_PATTERNS: List[re.Pattern[str]] = [
    re.compile(r"氏名[：:]\s*[一-鿿぀-ゟ゠-ヿ a-zA-Z]{1,20}"),
    re.compile(r"claimant[_\s]?name[:\s=]+\S+", re.IGNORECASE),
    re.compile(r"申請者[：:]\s*[一-鿿぀-ゟ゠-ヿ a-zA-Z]{1,20}"),
    re.compile(r"name[:\s=]+[A-Za-z\s]{2,40}(?=[\s,;]|$)", re.IGNORECASE),
]

# Address patterns
_PII_ADDRESS_PATTERNS: List[re.Pattern[str]] = [
    re.compile(r"住所[：:]\s*[^\n\r,]{5,60}"),
    re.compile(r"address[:\s=]+[^\n\r]{5,80}", re.IGNORECASE),
    re.compile(r"\b[0-9]{3}-[0-9]{4}\b"),  # postal code
]

# Health-insurance number (被保険者番号) patterns
_PII_NHI_PATTERNS: List[re.Pattern[str]] = [
    re.compile(r"被保険者番号[：:]\s*[0-9A-Za-z\-]{4,20}"),
    re.compile(r"nhi[_\s]?(?:number|no)[:\s=]+\S+", re.IGNORECASE),
    re.compile(r"health[_\s]?insurance[_\s]?(?:number|no|id)[:\s=]+\S+", re.IGNORECASE),
    re.compile(r"保険証番号[：:]\s*[0-9A-Za-z\-]{4,20}"),
]

_PII_REDACTED = "[REDACTED-PII]"

# Keys dropped entirely from structured payloads.
_PII_DICT_KEYS = frozenset(
    {
        "claimant_name",
        "name",
        "姓名",
        "氏名",
        "address",
        "住所",
        "所在地",
        "nhi_number",
        "health_insurance_number",
        "被保険者番号",
        "保険証番号",
        "claimant_dob",
        "date_of_birth",
        "生年月日",
        "phone",
        "telephone",
        "電話番号",
        "携帯番号",
        "email",
        "メール",
        "メールアドレス",
    }
)

# ── Caller-supplied option bounds ─────────────────────────────────────────────

# Rendered-document length bound. The lower bound keeps a caller from requesting
# a document too short to hold the statutory sections; the upper bound caps
# response size.
_MIN_SUMMARY_CHARS = 500
_MAX_SUMMARY_CHARS = 50_000

_OUTPUT_FORMATS = frozenset({"markdown", "json"})

# The consumed key set is CONTEXT_KEYS (src/services/caller_contract.py). Keys
# outside it are ignored here rather than rejected, so a platform-injected key
# cannot fail an otherwise valid request. Ignoring is not stripping, though: an
# unrecognised key stays in state and is still scanned by the framework's output
# gate on the FIRST node, which is why the HTTP adapter drops unknown keys before
# invoke() rather than relying on this node — see src/api/server.py.


def _strip_pii_string(text: str) -> str:
    """Replace personal-data matches in a string with the anonymised marker."""
    for pattern in _PII_NAME_PATTERNS + _PII_ADDRESS_PATTERNS + _PII_NHI_PATTERNS:
        text = pattern.sub(_PII_REDACTED, text)
    return text


def _strip_pii_dict(record: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively remove personal data from a mapping."""
    sanitized: Dict[str, Any] = {}
    for key, value in record.items():
        if isinstance(key, str) and key.lower() in _PII_DICT_KEYS:
            sanitized[key] = _PII_REDACTED
        elif isinstance(value, str):
            sanitized[key] = _strip_pii_string(value)
        elif isinstance(value, dict):
            sanitized[key] = _strip_pii_dict(value)
        elif isinstance(value, list):
            sanitized[key] = [
                _strip_pii_dict(item)
                if isinstance(item, dict)
                else (_strip_pii_string(item) if isinstance(item, str) else item)
                for item in value
            ]
        else:
            sanitized[key] = value
    return sanitized


# ── Claim type normalization ──────────────────────────────────────────────────

_CLAIM_TYPE_ALIASES = {
    "自動車": "auto",
    "交通": "auto",
    "医療": "medical",
    "生命": "medical",
    "health": "medical",
    "財物": "property",
    "損害": "property",
    "賠償": "liability",
    "第三者": "liability",
    "旅行": "travel",
}


def _normalize_claim_type(raw: str) -> str:
    """Normalize a claim type to its canonical English key."""
    lower = raw.strip().lower()
    return _CLAIM_TYPE_ALIASES.get(lower, lower)


# Required for valid structured intake.
_REQUIRED_TEXT_FIELDS = frozenset({"claim_type", "incident_date"})

# A policy reference renders verbatim into the delivered document, so it is held
# to an inert alphabet wherever it comes from.
_POLICY_REFERENCE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class ClaimsIntakeParseNode(FunctionNode):
    """Parse and validate the claims intake; remove personal data before writing State.

    Accepts:
      validated_input: str — validated claims payload; a JSON object (structured
        intake) or free-form text (事故報告書 / 交通事故証明書 / 診断書).
      input_context: dict — caller options (see CONTEXT_KEYS).

    Returns partial dict with:
      parsed_claims_data: dict — structured, personal-data-free claims data:
        claim_type: str             — normalized claim type
        incident_date: str          — incident date (ISO or descriptive)
        policy_number: str          — anonymised policy reference (or "unknown")
        damage_description: str     — damage description, personal data removed
        coverage_rules_excerpt: str — coverage rules, when supplied
      output_format: str            — resolved document format
      max_summary_chars: int        — resolved document length bound

    On validation failure:
      {"error_log": [...], "status": error} — no claims data written.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _reject(self, reason: str, detail: str) -> dict[str, Any]:
        """Emit the refusal audit event and return the error delta."""
        emit_trace_event(
            "claims_parse_rejected",
            {"node": self.__class__.__name__, "reason": reason},
            {},
        )
        logger.warning("ClaimsIntakeParseNode: rejected (%s)", reason)
        return {
            "error_log": [f"ClaimsIntakeParseNode: {detail}"],
            "status": AgentStatus.ERROR.value,
            # The runner surfaces `formatted_output or result` as `output`. A reason left only in
            # error_log reaches no one: the terminal result carries just `status`, and get_output()
            # does not copy error_log out of the graph -- the caller sees a blank spinner.
            "formatted_output": "Request could not be completed. " + (f"ClaimsIntakeParseNode: {detail}"),
        }

    def execute(self, state: AgentState, config: Optional[Dict[str, Any]] = None) -> dict[str, Any]:
        validated_input = state.get("validated_input") or state.get("user_input", "")

        if not validated_input or not isinstance(validated_input, str) or not validated_input.strip():
            return self._reject("empty_input", "validated_input is empty or missing.")

        service = ClaimsDocumentService((config or {}).get("configurable", {}).get("agent_config"))

        # ── Validate caller options ───────────────────────────────────────────
        try:
            options = self._validate_context(state.get("input_context") or {}, service)
        except CallerContractError as exc:
            return self._reject("caller_context", str(exc))

        # ── Parse the intake ──────────────────────────────────────────────────
        payload: Optional[Dict[str, Any]] = None
        try:
            candidate = json.loads(validated_input.strip())
            if isinstance(candidate, dict):
                payload = candidate
        except (json.JSONDecodeError, ValueError):
            payload = None

        if payload is not None:
            parsed_data, errors = self._parse_structured(payload)
        else:
            parsed_data, errors = self._parse_freeform(validated_input)

        if errors:
            return self._reject("schema_validation", " ".join(errors))

        # A caller-supplied coverage excerpt takes precedence over one embedded in
        # the intake document, and is capped at the configured length.
        if options.get("coverage_rules_excerpt"):
            parsed_data["coverage_rules_excerpt"] = options["coverage_rules_excerpt"]
        if options.get("policy_reference"):
            parsed_data["policy_number"] = options["policy_reference"]
        if options.get("claim_type_hint") and not parsed_data.get("claim_type"):
            parsed_data["claim_type"] = options["claim_type_hint"]

        excerpt_cap = service.max_coverage_excerpt_chars()
        excerpt = parsed_data.get("coverage_rules_excerpt") or ""
        if len(excerpt) > excerpt_cap:
            parsed_data["coverage_rules_excerpt"] = excerpt[:excerpt_cap]

        # ── Remove personal data before the State write ───────────────────────
        clean_data = _strip_pii_dict(parsed_data)

        emit_trace_event(
            "claims_intake_parsed",
            {
                "node": self.__class__.__name__,
                "claim_type": clean_data.get("claim_type", "unknown"),
                "has_coverage_excerpt": bool(clean_data.get("coverage_rules_excerpt")),
            },
            state,
        )
        logger.info(
            "ClaimsIntakeParseNode: parsed claim_type=%s, incident_date=%s",
            clean_data.get("claim_type", "unknown"),
            clean_data.get("incident_date", "unknown"),
        )

        delta: Dict[str, Any] = {"parsed_claims_data": clean_data}
        if "output_format" in options:
            delta["output_format"] = options["output_format"]
        if "max_summary_chars" in options:
            delta["max_summary_chars"] = options["max_summary_chars"]
        return delta

    # ── Caller options ────────────────────────────────────────────────────────

    def _validate_context(self, context: Any, service: ClaimsDocumentService) -> Dict[str, Any]:
        """Validate the caller's input_context against explicit bounds.

        Raises CallerContractError naming the offending field. The rejected value
        is never included in the message.
        """
        if not isinstance(context, dict):
            raise CallerContractError("input_context: must be an object")

        options: Dict[str, Any] = {}

        # Screen every supplied value — including mapping keys — before any of it
        # is interpreted, so a hostile field name cannot reach a log line.
        screen_injection({k: v for k, v in context.items() if k in CONTEXT_KEYS}, field="input_context")

        if "output_format" in context:
            options["output_format"] = one_of(
                context["output_format"], _OUTPUT_FORMATS, field="input_context.output_format"
            )

        if "policy_reference" in context:
            options["policy_reference"] = inert_identifier(
                context["policy_reference"], field="input_context.policy_reference"
            )

        if "claim_type_hint" in context:
            options["claim_type_hint"] = one_of(
                context["claim_type_hint"],
                service.recognized_claim_types(),
                field="input_context.claim_type_hint",
            )

        if "coverage_rules_excerpt" in context:
            excerpt = context["coverage_rules_excerpt"]
            if not isinstance(excerpt, str):
                raise CallerContractError("input_context.coverage_rules_excerpt: must be a string")
            cap = service.max_coverage_excerpt_chars()
            if len(excerpt) > cap:
                raise CallerContractError(f"input_context.coverage_rules_excerpt: exceeds the {cap}-character limit")
            options["coverage_rules_excerpt"] = excerpt.strip()

        if "max_summary_chars" in context:
            options["max_summary_chars"] = int(
                finite_in_range(
                    context["max_summary_chars"],
                    field="input_context.max_summary_chars",
                    minimum=_MIN_SUMMARY_CHARS,
                    maximum=_MAX_SUMMARY_CHARS,
                )
            )

        return options

    # ── Intake parsing ────────────────────────────────────────────────────────

    def _parse_structured(self, payload: Dict[str, Any]) -> tuple[Dict[str, Any], List[str]]:
        """Parse a structured JSON intake payload."""
        errors: List[str] = []

        missing = _REQUIRED_TEXT_FIELDS - payload.keys()
        if missing:
            errors.append(
                f"schema validation failed: missing required fields {sorted(missing)}. "
                "Structured intake must include at minimum claim_type and incident_date."
            )
            return {}, errors

        claim_type_raw = payload.get("claim_type", "")
        if not isinstance(claim_type_raw, str) or not claim_type_raw.strip():
            errors.append("schema validation failed: claim_type must be a non-empty string.")
            return {}, errors

        incident_date_raw = payload.get("incident_date", "")
        if not isinstance(incident_date_raw, str) or not incident_date_raw.strip():
            errors.append("schema validation failed: incident_date must be a non-empty string.")
            return {}, errors

        # A policy number from a structured payload renders verbatim into the
        # delivered document, so it is held to the same inert alphabet as a
        # caller-supplied reference rather than being cast with str().
        policy_raw = payload.get("policy_number", "unknown")
        policy_number = str(policy_raw).strip() or "unknown"
        if policy_number != "unknown" and not _POLICY_REFERENCE.match(policy_number):
            errors.append(
                "schema validation failed: policy_number must be 1-32 characters " "from the set A-Z a-z 0-9 _ -."
            )
            return {}, errors

        parsed: Dict[str, Any] = {
            "claim_type": _normalize_claim_type(claim_type_raw),
            "incident_date": incident_date_raw.strip(),
            "policy_number": policy_number,
            "damage_description": str(payload.get("damage_description", "")).strip(),
            "coverage_rules_excerpt": str(payload.get("coverage_rules_excerpt", "")).strip(),
        }
        return parsed, []

    def _parse_freeform(self, text: str) -> tuple[Dict[str, Any], List[str]]:
        """Parse free-form intake text (事故報告書 / 診断書 / etc.).

        Extracts structured fields from unstructured Japanese/English claims
        documents by pattern matching.
        """
        errors: List[str] = []

        claim_type = self._extract_claim_type(text)
        if not claim_type:
            errors.append(
                "schema validation failed: cannot determine claim_type from free-form "
                "intake. Expected auto / medical / property / liability / travel or the "
                "Japanese equivalents (自動車 / 医療 / 財物 / 賠償 / 旅行)."
            )
            return {}, errors

        incident_date = self._extract_incident_date(text)
        if not incident_date:
            errors.append(
                "schema validation failed: cannot extract incident_date from free-form "
                "intake. Expected YYYY-MM-DD, YYYY年MM月DD日, or similar."
            )
            return {}, errors

        parsed: Dict[str, Any] = {
            "claim_type": claim_type,
            "incident_date": incident_date,
            "policy_number": self._extract_policy_number(text) or "unknown",
            "damage_description": self._extract_damage_description(text),
            "coverage_rules_excerpt": "",
        }
        return parsed, []

    def _extract_claim_type(self, text: str) -> str:
        """Heuristically extract the claim type from free-form text."""
        lower = text.lower()
        for keyword in ("auto", "automobile", "traffic", "vehicle", "car"):
            if keyword in lower:
                return "auto"
        for keyword in ("medical", "diagnosis", "hospital", "health", "injury", "illness"):
            if keyword in lower:
                return "medical"
        for keyword in ("property", "damage", "fire", "flood", "theft", "burglary"):
            if keyword in lower:
                return "property"
        for keyword in ("liability", "third party", "bodily injury"):
            if keyword in lower:
                return "liability"
        for keyword in ("travel", "trip", "overseas", "baggage"):
            if keyword in lower:
                return "travel"
        if "自動車" in text or "交通事故" in text or "車両" in text:
            return "auto"
        if "診断書" in text or "医療" in text or "傷病" in text or "入院" in text:
            return "medical"
        if "火災" in text or "盗難" in text or "財物" in text:
            return "property"
        if "賠償" in text or "第三者" in text:
            return "liability"
        if "旅行" in text:
            return "travel"
        return ""

    def _extract_incident_date(self, text: str) -> str:
        """Extract the incident date from free-form text."""
        m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
        if m:
            return m.group(1)
        m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
        if m:
            return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
        m = re.search(r"(\d{4})年(\d{1,2})月", text)
        if m:
            return f"{m.group(1)}-{m.group(2).zfill(2)}"
        m = re.search(r"\b(\d{4})/(\d{1,2})/(\d{1,2})\b", text)
        if m:
            return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
        return ""

    def _extract_policy_number(self, text: str) -> str:
        """Extract the policy number from free-form text."""
        for pattern in (
            re.compile(r"(?:POL|policy)[_\s\-]*(?:no|number|#)?[:\s=]+([A-Za-z0-9\-]{4,20})", re.IGNORECASE),
            re.compile(r"証券番号[：:]\s*([A-Za-z0-9\-]{4,20})"),
            re.compile(r"契約番号[：:]\s*([A-Za-z0-9\-]{4,20})"),
        ):
            m = pattern.search(text)
            if m:
                return m.group(1).strip()
        return ""

    def _extract_damage_description(self, text: str) -> str:
        """Extract the damage description from free-form text.

        The full text is used as the description after removing structured header
        prefixes. Personal data is removed after this method returns.
        """
        cleaned = re.sub(
            r"(?:事故報告書|交通事故証明書|診断書|保険金請求書)[：:\s]*",
            "",
            text,
        ).strip()
        max_len = 2000
        if len(cleaned) > max_len:
            return cleaned[:max_len] + "... [truncated]"
        return cleaned
