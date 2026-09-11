# `ann/` — the annotation subsystem (PR3)

Source of truth: `.claude-design/inventory/02-annotations-narrow.md` (`T` =
`fused_render/templates/claude/template.html`). `useAnnotations()` is the door;
everything else is a piece it wires. Voice recording (`rec*`, `transcribe*`)
plugs in through the `AnnRecorder` seam in `types.ts`.

## What the integrator must wire

1. `ui/AnnStrip` — drop `disabled` on the two seats: Comment → `onCommentSeat()`
   (arm / ✓ Done / inert while recording), Annotate → the recorder's begin/end.
   `aria-pressed` from `mode`, `aria-disabled` from `seatsAria(mode)`, and both
   seats plus the camera HIDDEN (not disabled) when `capable` is false (T:8447).
2. `ui/AttachTray` — pass `<AnnChips items={chips} onEdit={editNote}
   onRemove={removeNote} />` as `children`; the tray already renders them first.
3. `pane/AppPane` — `onFrameLoad={onFrameLoad}`; hand `.c-leftview` to
   `<AnnPins stage={…} onBind={bindPins} />` and mount `<AnnBar …
   barRef={bindBar} />` as the row before it (`.c-left` is the flex column).
4. `pane/useNarrowView` — `onArriveChat={arriveNarrowChat}` (T:8940),
   `onRemeasure={remeasure}`; `pane/useSplit` — `onDragTick={remeasure}`.
5. `pane/AppPane.enterNoPane` — `clearAnnotations` (step 1a), `render` (1b),
   `annSetMode: setMode` (2), `rescueComposer` (5).
6. `ClaudeChat` — mount `<AnnPopover handlers={…} popRef={bindPop} />` in the
   chat column (its parked seat); claim Escape with `onEscape` after the shot
   viewer; toggle `.chat-root.annlock` from `locked` and disable ← Chats.
7. The send path — `await overviewForSend()`, put its `notes` through
   `protocol/wire.formatAnnotations`, attach the overview as an `overview`
   `Attachment`, then `markSent(notes)` / `unmarkSent(notes)` on failure, and
   `resolveSent()` when a run that carried notes ends cleanly (T:10608).
8. Host props: `annotateTarget` (hosted), `parentDocument` (XO overlay only),
   `composerHome`, `canSend` (`activeRun || !sending`), `autoSubmit`,
   `viewerOpen`, `onXOArm` (tab share when the native shot is off).

Not ported here (owned elsewhere, by design): `paneview` (`pane/useNarrowView`),
the encode ladder and the badge draw (`shots/`), the annotations stanza grammar
(`protocol/wire.ts`), and the recorder itself.

## Wired (PR3 integration)

All eight are live in `ClaudeChat.tsx`. Three seams the list above did not
anticipate, and where they went:

* `popHandlers` — the card's commit / close / delete all read the DRAFT, which
  is the coordinator's own state, so they come OUT of `useAnnotations` rather
  than being assembled by the integrator.
* `recMark` / `recMarkPoint` — a walkthrough's click is the RECORDER's write
  (`ann/rec.ts` mints the id and the `t` the transcript is matched against), so
  the click handler asks it first and falls back to the store's own writer.
* `shots/attach.attachOverview` now UPLOADS a capture instead of taking one:
  the badge points are `ann/overview`'s to compute, and a second capture would
  photograph a pane that had moved on between the two. `captureAndAttachOverview`
  is the one-call form for a caller with badges and no picture yet.

`setRecStart` is GONE (PR3 review 2): with `recMark` wired the recorder owns
every stamp, and the seam's only readers were the store's own fallback marks —
which stamped an absolute `performance.now()` against a `recStart` nothing ever
wrote. `mark`/`markPoint` now write no `t` at all, which is the honest answer
for a click with no recording behind it.
