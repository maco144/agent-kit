# Spec 13 — Typed Results

Status: **approved** · Written 2026-09-14 · Roadmap item: 2.2 (`specs/06-harness-roadmap.md`)

## Goal

An agent returns a validated Python object instead of prose — while still using tools, hooks, budgets,
audit, and Cloud reporting on the way there.

**Done means:** `await agent.run(prompt, output_type=Invoice)` returns `AgentResult[Invoice]` whose
`.parsed` is a validated `Invoice`; the Anthropic and OpenAI providers constrain the final answer with
their native structured-output APIs; providers without native support get the schema in the system
prompt; an invalid answer is sent back to the model with the validation errors up to
`output_retries` times and then raises `OutputValidationError`; every failed attempt is audited.

## Decisions

1. **Result shape:** `AgentResult` becomes `Generic[T]` with `parsed: T | None = None`. `.output` keeps
   the raw final text (the JSON). Existing callers are unaffected.
2. **Native first, prompt fallback.** Providers advertise `supports_structured_output`; the loop uses
   native constraints when the provider supports them and the schema is compatible, otherwise it puts
   the schema in the system prompt. Both paths validate with Pydantic.
3. **Repair on invalid output.** Validation errors go back to the model as a user message, up to
   `AgentConfig.output_retries` (default 2). Repair turns count toward `max_turns` and budgets.
4. **Any type Pydantic validates** — `BaseModel`, dataclasses, `TypedDict`, `list[...]`, enums,
   `Literal`, unions, primitives — via `TypeAdapter`. Non-object roots are wrapped for the provider.
5. **Tools keep working.** Every provider call in a typed run carries the output constraint; it
   shapes only text answers, so tool-calling turns proceed normally and the turn without tool calls is
   the one validated.
6. **No forced tool call.** Output is not delivered through a synthetic "final_result" tool:
   `tool_choice` `any`/`tool` returns 400 on Claude Fable 5.1, and `auto` lets the model answer in prose.

## API

### `agent_kit/output.py` (new)

```python
T = TypeVar("T")

@dataclass(frozen=True)
class OutputSpec(Generic[T]):
    output_type: Any               # the type passed as output_type
    name: str                      # provider-facing schema name, [A-Za-z0-9_-]{1,64}
    json_schema: dict[str, Any]    # provider-ready schema (object root, strict-transformed)
    wrapped: bool                  # True when the root was wrapped as {"result": ...}
    native_compatible: bool        # False when the schema can't be expressed natively

    @classmethod
    def from_type(cls, output_type: type[T] | Any) -> OutputSpec[T]
    def parse(self, text: str) -> T          # raises OutputParseError
    def instructions(self) -> str            # prompt-mode system prompt addition


class OutputParseError(ValueError):
    errors: str        # human/model-readable description of what was wrong
```

### `agent_kit/types.py`

```python
T = TypeVar("T")

class AgentResult(BaseModel, Generic[T]):
    output: str
    parsed: T | None = None
    ...                             # existing fields unchanged
```

### `agent_kit/exceptions.py`

```python
class OutputValidationError(AgentKitError):
    errors: str          # validation errors from the last attempt
    raw_output: str      # the last final text
    attempts: int        # validation attempts made (1 + repairs)
```

### `AgentConfig` / `Agent`

```python
AgentConfig.output_retries: int = 2      # repair turns after the first invalid answer

@overload
async def run(self, prompt: str, *, output_type: type[T], **context: Any) -> AgentResult[T]
@overload
async def run(self, prompt: str, *, output_type: None = None, **context: Any) -> AgentResult[Any]

async def stream(self, prompt: str, *, output_type: Any = None, **context: Any) -> AsyncIterator[str]
```

`output_type` becomes a reserved keyword: it is never forwarded into hook `context`.

### Providers

`BaseProvider.complete()` and `stream()` gain `output_schema: OutputSpec[Any] | None = None`. Providers
declare `supports_structured_output: bool`; the loop reads it with `getattr(provider,
"supports_structured_output", False)` and passes `output_schema` only when using native mode, so
third-party providers without the attribute keep working through the prompt fallback.

