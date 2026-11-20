"""OpenMatrix Agent orchestration layer (P5).

Server-side planner + state machine + tool-call loop that drives the existing
MCP tools through the MCP protocol boundary (``FastMCP.call_tool``), instead
of importing tool functions directly.

Design notes:

- ``LangGraph`` remains an **optional** future swap. This package ships a
  minimal self-built orchestrator (``RuleBasedPlanner`` -> ``ResearchOrchestrator``
  step loop) behind narrow Protocol boundaries (``ToolGateway``, ``ResearchPlanner``)
  so the orchestration engine can be replaced without touching the tool layer.
- ``research_run(research_id, objective, context)`` runs a research run as an
  interruptible, observable task (pause / resume / cancel / status / result).

Patent guardrail (red line): the planner and the orchestrator only ever route
``document -> retrieval -> evidence -> conclusion`` tools. Matrix-run export
tools (``export_matrix_report`` / ``export_report``) are excluded from the agent
allowlist and the orchestrator refuses to plan or execute them.
"""

from agent.state_machine import (
    ResearchRun,
    RunEvent,
    RunPhase,
    StepRecord,
    StepStatus,
    StateMachineError,
)
from agent.planner import (
    Plan,
    PlanStep,
    PlanningError,
    ResearchPlanner,
    RuleBasedPlanner,
)
from agent.tools import (
    AGENT_TOOL_ALLOWLIST,
    MATRIX_CELL_TOOLS,
    McpToolGateway,
    ToolGateway,
    ToolResult,
    ToolSpec,
    default_gateway,
    is_allowed_tool,
)
from agent.orchestrator import (
    InterruptToken,
    ResearchOrchestrator,
    RetryPolicy,
)
from agent.research_run import (
    ResearchRunController,
    ResearchRunManager,
    research_run,
)

__all__ = [
    "AGENT_TOOL_ALLOWLIST",
    "MATRIX_CELL_TOOLS",
    "InterruptToken",
    "McpToolGateway",
    "Plan",
    "PlanStep",
    "PlanningError",
    "ResearchOrchestrator",
    "ResearchPlanner",
    "ResearchRun",
    "ResearchRunController",
    "ResearchRunManager",
    "RetryPolicy",
    "RuleBasedPlanner",
    "RunEvent",
    "RunPhase",
    "StateMachineError",
    "StepRecord",
    "StepStatus",
    "ToolGateway",
    "ToolResult",
    "ToolSpec",
    "default_gateway",
    "is_allowed_tool",
    "research_run",
]
