# Spec 14 — Context Management

Status: **approved** · Written 2026-09-14 · Roadmap item: 2.4 (`specs/06-harness-roadmap.md`)

## Goal

Long-running, tool-heavy agents stay correct, cheap, and inside the context window: provider-native
content (thinking, compaction) survives the round-trip, prompt caching works by default, reasoning is
configurable, Anthropic's server-side compaction and tool-result clearing are one config line away, and
history trimming is token-based and cache-friendly.

**Done means:** a tool-using run on a thinking model sends every thinking block back unchanged; turn 2+
requests to Anthropic carry cache breakpoints and report cache reads; `AgentConfig(effort="high",
thinking="adaptive")` reaches the request; `AgentConfig(compaction=Compaction())` sends the compaction
edit under its beta header, preserves the returned `compaction` block, prices every usage iteration, and
audits `context_compacted`; a history over `context_budget_tokens` is cut once to half the budget at a
tool-safe boundary, audited as `context_trimmed`, and the request prefix stays byte-identical between
cuts.

## Why now — defects this fixes

1. **Thinking blocks are dropped.** `_turn_from_response` keeps only `text` and `tool_use`. Claude
   Opus 5 and Sonnet 5 think by default; thinking blocks must be passed back unchanged, and dropping them
   breaks the turn.
2. **The message window rewrites history every turn.** Past 50 messages, every append trims the front, so
   the prompt prefix changes each request: prompt caching misses from the first changed message, and on
   Claude Fable 5.1 (accounts created on or after 2026-08-31) edited history with thinking blocks is a 400.
3. **Compaction is impossible** without preserving `compaction` blocks.

## Decisions

1. **Native content round-trips verbatim**, tagged with the provider that produced it; other providers use
   the portable `content` + `tool_calls`.
2. **Prompt caching on by default** (Anthropic); `prompt_caching=False` opts out.
3. **Token budget replaces the message window by default**, trimming in one cut to half the budget.
4. **Reasoning is first-class** (`thinking`, `effort`) **plus a raw passthrough** (`provider_options`).
5. **Server-side context management is opt-in** (`compaction`, `clear_tool_results`); when compaction is
   on, client trimming is off and history stays append-only.

## API

### `agent_kit/types.py`

```python
class Message(BaseModel):
    ...                                            # existing fields
    native_content: list[dict[str, Any]] | None = None   # assistant blocks exactly as the provider returned them
    native_provider: str | None = None                   # provider.name() that produced native_content


class Turn(BaseModel):
    ...                                            # existing fields
    context_events: list[dict[str, Any]] = Field(default_factory=list)  # server-side context management


class Compaction(BaseModel):
    trigger_tokens: int = 150_000                  # API minimum 50_000
    instructions: str | None = None                # replaces the default summarisation prompt


class ClearToolResults(BaseModel):
    trigger_tokens: int = 100_000
    keep: int = 3                                  # most recent tool uses kept
    exclude_tools: list[str] = Field(default_factory=list)
    clear_inputs: bool = False                     # also clear tool_use inputs


class RequestOptions(BaseModel):
    thinking: Literal["adaptive", "disabled"] | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    prompt_caching: bool = True
    compaction: Compaction | None = None
    clear_tool_results: ClearToolResults | None = None
    provider_options: dict[str, Any] = Field(default_factory=dict)

    def is_default(self) -> bool                   # True when equal to RequestOptions()
```

### `AgentConfig`

New parameters (stored as attributes of the same name):

```python
thinking: Literal["adaptive", "disabled"] | None = None
effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
prompt_caching: bool = True
compaction: Compaction | None = None
clear_tool_results: ClearToolResults | None = None
provider_options: dict[str, Any] | None = None
context_budget_tokens: int | None = 150_000        # None disables token trimming
memory_window: int | None = None                   # default changes from 50; an explicit value keeps the message cap
```

`Compaction`, `ClearToolResults` are exported from `agent_kit` alongside `AgentConfig`.

### Providers

- `complete()` / `stream()` accept `options: RequestOptions | None = None`.
- `options=None` (a provider called directly) adds nothing to the request; the loop always passes a
  `RequestOptions`, so agent runs get the defaults (prompt caching on).
- Built-in providers set `supports_request_options = True`. `AgentLoop` passes `options=` only when
  `getattr(provider, "supports_request_options", False)`; otherwise, if the options are not default, it
  logs one warning per loop (`"<provider> does not accept request options; thinking/effort/caching/
  context settings are ignored"`) and passes nothing.

### Memory stores

- `InMemoryStore(window: int | None = None)`, `SQLiteMemory(path, window: int | None = None)` — `None` means
  no count cap (defaults change from 50 / 100).