| Provider | `supports_structured_output` | Request addition |
|---|---|---|
| Anthropic | `True` | `output_config={"format": {"type": "json_schema", "schema": spec.json_schema}}` |
| OpenAI | `True` | `response_format={"type": "json_schema", "json_schema": {"name": spec.name, "schema": spec.json_schema, "strict": True}}` |
| Ollama | `True` (inherits OpenAI); `structured_output_with_tools = False` | same as OpenAI (Ollama's OpenAI-compatible endpoint honours `response_format`) |

**Amended during implementation (live Ollama runs):** Ollama applies `response_format` as a grammar over
the whole reply, so with tools in the request the model can never call them — 4/4 live runs skipped the
tool and invented the data. Providers with that limitation declare `structured_output_with_tools = False`
(read with `getattr(..., True)`). For such a provider, a typed run with tools starts in prompt mode; once
the model gives a tool-free answer that fails validation, it has stopped calling tools, so the repair
turns switch to native mode. Live: llama3.2 calls the tool, answers in prose, and the natively constrained
repair returns valid JSON — 3 turns.

OpenAI: when the message has `refusal` and no `content`, the assistant text is the refusal text, so it
reaches validation (and the repair message) instead of an empty string.

Dependency floors rise to what these parameters need: `anthropic>=1.0`, `openai>=1.40`.

## Behaviour

### Building the spec — `OutputSpec.from_type`

1. `adapter = TypeAdapter(output_type)`; `schema = adapter.json_schema()`.
2. **Root:** if the schema is not a plain object (`"type": "object"` with `properties`, no
   `additionalProperties` schema), wrap it:
   `{"type": "object", "properties": {"result": <schema minus $defs>}, "required": ["result"], "$defs": <$defs>}`
   and set `wrapped=True`. Unions, lists, enums, primitives, and dicts at the root are wrapped.
3. **Strict transform**, applied recursively (including inside `$defs`, `items`, `anyOf`, `allOf`):
   - every object schema with `properties` gets `additionalProperties: false` and
     `required` = all property names (optional Pydantic fields become required-but-nullable in
     effect; the model emits `null` or a value and Pydantic accepts either);
   - `default` is removed;
   - the constraint keywords `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`,
     `multipleOf`, `minLength`, `maxLength`, `pattern`, `minItems` (other than 0/1), `maxItems`,
     `uniqueItems` are removed and appended to that node's `description` as
     `"(constraints: minLength=3, pattern=^A)"` — Pydantic still enforces them on parse;
   - `format` is kept only for `date-time`, `time`, `date`, `duration`, `email`, `hostname`, `uri`,
     `ipv4`, `ipv6`, `uuid`; any other format moves to the description the same way;
   - `oneOf` becomes `anyOf` and `discriminator` is removed (Pydantic's discriminated unions);
   - for native-compatible schemas, a `$ref` with sibling keys (Pydantic emits `{"$ref": …,
     "description": …}` for described model fields) is replaced by the referenced definition merged
     with those keys — strict mode rejects `$ref` siblings;
   - `title` is removed.
4. **`native_compatible = False`** when the schema contains, anywhere: an object whose
   `additionalProperties` is `true` or a schema (open dicts — `dict[str, X]` not at the root), or a
   recursive `$ref` (a `$defs` entry reachable from itself). These are the schemas neither API accepts in
   strict form; such runs use prompt mode on every provider.
5. `name` = `output_type.__name__` when available, else `"output"`, sanitised to `[A-Za-z0-9_-]` and
   truncated to 64 characters.

### Parsing — `OutputSpec.parse(text)`

1. Strip whitespace; if the text is a single fenced block (```` ```json … ``` ```` or ```` ``` … ``` ````),
   take its body.
2. `json.loads` — on failure raise `OutputParseError("response is not valid JSON: <msg>")`.
3. If `wrapped`: the value must be an object with a `result` key → take `result`; otherwise raise
   `OutputParseError('expected a JSON object with a "result" key')`.
4. `adapter.validate_python(value)` — on `ValidationError` raise `OutputParseError` whose `errors` lists
   each error as `"<loc>: <msg>"`, one per line, at most 20 lines.

### Prompt mode — `OutputSpec.instructions()`

Appended to the system prompt (after a blank line; used alone when there is no system prompt):

```
Respond with only a JSON value that matches this JSON Schema — no prose, no code fences.
<json.dumps(json_schema, indent=2)>
```

The schema shown is the same transformed schema (so a wrapped root asks for `{"result": ...}`).
Prompt mode does not pass `output_schema` to the provider.

### Loop

- `output_type` given → `spec = OutputSpec.from_type(output_type)` at run start (a type Pydantic cannot
  build a schema for raises `pydantic` errors before any provider call); `native = spec.native_compatible
  and getattr(provider, "supports_structured_output", False) and (getattr(provider,
  "structured_output_with_tools", True) or the run has no tools)`.
- Each provider call (complete and stream) passes `output_schema=spec` when native; in prompt mode the
  system prompt carries `spec.instructions()`.
- A turn with tool calls proceeds as today.
- A turn without tool calls (the final answer) is recorded exactly as today (memory, turns, cost,
  Cloud `turn_complete`), then parsed:
  - **valid** → the run completes; `AgentResult.parsed` = the value; `output` = the raw text.
  - **invalid**, with invalid answers so far ≤ `output_retries` (so the default allows 3 answers in
    total) → audit `output_validation_failed`, add a user
    message and continue the loop:
    ```
    Your response did not match the required output schema:
    <errors>
    Respond again with only the corrected JSON.
    ```
    If native mode was held back only because of tools, the repair turns use native mode.
  - **invalid**, retries exhausted → audit `output_validation_failed`, then raise
    `OutputValidationError(errors, raw_output, attempts)` through the normal error path (`run_error` to
    Cloud).
- Budgets, `before_llm` hooks, `max_turns` apply to repair turns as to any turn; hitting `max_turns`
  during repair raises `MaxTurnsExceededError` as usual.
- No `output_type` → behaviour is byte-for-byte unchanged (no `output_schema` kwarg is passed to
  providers, `parsed` is `None`).

### Streaming

`stream(prompt, output_type=...)` yields the final answer's raw JSON text as it arrives, including the
text of any invalid attempts followed by the repair attempt; `agent.last_result.parsed` holds the
validated value once the stream is exhausted. Consumers that need only valid data should use `run()`.

### Audit

| Event | Actor | Payload |
|---|---|---|
| `output_validation_failed` | `"agent"` | `turn`, `attempt`, `native` (bool), `errors` (first 500 chars) |
| `agent_complete` (existing) | run id | adds `output_type` (spec name, or `null`) |

The raw output is not placed in audit payloads.

## Testing

- **`tests/test_output.py`** — `OutputSpec`: BaseModel (flat and nested via `$defs`), optional and
  defaulted fields become required, constraints moved to description and still enforced on parse,
  dataclass, `TypedDict`, `list[int]` / enum / `Literal` / union / `int` roots wrapped and unwrapped,
  `dict[str, int]` field → `native_compatible=False`, recursive model → `False`, name sanitising;
  `parse` with fences, invalid JSON, missing `result`, validation errors formatted and capped.
- **`tests/test_provider_requests.py`** — captured Anthropic `output_config` and OpenAI `response_format`
  payloads for `complete` and `stream`; no extra keys when `output_schema` is `None`; OpenAI refusal
  becomes assistant text.
- **`tests/test_typed_results.py`** — through `Agent` with scripted providers: valid first answer;
  invalid then repaired (repair message content, audit event, `parsed`); retries exhausted raises
  `OutputValidationError` with attempts; tool call turn then typed answer; native provider receives
  `output_schema` on every call; provider without the attribute gets prompt mode (system prompt contains
  the schema, no `output_schema` kwarg); `native_compatible=False` forces prompt mode on a native
  provider; `stream()` parity with `last_result.parsed`; `output_type` absent from hook context; untyped
  runs pass no `output_schema`; `agent_complete.output_type`.
- **Live** (outside CI, done): a typed run with a tool against local Ollama (`llama3.2`, native
  `response_format`), plus prompt mode on the same model; against the Anthropic and OpenAI APIs when
  `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` are set.

## Out of scope

Streaming partial objects; per-call `output_retries`; typed outputs in the Claude Agent SDK / OpenAI
Agents SDK adapters and in `Pipeline` stages; strict tool schemas (`strict: true` on tools).
