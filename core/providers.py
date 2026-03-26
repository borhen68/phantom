"""LLM provider abstraction for PHANTOM."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.settings import (
    provider_max_retries,
    provider_retry_backoff_seconds,
    provider_timeout_seconds,
    secret_settings,
)


@dataclass(frozen=True)
class ProviderUsage:
    input_tokens: int = 0
    output_tokens: int = 0


class MessageProvider:
    name = "provider"

    def create_messages(self, **kwargs):  # pragma: no cover - interface
        raise NotImplementedError


def retry_delay_seconds(exc: Exception, attempt: int, base_backoff: float) -> float:
    message = str(exc)
    delay = max(0.0, float(base_backoff)) * max(1, attempt)
    match = re.search(r"try again in\s+([0-9]+(?:\.[0-9]+)?)s", message, re.IGNORECASE)
    if match:
        return max(delay, float(match.group(1)) + 0.5)
    return delay


def _block_attr(block: Any, name: str, default=None):
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def _content_blocks_of_type(content: Any, block_type: str) -> list[Any]:
    if not isinstance(content, list):
        return []
    return [block for block in content if _block_attr(block, "type") == block_type]


class AnthropicProvider(MessageProvider):
    name = "anthropic"

    def __init__(self):
        try:
            import anthropic
        except ModuleNotFoundError as exc:
            raise ModuleNotFoundError(
                "anthropic is required to run PHANTOM goals. "
                "Install dependencies with `pip install -r requirements.txt`."
            ) from exc

        secrets = secret_settings()
        if not secrets.anthropic_key:
            raise EnvironmentError(
                "ANTHROPIC_API_KEY is not configured. Set it directly, via ANTHROPIC_API_KEY_FILE, "
                "or through PHANTOM_SECRETS_FILE."
            )
        self.timeout_seconds = provider_timeout_seconds()
        self.max_retries = provider_max_retries()
        self.retry_backoff_seconds = provider_retry_backoff_seconds()
        self.client = anthropic.Anthropic(
            api_key=secrets.anthropic_key,
            max_retries=0,
            timeout=self.timeout_seconds,
        )

    def create_messages(self, **kwargs):
        attempts = self.max_retries + 1
        last_error = None

        for attempt in range(1, attempts + 1):
            _raise_if_stop_requested()
            try:
                return self.client.messages.create(**kwargs)
            except Exception as exc:  # pragma: no cover - depends on provider runtime
                last_error = exc
                if attempt >= attempts:
                    break
                time.sleep(retry_delay_seconds(exc, attempt, self.retry_backoff_seconds))

        raise RuntimeError(
            f"Anthropic request failed after {attempts} attempt(s): {last_error}"
        ) from last_error


class _OpenAIResponse:
    """Thin shim that presents an OpenAI Chat Completion as the Anthropic response shape
    that core/loop.py expects (resp.content, resp.stop_reason, resp.usage)."""

    class _UsageShim:
        def __init__(self, completion):
            usage = getattr(completion, "usage", None)
            self.input_tokens = getattr(usage, "prompt_tokens", 0) or 0
            self.output_tokens = getattr(usage, "completion_tokens", 0) or 0

    class _ContentBlock:
        def __init__(self, block_type, **kwargs):
            self.type = block_type
            for k, v in kwargs.items():
                setattr(self, k, v)

    def __init__(self, completion):
        choice = completion.choices[0]
        msg = choice.message
        self.stop_reason = "end_turn" if choice.finish_reason in ("stop", "length") else choice.finish_reason
        self.usage = self._UsageShim(completion)

        blocks = []
        if msg.content:
            blocks.append(self._ContentBlock("text", text=msg.content))
        for tc in (msg.tool_calls or []):
            import json as _json
            try:
                raw_input = _json.loads(tc.function.arguments or "{}")
            except Exception:
                raw_input = {}
            blocks.append(self._ContentBlock(
                "tool_use",
                id=tc.id,
                name=tc.function.name,
                input=raw_input,
            ))
        self.content = blocks


class OpenAIProvider(MessageProvider):
    """OpenAI Chat Completions adapter that implements the same interface as AnthropicProvider."""
    name = "openai"

    def __init__(self):
        try:
            import openai
        except ModuleNotFoundError as exc:
            raise ModuleNotFoundError(
                "openai is required when PHANTOM_PROVIDER=openai. "
                "Install with: pip install openai"
            ) from exc

        secrets = secret_settings()
        api_key = secrets.openai_key or secrets.groq_key
        if not api_key:
            raise EnvironmentError(
                "OPENAI_API_KEY is not configured. Set it directly, use GROQ_API_KEY for Groq, "
                "or provide it through PHANTOM_SECRETS_FILE."
            )
        self.timeout_seconds = provider_timeout_seconds()
        self.max_retries = provider_max_retries()
        self.retry_backoff_seconds = provider_retry_backoff_seconds()
        base_url = (
            os.environ.get("PHANTOM_OPENAI_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or None
        )
        self.client = openai.OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=self.timeout_seconds,
            max_retries=0,
        )

    def create_messages(self, **kwargs) -> _OpenAIResponse:
        """Translate Anthropic-style kwargs to OpenAI Chat Completions and return a shim response."""
        import json as _json

        model = kwargs["model"]
        system = kwargs.get("system", "")
        messages = list(kwargs.get("messages", []))
        max_tokens = kwargs.get("max_tokens", 4096)

        # Prepend system message
        oai_messages = []
        if system:
            oai_messages.append({"role": "system", "content": system})

        # Convert Anthropic-format user/assistant messages
        for m in messages:
            role = m["role"]
            content = m["content"]
            if isinstance(content, list):
                # Tool result content blocks -> OpenAI tool message format
                tool_results = _content_blocks_of_type(content, "tool_result")
                tool_uses = _content_blocks_of_type(content, "tool_use")
                text_blocks = _content_blocks_of_type(content, "text")

                if tool_results:
                    for tr in tool_results:
                        oai_messages.append({
                            "role": "tool",
                            "tool_call_id": _block_attr(tr, "tool_use_id"),
                            "content": _block_attr(tr, "content", ""),
                        })
                    continue

                text = "\n".join(str(_block_attr(b, "text", "")).strip() for b in text_blocks).strip()
                if tool_uses:
                    assistant_message: dict[str, Any] = {
                        "role": "assistant",
                        "tool_calls": [],
                    }
                    assistant_message["content"] = text or ""
                    for tool_use in tool_uses:
                        assistant_message["tool_calls"].append({
                            "id": _block_attr(tool_use, "id"),
                            "type": "function",
                            "function": {
                                "name": _block_attr(tool_use, "name"),
                                "arguments": _json.dumps(_block_attr(tool_use, "input", {})),
                            },
                        })
                    oai_messages.append(assistant_message)
                    continue

                if text:
                    oai_messages.append({"role": role, "content": text})
            else:
                oai_messages.append({"role": role, "content": content})

        # Convert Anthropic tool schemas to OpenAI function tool format
        oai_tools = None
        if kwargs.get("tools"):
            oai_tools = []
            for tool in kwargs["tools"]:
                oai_tools.append({
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                    },
                })

        call_kwargs: dict[str, Any] = dict(
            model=model,
            messages=oai_messages,
            max_tokens=max_tokens,
        )
        if oai_tools:
            call_kwargs["tools"] = oai_tools

        attempts = self.max_retries + 1
        last_error = None
        for attempt in range(1, attempts + 1):
            _raise_if_stop_requested()
            try:
                completion = self.client.chat.completions.create(**call_kwargs)
                return _OpenAIResponse(completion)
            except Exception as exc:  # pragma: no cover
                last_error = exc
                if attempt >= attempts:
                    break
                time.sleep(retry_delay_seconds(exc, attempt, self.retry_backoff_seconds))

        raise RuntimeError(
            f"OpenAI request failed after {attempts} attempt(s): {last_error}"
        ) from last_error


class FallbackProvider(MessageProvider):
    name = "fallback"

    def __init__(self, provider_names: list[str] | tuple[str, ...], factories: dict[str, type[MessageProvider]] | None = None):
        self.provider_names = [name.strip().lower() for name in provider_names if str(name).strip()]
        self.factories = factories or {
            "anthropic": AnthropicProvider,
            "openai": OpenAIProvider,
            "groq": OpenAIProvider,
        }
        self._providers: dict[str, MessageProvider] = {}

    def _provider(self, name: str) -> MessageProvider:
        if name not in self.factories:
            raise ValueError(f"Unsupported PHANTOM provider: {name!r}. Choose: anthropic, openai, groq")
        if name not in self._providers:
            self._providers[name] = self.factories[name]()
        return self._providers[name]

    def create_messages(self, **kwargs):
        last_error = None
        for provider_name in self.provider_names:
            try:
                provider = self._provider(provider_name)
            except (EnvironmentError, ModuleNotFoundError) as exc:
                last_error = exc
                continue
            try:
                return provider.create_messages(**kwargs)
            except RuntimeError as exc:
                last_error = exc
                continue
        if last_error is None:
            raise RuntimeError("No configured providers are available.")
        raise RuntimeError(
            f"All configured providers failed or were unavailable. Last error: {last_error}"
        ) from last_error


def _raise_if_stop_requested() -> None:
    stop_file = os.environ.get("PHANTOM_STOP_FILE")
    if stop_file and Path(stop_file).expanduser().exists():
        raise TimeoutError(f"Kill switch activated: {stop_file}")


def provider_chain_from_env() -> list[str]:
    raw = os.environ.get("PHANTOM_PROVIDER_CHAIN", "").strip()
    if raw:
        chain = [name.strip().lower() for name in raw.split(",") if name.strip()]
        if chain:
            return chain

    primary = os.environ.get("PHANTOM_PROVIDER", "anthropic").strip().lower()
    if primary == "openai":
        return ["openai", "anthropic"]
    if primary == "groq":
        return ["groq", "openai", "anthropic"]
    if primary == "anthropic":
        return ["anthropic", "openai"]
    raise ValueError(f"Unsupported PHANTOM provider: {primary!r}. Choose: anthropic, openai, groq")


def provider_from_env() -> MessageProvider:
    return FallbackProvider(provider_chain_from_env())


def usage_from_response(resp: Any) -> ProviderUsage:
    usage = getattr(resp, "usage", None)
    if usage is None:
        return ProviderUsage()
    return ProviderUsage(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
    )