- Both gain `trim_oldest(count: int) -> int`: remove at least `count` of the oldest non-system messages,
  extended to a boundary that never orphans a tool result and keeps the latest user turn (reusing
  `window_indices(roles, keep=n - count)`); returns the number removed. System messages are never removed.
- `SQLiteMemory` persists `native_content` / `native_provider` in a new `native` TEXT column (JSON
  `{"provider": ..., "content": [...]}` or `NULL`), added with `ALTER TABLE` for existing databases.

### `agent_kit/memory/budget.py` (new)

```python
def prompt_chars(system: str, tools: list[ToolSchema], messages: list[Message]) -> int
def plan_trim(
    messages: list[Message],
    chars: int,
    budget_tokens: int,
    tokens_per_char: float,
) -> tuple[int, int, int]     # (messages_to_drop, estimated_tokens_before, estimated_tokens_after)
```

## Behaviour

### 1. Native content — Anthropic

- `_turn_from_response` sets `message_out.native_content` to every response content block converted to a
  dict (`block.model_dump(mode="json", exclude_none=True)`; plain objects via their attributes, recursively)
  and `native_provider="anthropic"`. `content` and `tool_calls` are extracted as today.
- `_messages_to_anthropic`: an assistant message with `native_provider == "anthropic"` and
  `native_content` renders as `{"role": "assistant", "content": native_content}` — verbatim, including
  empty-text thinking blocks, `redacted_thinking`, and `compaction` blocks; the empty-assistant skip rule
  does not apply to it. Other assistant messages render as today.
- OpenAI/Ollama never read `native_content`.
- Streaming uses the final message from `get_final_message()`, so native content is identical to
  `complete()`.

### 2. Request options — Anthropic

Applied in both `complete()` and `stream()`, in this order, after tools:

1. `provider_options` shallow-merged into the call kwargs (`betas` handled below; `output_config` merged,
   not replaced).
2. `thinking` → `thinking={"type": thinking}`.
3. `effort` → `output_config["effort"] = effort` (merged; `output_schema` then adds `format`).
4. `prompt_caching` and a system prompt → `system=[{"type": "text", "text": system, "cache_control":
   {"type": "ephemeral"}}]`; `prompt_caching` → top-level `cache_control={"type": "ephemeral"}`
   (automatic breakpoint on the growing conversation). Two of the four breakpoint slots.
5. `compaction` → `context_management.edits` gains
   `{"type": "compact_20260112", "trigger": {"type": "input_tokens", "value": trigger_tokens}}` plus
   `"instructions"` when set; beta `compact-2026-01-12`.
6. `clear_tool_results` → (emitted before the compaction edit in `edits`) `context_management.edits` gains
   `{"type": "clear_tool_uses_20250919", "trigger": {"type": "input_tokens", "value": trigger_tokens},
   "keep": {"type": "tool_uses", "value": keep}}` plus `"exclude_tools"` when non-empty and
   `"clear_tool_inputs": true` when `clear_inputs`; beta `context-management-2025-06-27`.
7. When any beta is needed (from 5, 6, or `provider_options["betas"]`) → call
   `client.beta.messages.create(betas=[...sorted unique...], ...)` / `client.beta.messages.stream(...)`;
   otherwise the non-beta methods as today.

Values are not validated against models: an unsupported combination (e.g. `thinking="disabled"` on
Claude Fable 5.1) returns the API's own 400 as a `ProviderError`. Platforms without automatic caching
need `prompt_caching=False`.

### 3. Usage and context events — Anthropic

- When `usage.iterations` is present and non-empty, `CostSummary` token fields are the sums over all
  iterations (`input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`,
  missing fields counting 0), and cost is priced on those sums; otherwise top-level usage as today.
- Each `iterations` entry of type `compaction` adds a `Turn.context_events` entry
  `{"type": "compaction", "input_tokens": n, "output_tokens": n}`.
- Each `response.context_management.applied_edits` entry is added to `Turn.context_events` as its dict
  (e.g. `{"type": "clear_tool_uses_20250919", "cleared_tool_uses": 4, "cleared_input_tokens": 51000}`).

### 4. Request options — OpenAI / Ollama

- `provider_options` shallow-merged into the call kwargs.
- `effort` → `reasoning_effort`: `low`/`medium`/`high` as-is; `xhigh`/`max` → `"high"`.
- `thinking`, `prompt_caching`, `compaction`, `clear_tool_results` are ignored (OpenAI caches
  automatically).

### 5. Loop

- `AgentLoop` receives `request_options: RequestOptions`, `context_budget_tokens: int | None`.
- Every provider call (complete and stream) passes `options=request_options` when the provider supports
  request options (see Providers).
