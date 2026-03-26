"""
PHANTOM Base Loop — the shared agentic loop used by all specialist agents.
Each agent gets a role, a model, and a set of tools.
"""
from pathlib import Path

from core.contracts import CriticDecision, RunMetrics
from core.errors import BudgetExceeded, CriticEscalation
from core.providers import provider_from_env, usage_from_response
from core.router import max_tokens_for_role
from core.settings import budget_settings, estimate_cost_usd
from core.souls import soul_for, system_with_soul
import memory as mem
from tools import dispatch

_client = None


def client():
    global _client
    if _client is None:
        _client = provider_from_env()
    return _client


def _enforce_budget(metrics: RunMetrics | None):
    if metrics is None:
        return
    budget = budget_settings()
    if metrics.llm_calls >= budget.max_llm_calls:
        raise BudgetExceeded(f"Run exceeded max LLM calls ({budget.max_llm_calls}).")
    if metrics.tool_calls >= budget.max_tool_calls:
        raise BudgetExceeded(f"Run exceeded max tool calls ({budget.max_tool_calls}).")
    if budget.max_llm_calls_per_minute is not None:
        if metrics.recent_llm_calls() >= budget.max_llm_calls_per_minute:
            raise BudgetExceeded(
                f"Run exceeded LLM rate limit ({budget.max_llm_calls_per_minute}/minute)."
            )
    if budget.max_tool_calls_per_minute is not None:
        if metrics.recent_tool_calls() >= budget.max_tool_calls_per_minute:
            raise BudgetExceeded(
                f"Run exceeded tool rate limit ({budget.max_tool_calls_per_minute}/minute)."
            )
    if metrics.input_tokens >= budget.max_input_tokens:
        raise BudgetExceeded(f"Run exceeded max input tokens ({budget.max_input_tokens}).")
    if metrics.output_tokens >= budget.max_output_tokens:
        raise BudgetExceeded(f"Run exceeded max output tokens ({budget.max_output_tokens}).")
    if budget.max_total_cost_usd is not None and metrics.estimated_cost_usd is not None:
        if metrics.estimated_cost_usd >= budget.max_total_cost_usd:
            raise BudgetExceeded(f"Run exceeded max estimated cost (${budget.max_total_cost_usd:.2f}).")
    if budget.stop_file and Path(budget.stop_file).expanduser().exists():
        raise BudgetExceeded(f"Kill switch activated: {budget.stop_file}")


def run_agent(
    role: str,
    model: str,
    system: str,
    messages: list,
    tools: list = None,
    max_steps: int = 20,
    max_output_tokens: int | None = None,
    on_event=None,
    critic_fn=None,
    metrics: RunMetrics | None = None,
) -> str:
    """Generic agent loop. Returns the final text response."""

    def emit(event_type, data):
        if on_event:
            on_event(event_type, {**data, "agent": role})

    soul = soul_for(role)
    active_tools = tools if tools is not None else []
    msgs = list(messages)
    final_text = ""
    wrapped_system = system_with_soul(role, system)

    first_user_message = ""
    for message in msgs:
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            first_user_message = content.strip().splitlines()[0].strip()
        else:
            first_user_message = str(content)
        if first_user_message:
            break
    if role != "critic":
        emit("soul", {
            "name": soul.name,
            "title": soul.title,
            "intro": soul.kickoff(first_user_message),
        })

    resolved_max_tokens = max_output_tokens if max_output_tokens is not None else max_tokens_for_role(role, model)

    for step in range(max_steps):
        emit("step", {"step": step + 1})
        _enforce_budget(metrics)

        kwargs = dict(model=model, max_tokens=resolved_max_tokens, system=wrapped_system, messages=msgs)
        if active_tools:
            kwargs["tools"] = active_tools

        if metrics is not None:
            metrics.note_llm_call()
        resp = client().create_messages(**kwargs)
        usage = usage_from_response(resp)
        if metrics is not None:
            metrics.note_token_usage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                estimated_cost=estimate_cost_usd(model, usage.input_tokens, usage.output_tokens),
            )
        emit("usage", {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "model": model,
        })

        text_parts = []
        tool_uses = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_uses.append(block)

        text = "\n".join(text_parts).strip()
        if text:
            final_text = text
            emit("text", {"text": text})

            if critic_fn and step > 0 and len(text) > 60:
                decision = critic_fn(text)
                if isinstance(decision, str):
                    decision = CriticDecision(action="revise", issue=decision, severity="medium")
                if decision and decision.requires_revision():
                    emit("critic", {
                        "issue": decision.issue,
                        "severity": decision.severity,
                        "action": decision.action,
                    })
                    if decision.blocks_progress() and metrics is not None:
                        if metrics.critic_blocks >= budget_settings().max_critic_blocks:
                            raise CriticEscalation(
                                f"Critic blocked progress {metrics.critic_blocks} times: {decision.issue}"
                            )
                    instruction = (
                        "Do not continue with the blocked approach. Produce a safer alternative or explain why "
                        "the task cannot proceed."
                        if decision.blocks_progress()
                        else "Revise your approach before proceeding."
                    )
                    msgs.append({"role": "assistant", "content": resp.content})
                    msgs.append({
                        "role": "user",
                        "content": (
                            f"[CRITIC::{decision.action.upper()}] {decision.issue} "
                            f"(severity={decision.severity}). {instruction}"
                        ),
                    })
                    continue

        if not tool_uses:
            break

        msgs.append({"role": "assistant", "content": resp.content})
        results = []
        for tool_use in tool_uses:
            _enforce_budget(metrics)
            emit("tool", {"name": tool_use.name, "inputs": tool_use.input})
            result, err = dispatch(tool_use.name, tool_use.input)
            mem.record_tool(tool_use.name, failed=err)
            if metrics is not None:
                metrics.note_tool_call(error=err)
            emit("tool_result", {"name": tool_use.name, "result": result[:400], "error": err})
            results.append({
                "type": "tool_result",
                "tool_use_id": tool_use.id,
                "content": result,
                "is_error": err,
            })
        msgs.append({"role": "user", "content": results})

        if resp.stop_reason == "end_turn":
            break

    return final_text
