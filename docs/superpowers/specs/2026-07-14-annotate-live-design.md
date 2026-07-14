# annotate-live — collaborative annotation template

Date: 2026-07-14. Status: approved. Feature A of two (Feature B = "Send to Claude
cloud" worker, separate spec later; this template only reserves the hook).

## Goal

A sibling of the `annotate` template where multiple people comment on the same
opened file simultaneously, synced through Liveblocks. URL `comments` param stays
the local mirror (refresh-proof, budget-evicted, sidecar-logged exactly like
annotate); Liveblocks LiveMap is the shared truth between peers.

## Files (`fused_render/templates/annotate-live/`)

- `template.html` — copy of annotate's, plus the live layer (below).
- `annotate_live.py` — copy of annotate.py (record/tombstone sidecar contract
  unchanged; sidecar stays per-machine).
- `icon.svg` — annotate glyph + presence dot.
- Registry: append `"annotate-live"` immediately after every `"annotate"`
  occurrence (extra non-default mode; never changes existing defaults).

## Live layer (inside template.html, marked block)

- Connection: Liveblocks CDN ESM (same import/enterRoom wiring as the
  collab-canvas liveblocksTransport — reference file provided), public key const
  (same pk_prod key), room from `room` URL param (generated + pinned if absent),
  `transport=off` param disables sync entirely (pure-local fallback + tests).
- Shared state: `LiveMap("comments")`, key = comment id, value = full comment
  JSON (+ `author`, `updatedAt` epoch-ms, `deleted: true` tombstones — tombstone
  entries persist, never removed, so late joiners can't resurrect).
- Outbound: annotate's existing `save(arr, deletedIds)` funnel gains one step —
  upsert changed/new ids into the LiveMap, write tombstone entries for
  deletedIds. Change detection vs a `lastSynced` snapshot map.
- Inbound: LiveMap subscribe → merge into local array by id (tombstone wins;
  else higher `updatedAt` wins) → `fused.params.set("comments", …)` + rerender,
  inside an echo guard so save() doesn't re-publish.
- Late join: on storage load, full merge (both directions — local URL comments
  a peer took offline get pushed up).
- Identity: `author` field on every new comment. Name from
  `localStorage["fused.annotateLive.author"]`; first comment prompts inline in
  the composer (small name input, remembered). Author chip rendered on cards.
- Presence: Liveblocks presence `{name}`; header shows live avatars/name chips
  (count + names, no cursors — comments anchor to content, not pixels).
- Feature-B hook only: comment card gets an "→ Claude" button that sets
  `assigned: "claude"` + `updatedAt` on the comment (syncs like any edit) and
  renders an "assigned to Claude" badge. No worker in this PR.

## Constraints

- Anchor/resolver machinery, eviction budget, legacy import, claude-mode
  handoff: untouched copies.
- Author is display-only trust (prototype posture, D3 single-user server).
- Sidecar log records remote-authored comments too (whoever's machine, their
  sidecar gets the merged log — acceptable, documented).

## SPEC/DECISIONS

New SPEC section (next free §), one D entry (next free D — check main at PR
time): LiveMap-per-comment over single-array LWW; tombstone persistence;
URL-param-as-mirror.

## Tests

Follow test_canvas_template.py conventions: registry placement (annotate-live
after annotate, never first/default), template ships files, no runtime script
tag, merge function golden tests if extracted to testable shape, recorder
parity with annotate.py (same record/tombstone behavior on the copy).

## Verify

Two separate browser contexts, same room: comment appears live both sides;
resolve/delete propagate; tombstone survives reload + late join; author chips +
presence names; `transport=off` works alone; budget eviction still fires.
