"""The model boundary: one method, one return type, no vendor beyond this file.

Deliberately the smallest interface that does the job. Everything the project
asks a model for is a single request with a stable prefix and a short structured
answer -- no tool loop, no filesystem, no session. An interface that admitted
more would invite the rest of the codebase to depend on one vendor's shape.

The contract mirrors `SolutionProvider`: **unavailable returns None, never
raises.** No key, no network, rate limited, a 500 -- all of it comes back as
None, and the caller loses one estimate rather than the run. That is what lets
the pipeline stay useful offline and lets tests run with no credentials.

Swapping vendors means writing one class. `AnthropicLLM` is ~30 lines of it;
an OpenAI or local-model equivalent is the same shape.

Cost control is the caller's job, not this module's: `Budget` is passed in and
enforced here only because the check has to happen at the call site.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol

# Enough for a weighted range plus whatever thinking precedes it. Ranges are
# short; this is sized for the reasoning, not the answer.
DEFAULT_MAX_TOKENS = 4096
DEFAULT_MODEL = "claude-opus-5"


@dataclass(frozen=True, slots=True)
class Reply:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    # True when the provider reported reading the prefix from its cache. Worth
    # surfacing: if this is False across a run, the prefix is not stable and
    # every hand is paying full freight for the same heuristics.
    cached: bool = False


@dataclass
class Budget:
    """A ceiling on a batch, in whole requests and in dollars.

    Deliberately crude. The point is that an unattended run over a session
    cannot cost an unbounded amount, not that the accounting is exact.
    """

    max_requests: int | None = None
    max_usd: float | None = None
    requests: int = 0
    usd: float = 0.0

    def exhausted(self) -> bool:
        return (
            (self.max_requests is not None and self.requests >= self.max_requests)
            or (self.max_usd is not None and self.usd >= self.max_usd)
        )

    def record(self, reply: Reply, rate_in: float, rate_out: float) -> None:
        self.requests += 1
        self.usd += (
            reply.input_tokens * rate_in + reply.output_tokens * rate_out
        ) / 1_000_000


class LLM(Protocol):
    """Anything that can turn a prompt into text."""

    name: str

    def complete(
        self, *, system: str, prompt: str, max_tokens: int = DEFAULT_MAX_TOKENS
    ) -> Reply | None:
        ...


class NullLLM:
    """The default. Answers nothing, costs nothing, never fails.

    Present so that every stage runs end to end with no credentials: a missing
    estimate is a `None` the caller already has to handle, which is a far better
    default than a crash or an accidental charge.
    """

    name = "null"

    def complete(
        self, *, system: str, prompt: str, max_tokens: int = DEFAULT_MAX_TOKENS
    ) -> Reply | None:
        return None


class StubLLM:
    """Canned answers, and a record of what it was asked.

    For tests: the prompt is as much of the design as the code around it, so it
    is worth asserting on directly rather than only on what came back.
    """

    name = "stub"

    def __init__(self, replies: list[str] | None = None):
        self.replies = list(replies or [])
        self.calls: list[dict[str, str]] = []

    def complete(
        self, *, system: str, prompt: str, max_tokens: int = DEFAULT_MAX_TOKENS
    ) -> Reply | None:
        self.calls.append({"system": system, "prompt": prompt})
        if not self.replies:
            return None
        return Reply(text=self.replies.pop(0), model="stub", output_tokens=1)


@dataclass
class AnthropicLLM:
    """Anthropic's API, through the official client.

    `anthropic` is imported inside the call rather than at module scope, so the
    package stays an optional dependency and importing `poker_coach` does not
    require it.

    The system prompt is sent as a cached block. Everything stable -- the task
    description and the heuristics -- belongs there, and the per-hand text
    belongs in the user turn, or the cache never hits.
    """

    model: str = DEFAULT_MODEL
    # Adaptive rather than a token budget: `budget_tokens` is rejected outright
    # on this model family.
    thinking: bool = True
    api_key: str | None = None
    # Point at a gateway that speaks the Anthropic API rather than at Anthropic.
    # Enough for any proxy that is wire-compatible; one that speaks a different
    # shape wants its own class, which is what the protocol is for.
    base_url: str | None = None
    # For gateways that authenticate with something other than `x-api-key`.
    headers: dict[str, str] = field(default_factory=dict)
    name: str = field(init=False, default="anthropic")
    _client: object | None = field(init=False, default=None, repr=False)

    def _connect(self) -> object | None:
        if self._client is not None:
            return self._client
        base = self.base_url or os.environ.get("ANTHROPIC_BASE_URL")
        key = self.api_key or os.environ.get("ANTHROPIC_API_KEY")
        # A gateway may authenticate by header instead of by key, so a key is
        # only required when talking to Anthropic directly. Something must be
        # configured, though -- otherwise this is `NullLLM` with extra steps.
        if not key and not base:
            return None
        try:
            import anthropic
        except ImportError:
            return None
        opts: dict[str, object] = {"api_key": key or "unused"}
        if base:
            opts["base_url"] = base
        if self.headers:
            opts["default_headers"] = dict(self.headers)
        self._client = anthropic.Anthropic(**opts)
        return self._client

    def complete(
        self, *, system: str, prompt: str, max_tokens: int = DEFAULT_MAX_TOKENS
    ) -> Reply | None:
        client = self._connect()
        if client is None:
            return None
        kwargs: dict[str, object] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.thinking:
            kwargs["thinking"] = {"type": "adaptive"}
        try:
            msg = client.messages.create(**kwargs)  # type: ignore[attr-defined]
        except Exception:
            # Every failure is the same failure to the caller: no estimate.
            # Distinguishing them here would only give callers more ways to
            # crash a run over one unavailable answer.
            return None
        text = "".join(
            b.text for b in msg.content if getattr(b, "type", None) == "text"
        )
        usage = msg.usage
        return Reply(
            text=text,
            model=msg.model,
            input_tokens=getattr(usage, "input_tokens", 0),
            output_tokens=getattr(usage, "output_tokens", 0),
            cached=bool(getattr(usage, "cache_read_input_tokens", 0)),
        )