- **Context events:** after each provider call, for each `turn.context_events` entry: `compaction` → audit
  `context_compacted` (`turn`, `input_tokens`, `output_tokens`); any other type → audit `context_edited`
  (`turn`, `edit` = type, plus `cleared_tool_uses`, `cleared_thinking_turns`, `cleared_input_tokens` when
  present).
- **Token budget**, checked before each provider call, after budget enforcement and before `before_llm`
  hooks. Skipped when `context_budget_tokens is None` or `request_options.compaction` is set.
  1. `messages = memory.history(include_system=False)`; `chars = prompt_chars(system, tools, messages)`.
  2. Estimate: when a previous call in this loop reported tokens, `estimated = reported (input +
     cache_read + cache_write) tokens + (chars − chars sent in that call) × 0.25`; otherwise
     `chars × 0.25`. `tokens_per_char = max(estimated, 0) / chars`. *(Amended after live Ollama runs: a
     ratio taken from a short first request is dominated by fixed chat-template tokens — 169 reported
     tokens for ~250 characters — and overestimated a 2,000-character tool result by 4×.)*
  3. `plan_trim(messages, chars, budget, tokens_per_char)`:
     - `before = ceil(chars × tokens_per_char)`; if `before <= budget` → `(0, before, before)`.
     - Otherwise drop the oldest messages one by one (each message's chars = `len(content)` +
       `len(json.dumps(native_content))` when present + `len(json.dumps(arguments))` per tool call),
       subtracting their estimated tokens, until the estimate is `<= budget // 2` or only the last message
       remains → `(dropped, before, after)`.
  4. If `dropped > 0`: `removed = memory.trim_oldest(dropped)`; nothing more happens when `removed == 0`
     (only the current exchange remains). Otherwise (may remove more to stay tool-safe, or
     fewer when the latest user turn anchors the window); re-read history; audit `context_trimmed`
     (`turn`, `removed_messages` = removed, `estimated_tokens_before`, `estimated_tokens_after` =
     recomputed on the new history, `budget_tokens`).
  - Between cuts the history only grows at the end, so the request prefix is byte-identical turn to turn.
- `memory_window` (when set) still trims on append, as today.

### Audit summary

| Event | Actor | Payload |
|---|---|---|
| `context_trimmed` | `"agent"` | `turn`, `removed_messages`, `estimated_tokens_before`, `estimated_tokens_after`, `budget_tokens` |
| `context_compacted` | provider name | `turn`, `input_tokens`, `output_tokens` |
| `context_edited` | provider name | `turn`, `edit`, and present counts among `cleared_tool_uses`, `cleared_thinking_turns`, `cleared_input_tokens` |

## Testing

- **`tests/test_provider_requests.py`** — Anthropic: thinking + tool_use response round-trips the thinking
  block verbatim (empty `thinking` text and `signature` kept) into the next request; compaction block
  preserved; system prompt as cached block + top-level `cache_control` by default and neither with
  `prompt_caching=False`; `thinking` / `effort` payloads; `effort` + `output_schema` share `output_config`;
  compaction / clear-tool-results edits, sorted `betas`, and the beta client used only when needed (non-beta
  and beta fake clients); `provider_options` merging; `usage.iterations` summed and priced; context
  events from iterations and `applied_edits`; stream parity for beta + native content. OpenAI:
  `reasoning_effort` mapping, `provider_options` merge, options that don't apply are absent; an
  Anthropic-native assistant message renders portably for OpenAI.
- **`tests/test_context_budget.py`** — `prompt_chars`, `plan_trim` (under budget, cut to half, ratio from
  reported tokens vs 0.25 default, single remaining message); loop: a long history is cut once and audited,
  subsequent turns under budget send byte-identical prefixes, tool call/result pairs never split, latest
  user turn kept, no trimming when `compaction` is set or budget is `None`; `trim_oldest` on both stores;
  `context_compacted` / `context_edited` audit from scripted turns; a provider without
  `supports_request_options` receives no `options` and a warning is logged once.
- **`tests/test_sqlite_memory.py`** — `native_content` persists; a database created without the `native`
  column is migrated.
- **Existing window tests** — updated for `window=None` defaults; explicit windows unchanged.
- **Live** (outside CI): Ollama run with a small `context_budget_tokens` over a long tool-using session —
  cuts audited, run completes; `effort` passthrough accepted. Anthropic paths when `ANTHROPIC_API_KEY` is
  set: thinking round-trip on `claude-opus-5` with tools, cache reads on turn 2+, compaction request shape.

## Out of scope

Thinking-binding beta controls (`prefix_mismatch_behavior`); client-side summarisation for non-Anthropic
providers; OpenAI Responses API reasoning items; `count_tokens`-based estimates; per-block cache TTLs;
dropping pre-compaction messages client-side.
