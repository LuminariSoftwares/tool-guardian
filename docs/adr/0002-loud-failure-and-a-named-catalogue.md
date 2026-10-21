# 0002. Loud failure and a catalogue that names the servers

**Status:** accepted (0.1.0; respawn added in 0.7.0)

## Context

Two failure modes follow from hiding tools behind a router (0001):

1. A model that asks for a server's tools and gets `[]` concludes the capability does not exist and
   works around it silently. Behind a router one broken backend could look like that for all of them.
2. Three or four unnamed generic tools give a model no reason to believe any capability exists, so it
   improvises with the shell instead of opening the catalogue.

## Decision

- A backend that fails to start or stops answering is reported with its status (`UNKNOWN`, `DOWN`,
  `UNSUPPORTED`) and its real error, in `list_capabilities`, `search_capabilities` and `call_tool`, and
  never as an empty tool list (`Router.catalogue`, `Router.search`).
- A backend that exits or times out is respawned once; a second failure within 60 s marks it `DOWN`
  (`backend_respawns` in the stats).
- `list_capabilities`'s description carries a one-line catalogue: every live server's name and a hint
  (its configured `description`, or one derived from its tool names), with an explicit instruction to
  use them (`build_router_tools`).
- Results end with a concrete `NEXT STEP:` line, because a result is read at the moment the model decides
  what to do next and a description is read once, before.

## Alternatives

- **Hide dead servers.** Cleaner listing, but it is exactly the silent empty answer this avoids.
- **Neutral descriptions with no server names.** Fewer tokens; the August 2026 OpenClaude measurement
  in the README shows models bypassing a router that did not make its capabilities obvious.

## Consequences

- The router's description grows with the number of servers (a few tokens each; capped per server under
  `TOOL_GUARDIAN_PRESET=small`).
- The upper-case instruction in `list_capabilities` and `list_skills` reads as shouting to a human and
  costs a little on description-quality scores. It stays because it was written for small local models
  and nothing has measured that removing it is safe.
