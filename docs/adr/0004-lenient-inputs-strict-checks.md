# 0004. Lenient model-facing inputs, strict checks before a backend call

**Status:** accepted (aliases since 0.1.0; argument checks 0.4.0; nested paths 0.7.0)

## Context

The caller is a model, often a small local one. Models send `args` as a JSON string instead of an
object, name the server `query` or `name`, ask for 50 results when 25 is the cap, and send `"2"` for an
integer. A strict schema is right for a machine caller and loses correct answers for a model caller.
On the other side, a call that cannot match the backend tool's schema should not cost a backend round
trip and an error the model has to decode.

## Decision

- **The router's own tools are lenient.** `Router._coerce` parses a string `args`, aliases near-miss
  argument names, and resolves `call_tool` without a `server` when the tool name is unique.
  `search_capabilities.limit` is clamped to 1–25 and `retrieve_spill.max_lines` to 1–2000 by the router.
  The schemas therefore declare `default` and `examples` but no `minimum`, `maximum` or `pattern`, and
  `call_tool.args` has no `type`: a validating client would otherwise turn those inputs into errors.
- **Backend arguments are checked before they are sent.** `validate_args` walks the backend tool's
  schema (nested objects and arrays included), applies only safe coercions (`"2"` → `2` for an integer,
  never a bare word), and refuses a call that cannot match, listing every problem with its path and the
  arguments to retry with. If the checker itself fails, the call is sent unchanged (fail open).
- An identical call that keeps returning the identical result is refused after four repeats
  (`TG_LOOP_GUARD`).

## Alternatives

- **Strict schemas everywhere.** Simpler and standard, but measured sessions lose answers to argument shape.
- **No pre-check; let the backend reject.** One less moving part, but costs a round trip and an error
  message written for humans.

## Consequences

- The published schemas are slightly looser than what the router does with the values; the descriptions
  state the clamping so a model is not surprised.
- `TG_VALIDATE_ARGS=0` and `TG_LOOP_GUARD=0` turn the checks off for anyone who prefers raw pass-through.
