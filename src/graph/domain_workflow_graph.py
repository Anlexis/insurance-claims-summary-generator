"""AgentCore Platform v1.0"""

# INS-C2-014 — DomainWorkflowGraph (the inner graph)
#
# Encapsulates the full insurance claims summary domain workflow:
#
#   START
#     → claims_intake_parse     (ClaimsIntakeParseNode)
#     → claims_summary_generate (ClaimsSummaryGenerateNode)
#     → summary_format          (SummaryFormatNode)
#     → END
#
# Called by ClaimsSummaryGraphNode.get_subgraph(). get_output() shapes the
# sub_result dict consumed by merge_output() there.
#
# Statutory field preservation:
#   get_output() explicitly emits decision_basis and disclosure_statement. Both
#   must reach the outer graph so the output gate can verify them before the
#   document is released.
#
# Rules enforced:
#   ✅ Inherits BaseGraph (fully custom topology — no forced backbone)
#   ✅ register_nodes() does NOT call super() (abstract in BaseGraph)
#   ✅ Does NOT register initialize / finalize (outer backbone concerns)
#   ✅ get_output() emits decision_basis + disclosure_statement explicitly
#   ❌ No platform-internal imports

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus

from src.graph.context_bridge import get_caller_input_context
from src.nodes.claims_intake_parse import ClaimsIntakeParseNode
from src.nodes.claims_summary_generate import ClaimsSummaryGenerateNode
from src.nodes.summary_format import SummaryFormatNode
from src.schemas.state import State


class DomainWorkflowGraph(BaseGraph):
    """Inner domain workflow graph for INS-C2-014.

    Inherits BaseGraph directly for a fully custom node topology.

    Pipeline (linear):
        START
          → claims_intake_parse     (ClaimsIntakeParseNode)
          → claims_summary_generate (ClaimsSummaryGenerateNode)
          → summary_format          (SummaryFormatNode)
          → END

    All nodes are FunctionNode subclasses returning partial-dict state updates.
    initialize / finalize are outer backbone concerns — not registered here.
    """

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        """Unique identifier for this inner graph."""
        return "ins_c2_014_claims_summary_workflow"

    @property
    def state_schema(self) -> type:
        """TypedDict subclass shared across inner and outer graph."""
        return State

    # ── Config validation ─────────────────────────────────────────────────────

    def _validate_config(self) -> None:
        """Validate the inner graph configuration before compilation.

        Every runtime key this graph consumes is optional and has a documented
        default, so an absent key is not a failure. A key that is present but of
        the wrong shape IS a failure: silently ignoring it would restore exactly
        the condition this migration removed, where declared configuration had no
        effect on any run.
        """
        for block in ("claims", "output", "llm"):
            value = self.config.get(block)
            if value is not None and not isinstance(value, dict):
                from framework.errors import ConfigError

                raise ConfigError(f"config['{block}'] must be a mapping when present")

    # ── Caller context hand-off ──────────────────────────────────────────────

    def _extra_initial_state(self) -> dict[str, Any]:
        """Seed the inner state with the caller's input_context.

        The framework's GraphNode.execute() does not forward input_context into
        subgraph.invoke(), so the inner nodes would otherwise never see the
        caller's data. ClaimsSummaryGraphNode.extract_input() stashes it
        immediately before the invoke and this hook reads it back — see
        src/graph/context_bridge.py.
        """
        return {"input_context": get_caller_input_context()}

    # ── Node registration ─────────────────────────────────────────────────────

    def register_nodes(self) -> None:
        """Register all 3 domain nodes.

        No super() call — BaseGraph.register_nodes() is abstract.
        Do NOT register initialize or finalize; those are outer backbone
        concerns. Every key registered here is referenced in add_edges().
        """
        self._nodes["claims_intake_parse"] = ClaimsIntakeParseNode()
        self._nodes["claims_summary_generate"] = ClaimsSummaryGenerateNode()
        self._nodes["summary_format"] = SummaryFormatNode()

    # ── Edge wiring ───────────────────────────────────────────────────────────

    def add_edges(self) -> None:
        """Wire the linear claims summary topology.

        ClaimsIntakeParseNode → ClaimsSummaryGenerateNode → SummaryFormatNode.
        Linear pipeline — add_conditional_edges() is not used.
        """
        self._sg.add_edge(START, "claims_intake_parse")
        self._sg.add_edge("claims_intake_parse", "claims_summary_generate")
        self._sg.add_edge("claims_summary_generate", "summary_format")
        self._sg.add_edge("summary_format", END)

    # ── Routing ───────────────────────────────────────────────────────────────

    def route(self, state: State) -> str:
        """Conditional routing — required by the BaseGraph contract.

        This topology is linear and does not call add_conditional_edges(), so
        this method is not reached at runtime; it is implemented to satisfy the
        contract and returns END on error so an unexpected call cannot re-enter a
        processing node.

        The parameter is annotated with this graph's own State rather than the
        base AgentState on purpose. A path callable's annotation is read as its
        input schema and fields outside that annotation are projected away, so an
        under-specified annotation makes the routing flag it tests always absent —
        the branch then never runs while unit tests that call the method directly
        keep passing. The annotation is correct here even though nothing routes on
        it today, so that wiring a conditional edge later cannot silently inherit
        that defect.
        """
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        return "summary_format"

    # ── Output shape ──────────────────────────────────────────────────────────

    def get_output(self, state: AgentState) -> dict[str, Any]:
        """Shape the output dict returned to the outer graph as sub_result.

        Received by ClaimsSummaryGraphNode.merge_output(). Both are designed
        together to guarantee field-name consistency:

            Inner get_output()   emits: "result", "claims_summary",
                                        "decision_basis", "disclosure_statement",
                                        "status", "trace_id"
            Outer merge_output() reads: sub_result.get("result"),
                                        sub_result.get("decision_basis"),
                                        sub_result.get("disclosure_statement"),
                                        sub_result.get("status")

        decision_basis and disclosure_statement are emitted explicitly: they are
        the fields the output gate checks, and dropping either here would make
        the gate refuse a document that is actually compliant.
        """
        return {
            "result": state.get("result"),
            "claims_summary": state.get("claims_summary"),
            "decision_basis": state.get("decision_basis"),
            "disclosure_statement": state.get("disclosure_statement"),
            "status": state.get("status"),
            "trace_id": state.get("trace_id"),
            "node_history": state.get("node_history", []),
            "error_log": state.get("error_log", []),
        }
