"""AgentCore Platform v1.0"""

# INS-C2-014 — Outer graph (two-layer nested architecture)
#
# Architecture:
#
#   Outer backbone (fixed — do NOT override add_edges()):
#     START → initialize → pre_process → main → {route} → post_process → finalize → END
#                                             ↓ (RETRY, max 3)
#                                          pre_process
#
#   The `main` slot is ClaimsSummaryGraphNode (a GraphNode) which delegates the
#   full domain workflow to DomainWorkflowGraph (the inner graph).
#
#   Domain complexity is fully encapsulated inside the inner graph.
#   The outer backbone is never modified.
#
# Output containment:
#   The framework's default get_output() resolves the delivered document as
#   `formatted_output or result`, with no status check — so a run that ends in
#   ERROR still returns whatever the pipeline had already written to `result`.
#   For this agent that would mean releasing a claims document the output gate
#   has just refused to certify. get_output() is overridden below to withhold
#   the document on any non-success outcome and to publish, in its place, a
#   closed-set reason only — `error: {"reason": …}`. error_log is the internal
#   channel and is never projected to the caller.
#
# Rules enforced:
#   ✅ InsC2014Agent inherits AgentBaseGraph
#   ✅ super().register_nodes() called first (fills initialize + finalize)
#   ✅ ClaimsSummaryGraphNode assigned to self._nodes["main"]
#   ✅ merge_output() maps decision_basis + disclosure_statement into outer state
#   ✅ merge_output() returns only changed keys
#   ❌ add_edges() NOT overridden on the outer graph
#   ❌ No platform-internal imports

from typing import Any, ClassVar

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel

from src.graph.context_bridge import set_caller_input_context
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State
from src.services.caller_contract import ERROR_REASONS, REASON_WORKFLOW_FAILED, _contain
from src.services.service import load_runtime_config


