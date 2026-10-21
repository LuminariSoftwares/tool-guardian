# 0006. Archive the original before shortening a result

**Status:** accepted (0.3.0)

## Context

Hiding schemas fixes the fixed cost; a single 50 KB shell or JSON result can still flood the window.
Shortening results is lossy, and a model that cannot get the lost part back will guess or re-run the call.

## Decision

- `tg_ladder.compress` is pure and deterministic (no I/O, clock or randomness), so the same result
  always shortens the same way and provider prompt caches keep hitting. Rules are picked by result type:
  diff, JSON array, CSV, shell log, sampled text; errors and small results are left alone.
- Whenever a rule is lossy, the caller archives the full original first (`tg_spill`, under
  `~/.tool-guardian/spill`) and the shortened text carries a `[tool-guardian: ...]` notice with an id.
  `retrieve_spill(id, grep?, start_line?, max_lines?)` reads it back.
- The shape follows dsh-trim (MIT): run the tool first, fail open, archive before anything lossy. The
  implementation is independent; no dsh-trim code is included.

## Alternatives

- **Truncate at N characters.** Simple, but loses the tail of a log where the error usually is.
- **Summarise with a model.** Non-deterministic, costs a model call, and breaks prompt caching.

## Consequences

- Full originals are written to local disk; the archive keeps a bounded number of files.
- The recall rate (`recalled` per tool in the stats) shows whether a rule is cutting what models need.
