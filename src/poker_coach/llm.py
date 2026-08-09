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

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
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
    api_key: str | None = field(default=None, repr=False)
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


# Indeed's gateway, as used by hiring-criteria-mock-ux. OpenAI-shaped rather
# than Anthropic-shaped, which is why it is a separate class and not a base_url.
PROXY_URL = "https://llm-proxy.sandbox.qa.indeed.net"
PROXY_MODEL = "gpt-4.1-mini"
# Rendered by Vault Agent when deployed; absent on a laptop, where the env vars
# take over.
PROXY_VAULT_PROPS = Path(
    "/var/local/product_groups/advanced-sourcing/product_group_auto.properties"
)
PROXY_VAULT_KEY = "dominik-personal-llm-proxy.api-key"
# Same format, in the working directory, for running on a laptop. Gitignored via
# `*.properties`. Preferred over an environment variable because a key in the
# environment is visible to everything the shell starts and shows up in a stray
# `env` -- a file is read by the one thing that needs it.
PROXY_LOCAL_PROPS = Path(".llm.properties")


def _vault_props(path: Path | None = None) -> dict[str, str]:
    """`key=value` lines from the Vault-rendered properties file, if present.

    The path is resolved per call rather than defaulted in the signature: a
    default argument binds once at import, which would make the location
    impossible to repoint.
    """
    path = path or PROXY_VAULT_PROPS
    props: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return props
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        props[key.strip()] = value.strip()
    return props


@dataclass
class ProxyLLM:
    """An OpenAI-shaped gateway, over stdlib HTTP.

    No SDK. This is one POST of JSON to one endpoint, and the `openai` package
    would be a second vendor dependency to carry for it -- the same reasoning
    that keeps the web server on `http.server`.

    Key resolution follows the reference implementation: the Vault-rendered
    properties file first, then `LLM_PROXY_API_KEY`, then
    `VITE_INDEED_LLM_API_KEY`. Never logged, never echoed into a `Reply`, and
    nothing here writes it to disk.
    """

    model: str = PROXY_MODEL
    base_url: str | None = None
    # `repr=False` because a dataclass prints every field it has, and this one
    # ends up in tracebacks and log lines.
    api_key: str | None = field(default=None, repr=False)
    temperature: float = 0.3
    timeout: float = 60.0
    # The gateway's own response cache. Off, matching the reference: this layer
    # already caches on the abstract spot, and a second cache keyed on the exact
    # prompt would mostly serve entries we would never ask for twice anyway.
    cache: bool = False
    name: str = field(init=False, default="indeed-proxy")
    # Why the last call came back None. Recorded rather than raised: callers
    # still get one uniform failure, but "no key", "gateway 500" and "answer did
    # not parse" are different problems and collapsing them left nothing to
    # debug with. Never contains the key; may contain a response body, so it is
    # for a human reading it deliberately, not for logging.
    last_error: str | None = field(init=False, default=None, repr=False)

    def _key(self) -> str | None:
        """The key, from the first place that has one.

        Vault when deployed, then a local properties file, then the
        environment. Never returned to a caller, never logged, never put in a
        `Reply` -- the only thing that reads this is the request builder.
        """
        return (
            self.api_key
            or _vault_props().get(PROXY_VAULT_KEY)
            or _vault_props(PROXY_LOCAL_PROPS).get(PROXY_VAULT_KEY)
            or os.environ.get("LLM_PROXY_API_KEY")
            or os.environ.get("VITE_INDEED_LLM_API_KEY")
        )

    def _url(self) -> str:
        base = self.base_url or os.environ.get("LLM_PROXY_URL") or PROXY_URL
        return base.rstrip("/") + "/openai/v1/chat/completions"

    def complete(
        self, *, system: str, prompt: str, max_tokens: int = DEFAULT_MAX_TOKENS
    ) -> Reply | None:
        self.last_error = None
        key = self._key()
        if not key:
            self.last_error = "no key configured"
            return None
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    # System first and per-hand content second, so a provider
                    # that caches on a prefix can. Same requirement as the
                    # Anthropic path, expressed by ordering rather than by a
                    # cache_control block.
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "temperature": self.temperature,
                "max_tokens": max_tokens,
            }
        ).encode()
        request = urllib.request.Request(
            self._url(),
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                "x-indeed-cache": "true" if self.cache else "false",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode()[:300]
            except Exception:
                pass
            self.last_error = f"HTTP {exc.code}: {detail}"
            return None
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            # One failure mode to the caller, as everywhere else on this
            # boundary: no estimate.
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None
        try:
            text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            self.last_error = f"unexpected response shape: {str(payload)[:300]}"
            return None
        usage = payload.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        return Reply(
            text=text or "",
            model=payload.get("model", self.model),
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            cached=bool(details.get("cached_tokens")),
        )
