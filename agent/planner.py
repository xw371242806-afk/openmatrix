"""Rule-based research planner (minimal self-built orchestration).

The planner converts a research objective + context into an ordered list of
tool steps. It is deterministic, dependency-free, and only ever plans steps that
use ``AGENT_TOOL_ALLOWLIST`` tools, enforcing the patent red line at plan time.

A LangGraph ``StateGraph`` could replace this planner later by implementing the
same ``ResearchPlanner`` protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from agent.tools import AGENT_TOOL_ALLOWLIST, is_allowed_tool


class PlanningError(Exception):
    """Raised when a plan cannot be built for the given objective/context."""


@dataclass(frozen=True)
class PlanStep:
    """One tool invocation in a research plan."""

    step_id: str
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    # When required=True, a failed step fails the whole run; otherwise the
    # step is skipped (graceful degradation) and the run continues.
    required: bool = True
    max_attempts: int = 3
    # Step-output binding: maps an argument name to how it should be filled
    # from a prior step's output. Two forms:
    #   {"step": step_id, "field": "filings.0.accession"}  -> direct JSON-path field
    #   {"step": step_id, "template": "https://.../{filings.0.accession}/..."} -> template
    # with placeholders resolved from the prior step's parsed JSON output.
    input_bindings: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class Plan:
    """Ordered research plan for a single research_id."""

    research_id: str
    objective: str
    steps: list[PlanStep] = field(default_factory=list)


class ResearchPlanner(Protocol):
    def plan(self, research_id: str, objective: str, context: dict[str, Any]) -> Plan:
        ...


class RuleBasedPlanner:
    """Deterministic planner using small context-driven templates.

    Context keys (all optional) steer which template runs:

    - ``pages`` (+ ``question``): document analysis
      ``retrieve_context -> summarize_context -> verify_quote``
    - ``url`` (+ ``question``): URL analysis ``fetch_context``
    - ``documents`` (+ ``question``): multi-document ``analyze_documents``
    - ``ticker``: SEC ``sec_search -> sec_get_xbrl_facts``
    - ``code``: A-share ``ak_stock_quote -> ak_financials``
    - ``series_id``: FRED ``macro_fred_lookup``
    - ``kpi_names`` (+ ``pages``): KPI extraction ``extract_kpis``
    - ``financials`` (+ ``operation``): deterministic ``compute_financials``
    """

    def plan(self, research_id: str, objective: str, context: dict[str, Any]) -> Plan:
        ctx = context or {}
        steps: list[PlanStep] = []

        pages = ctx.get("pages")
        documents = ctx.get("documents")
        url = ctx.get("url")
        ticker = ctx.get("ticker")
        code = ctx.get("code")
        series_id = ctx.get("series_id")
        kpi_names = ctx.get("kpi_names")
        financials = ctx.get("financials")
        question = ctx.get("question") or objective

        if pages:
            steps.extend(self._document_steps(research_id, question, pages, ctx))
        if kpi_names and pages:
            steps.append(self._kpi_step(research_id, kpi_names, pages, ctx))
        if financials:
            steps.append(self._financials_step(research_id, financials, ctx))
        if documents:
            steps.extend(self._multi_document_steps(research_id, question, documents, ctx))
        if url:
            steps.extend(self._url_steps(research_id, question, url, ctx))
        if ticker:
            steps.extend(self._sec_steps(research_id, ticker, ctx))
        if code:
            steps.extend(self._akshare_steps(research_id, code, ctx))
        if series_id:
            steps.extend(self._fred_steps(research_id, series_id, ctx))

        if not steps:
            raise PlanningError(
                "no supported context keys found (expected pages/documents/url/ticker/code/series_id/kpi_names/financials)"
            )

        return Plan(research_id=research_id, objective=objective, steps=steps)

    @staticmethod
    def _kpi_step(
        research_id: str, kpi_names: list[str], pages: list[str], ctx: dict[str, Any]
    ) -> PlanStep:
        return PlanStep(
            step_id=f"{research_id}:extract_kpis",
            tool="extract_kpis",
            arguments={
                "pages": pages,
                "kpi_names": kpi_names,
                "mode": ctx.get("mode", "bm25"),
            },
            description=(
                "Extract the requested KPI values via the retrieval pipeline "
                "(deterministic; no grid/cell structure is built)."
            ),
            required=False,
        )

    @staticmethod
    def _financials_step(
        research_id: str, financials: list[dict], ctx: dict[str, Any]
    ) -> PlanStep:
        return PlanStep(
            step_id=f"{research_id}:compute_financials",
            tool="compute_financials",
            arguments={
                "values": financials,
                "operation": ctx.get("operation", "sum"),
            },
            description="Run a deterministic financial calculation (no LLM).",
            required=False,
        )

    @staticmethod
    def _document_steps(
        research_id: str, question: str, pages: list[str], ctx: dict[str, Any]
    ) -> list[PlanStep]:
        mode = ctx.get("mode", "bm25")
        quote = ctx.get("quote")
        steps = [
            PlanStep(
                step_id=f"{research_id}:retrieve",
                tool="retrieve_context",
                arguments={"question": question, "pages": pages, "mode": mode},
                description="Select the most relevant document chunks for the question.",
            ),
            PlanStep(
                step_id=f"{research_id}:summarize",
                tool="summarize_context",
                arguments={"question": question, "pages": pages, "mode": mode},
                description="Produce a grounded extractive summary of the document.",
                required=False,
            ),
        ]
        if quote:
            steps.append(
                PlanStep(
                    step_id=f"{research_id}:verify",
                    tool="verify_quote",
                    arguments={"quote": quote, "pages": pages, "mode": "exact"},
                    description="Verify the extracted quote against the document pages.",
                    required=False,
                )
            )
        return steps

    @staticmethod
    def _multi_document_steps(
        research_id: str, question: str, documents: list[dict], ctx: dict[str, Any]
    ) -> list[PlanStep]:
        return [
            PlanStep(
                step_id=f"{research_id}:analyze",
                tool="analyze_documents",
                arguments={
                    "documents": documents,
                    "question": question,
                    "mode": ctx.get("mode", "bm25"),
                },
                description=(
                    "Analyze each document independently via retrieval "
                    "(list form; no grid-cell construction)."
                ),
            )
        ]

    @staticmethod
    def _url_steps(
        research_id: str, question: str, url: str, ctx: dict[str, Any]
    ) -> list[PlanStep]:
        return [
            PlanStep(
                step_id=f"{research_id}:fetch",
                tool="fetch_context",
                arguments={"url": url, "question": question, "mode": ctx.get("mode", "bm25")},
                description="Fetch the URL and select the most relevant chunks.",
            )
        ]

    @staticmethod
    def _sec_steps(research_id: str, ticker: str, ctx: dict[str, Any]) -> list[PlanStep]:
        form_type = ctx.get("form_type")
        fact_names = ctx.get("fact_names")
        steps = [
            PlanStep(
                step_id=f"{research_id}:sec_search",
                tool="sec_search",
                arguments={"ticker": ticker, "form_type": form_type},
                description="Search recent SEC filings for the ticker.",
            )
        ]
        # Step-output binding: the latest filing URL produced by sec_search is
        # fed into sec_get_filing so the chain advances without the model.
        steps.append(
            PlanStep(
                step_id=f"{research_id}:sec_filing",
                tool="sec_get_filing",
                arguments={},
                description="Fetch the most recent filing text via the search result URL.",
                required=False,
                input_bindings={
                    "filing_url": {
                        "step": f"{research_id}:sec_search",
                        "field": "filings.0.filing_url",
                    }
                },
            )
        )
        if fact_names:
            steps.append(
                PlanStep(
                    step_id=f"{research_id}:sec_xbrl",
                    tool="sec_get_xbrl_facts",
                    arguments={"ticker": ticker, "fact_names": fact_names},
                    description="Return structured XBRL financial facts for the ticker.",
                    required=False,
                )
            )
        return steps

    @staticmethod
    def _akshare_steps(research_id: str, code: str, ctx: dict[str, Any]) -> list[PlanStep]:
        return [
            PlanStep(
                step_id=f"{research_id}:ak_quote",
                tool="ak_stock_quote",
                arguments={"code": code},
                description="Fetch the realtime A-share quote.",
            ),
            PlanStep(
                step_id=f"{research_id}:ak_financials",
                tool="ak_financials",
                arguments={"code": code},
                description="Fetch key financial-statement figures.",
                required=False,
            ),
        ]

    @staticmethod
    def _fred_steps(research_id: str, series_id: str, ctx: dict[str, Any]) -> list[PlanStep]:
        arguments: dict[str, Any] = {"series_id": series_id}
        if ctx.get("start"):
            arguments["start"] = ctx["start"]
        if ctx.get("end"):
            arguments["end"] = ctx["end"]
        return [
            PlanStep(
                step_id=f"{research_id}:fred",
                tool="macro_fred_lookup",
                arguments=arguments,
                description="Return the FRED macro time series observations.",
            )
        ]


def validate_plan_tools(plan: Plan) -> list[str]:
    """Return any step ids whose tool is outside the agent allowlist (red line)."""
    violations = []
    for step in plan.steps:
        if not is_allowed_tool(step.tool):
            violations.append(f"{step.step_id} -> {step.tool}")
    return violations


def plan_tools_available(plan: Plan) -> set[str]:
    """Set of tool names referenced by the plan (subset of the allowlist)."""
    return {step.tool for step in plan.steps} & set(AGENT_TOOL_ALLOWLIST)