class ClaimsSummaryGraphNode(GraphNode):
    """GraphNode assigned to the `main` slot of InsC2014Agent.

    Wraps DomainWorkflowGraph (the inner graph). Called by the backbone after
    pre_process and before post_process.

    Contracts:
      get_subgraph()    — instantiate and return DomainWorkflowGraph
      extract_input()   — pull validated_input (personal data removed) from outer state
      merge_output()    — map sub_result fields into the outer state delta (changed keys
                          only); MUST include decision_basis + disclosure_statement
      error_strategy    — "propagate": re-raise inner errors (fail fast)

    Statutory field preservation:
      merge_output() maps decision_basis and disclosure_statement from sub_result
      into the outer state. These fields must reach PostProcessNode so the output
      gate can verify their presence before the document is released.
    """

    # "handle": inner-graph failures are turned into an outer ERROR delta by
    # on_subgraph_error() below, rather than re-raised.
    #
    # This is an audit-trail choice, not a leniency one. Under "propagate" the
    # framework raises SubgraphError, the node wrapper converts it into a
    # formatted traceback, and merge_output() never runs — so the specific reason
    # the inner pipeline refused the claim (which field was malformed, which
    # option was out of range) is replaced in error_log by an implementation-
    # detail string. The outcome is identical either way — the status is ERROR,
    # no document is released, and the caller receives the same closed-set
    # reason from get_output() — but "handle" keeps the refusal reason intact in
    # error_log, the internal channel the audit trail reads.
    error_strategy: ClassVar[str] = "handle"

    # False: human-in-the-loop interrupts are handled inside the inner graph only.
    propagate_hitl: ClassVar[bool] = False

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def get_subgraph(self) -> Any:
        """Instantiate and return the inner domain workflow graph.

        DomainWorkflowGraph is imported inside the method to avoid a circular
        import at module load time.

        _parent_config() forwards the runtime configuration so the inner nodes
        read the values this repository actually ships rather than falling back
        to their built-in defaults.
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph(config=self._parent_config())

    def extract_input(self, state: AgentState) -> str:
        """Return the string input passed into the inner graph's invoke().

        Also stashes the caller's input_context for the inner graph. The
        framework's GraphNode.execute() invokes the subgraph without forwarding
        input_context, so without this hand-off every inner read of
        state["input_context"] would see an empty mapping. extract_input runs
        immediately before that invoke, which is what makes it the correct place
        to stash it — see src/graph/context_bridge.py.
        """
        set_caller_input_context(state.get("input_context") or {})
        validated: Any = state.get("validated_input") or state.get("user_input") or ""
        return validated if isinstance(validated, str) else ""

    def merge_output(self, state: AgentState, sub_result: dict[str, Any]) -> dict[str, Any]:
        """Map the inner graph's sub_result back into the outer state delta.

        Returns ONLY changed keys — never the full state.

        Key coupling (designed together with DomainWorkflowGraph.get_output()):
          Inner get_output() emits  → "result", "claims_summary", "decision_basis",
                                       "disclosure_statement", "status", "trace_id"
          This merge_output() reads → sub_result.get("result"),
                                       sub_result.get("decision_basis"),
                                       sub_result.get("disclosure_statement"),
                                       sub_result.get("status")

        The two statutory fields are mapped here because PostProcessNode verifies
        their presence in outer state; a mapping that dropped either one would
        make the gate refuse a document that is in fact compliant.

        error_log is mapped for the same reason in the other direction: the inner
        nodes own the refusals (a malformed claim, an out-of-range option), and
        without carrying them out of the subgraph the audit trail would record a
        bare error status with nothing behind it. It is carried verbatim here —
        state holds what happened — and it stops at the boundary: get_output()
        never projects it, publishing a closed-set reason in its place.
        """
        delta: dict[str, Any] = {
            "result": sub_result.get("result"),
            "claims_summary": sub_result.get("claims_summary"),
            "decision_basis": sub_result.get("decision_basis"),
            "disclosure_statement": sub_result.get("disclosure_statement"),
            "status": sub_result.get("status"),
        }
        errors = sub_result.get("error_log")
        if isinstance(errors, list) and errors:
            delta["error_log"] = [str(entry) for entry in errors]
        return delta

    def on_subgraph_error(self, state: AgentState, error: Exception) -> dict[str, Any]:
        """Turn an inner-graph failure into an outer error delta, reason intact.

        Returns ERROR and the refusal reasons the inner nodes recorded. It writes
        no document-bearing key, so nothing the failed run produced is carried
        forward; the graph's get_output() withholds the document on this status
        in any case.
        """
        reasons = [str(entry) for entry in getattr(error, "error_log", []) or []]
        if not reasons:
            reasons = [f"The claims pipeline did not complete: {type(error).__name__}."]
        _error_lines = reasons
        # The runner surfaces `formatted_output or result` as `output`. A reason left only in
        # error_log reaches no one: the terminal result carries just `status`, and get_output()
        # does not copy error_log out of the graph -- the caller sees a blank spinner.
        # The list is bound once: repeating the expression inline would evaluate it twice.
        return {
            "status": AgentStatus.ERROR.value,
            "error_log": _error_lines,
            "formatted_output": "Request could not be completed. " + "; ".join(str(_line) for _line in _error_lines),
        }

    def _parent_config(self) -> dict[str, Any]:
        """Forward the runtime configuration to the inner domain workflow graph.

        Reads config/config.yaml — the file the registry loads and passes as
        Graph(config=...). Returning an empty mapping here (the previous
        behaviour) left every declared value unread: the inner nodes fell back to
        their defaults and the configured excerpt cap, output format and
        disclosure template path had no effect on any run.
        """
        return dict(load_runtime_config())


class InsC2014Agent(AgentBaseGraph):
    """Outer graph for INS-C2-014.

    Inherits AgentBaseGraph directly (L1 Base). Domain logic is fully
    encapsulated in ClaimsSummaryGraphNode (main slot), which delegates to
    DomainWorkflowGraph (the inner graph).

    Backbone (fixed):
        START → initialize → pre_process → main → post_process → finalize → END

    register_nodes() is the ONLY node override:
      - super().register_nodes() fills: initialize, finalize (framework defaults)
      - pre_process:  PreProcessNode (claims intake envelope validation)
      - main:         ClaimsSummaryGraphNode (delegates to DomainWorkflowGraph)
      - post_process: PostProcessNode (output gate)

    add_edges() is NOT overridden — backbone wiring belongs to the framework.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    @property
    def name(self) -> str:
        """Agent identifier registered with the agent registry."""
        return "ins_c2_014"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        """Fill all 5 backbone slots.

        super().register_nodes() MUST be called first — it injects the
        framework's default initialize node (schema_version, session_id,
        trust_level) and finalize node (response_metadata, total_time_ms).
        """
        super().register_nodes()  # fills: initialize, finalize

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = ClaimsSummaryGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    def get_output(self, state: AgentState) -> dict[str, Any]:
        """Resolve the invoke envelope; closed-set labels only on any non-success outcome.

        The framework default is `formatted_output or result` with no status
        check. Two consequences make that unusable here:

          - the backbone routes a non-success `main` result straight to finalize,
            so post_process — the gate that certifies the document — does not run
            at all on that path; and
          - a gate that returns ERROR without clearing state leaves `result`
            in place, so the refused document is returned anyway.

        Either way the caller would receive a full claims document alongside
        `status: error`. This override resolves the document only on success. On
        every other outcome the envelope is

            output: None
            error:  {"reason": "output_withheld"}  — the gate ran and refused
                    {"reason": "workflow_failed"}  — anything else: a refused
                    intake, a rejected caller option, an inner-graph failure,
                    a trust-gate denial, a timeout

        and nothing else. The reason is the gate's own when it wrote one; any
        other value found in the `formatted_output` slot is not trusted and is
        replaced, never echoed. `error_log` is not projected: its entries are
        node-authored text — a refusal notice can carry a caller-supplied field
        name, a framework failure carries an exception's message — and a filter
        over such text (truncation, path stripping, credential redaction) is not
        a closed set. It stays in state for the audit trail.

        Containment lives HERE and only here, deliberately. Clearing `result` in
        the gate as well would be redundant — this override already withholds it —
        and a redundant layer would make each layer individually unfalsifiable:
        removing either one on its own would leave the boundary test green. With a
        single layer the test stays load-bearing, because restoring the framework
        default resolution re-opens the leak on both paths above.
        """
        status = state.get("status")
        if status != AgentStatus.SUCCESS.value:
            formatted = state.get("formatted_output")
            reason = formatted.get("reason") if isinstance(formatted, dict) else None
            if not (isinstance(reason, str) and reason in ERROR_REASONS):
                reason = REASON_WORKFLOW_FAILED
            return {
                "output": None,
                "error": _contain(reason),
                "status": status,
                "trace_id": state.get("trace_id"),
                "correlation_id": state.get("correlation_id"),
                "node_history": state.get("node_history", []),
            }
        return {
            "output": state.get("formatted_output") or state.get("result"),
            "status": status,
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.


__all__ = ["ClaimsSummaryGraphNode", "InsC2014Agent"]
