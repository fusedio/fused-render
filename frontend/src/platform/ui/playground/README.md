# Playground composites

Every visual state of the AI Models Playground, as Tailwind classes on shadcn
primitives. There is no playground stylesheet: `styles/ai-playground.css` is
being emptied one stage at a time, and these files are where its rules went.

Three things could not be a utility and live in `styles/tailwind.css` instead,
next to each other and commented there: the `pg-narrow` custom variant (the
tab's own 760px stacking point, which is not on Tailwind's scale), the six
`@keyframes` plus their `--animate-pg-*` registrations, and the settings fold
(`.stage-work-grid` / `.stage-config-rail` / `.stage-config-inner`) — a
container-query grid whose tracks transition and whose open state is held
through the exit by `:has()`.

## Old class → composite

| `ai-playground.css` | Composite / class | Notes |
|---|---|---|
| `.pg-body` | `<PlaygroundBody>` | `StageShell.tsx` |
| `.pg-side` | `<ModelRail>` | keeps the `pg-side` class as a **tour hook** (`TOUR_MODEL_RAIL`) |
| `.pg-stage` + `--pg-*` | `<StageScroller>` | the `@container` and all four `--pg-*` tokens |
| `.pg-frame` | `<StageFrame>` | |
| `.pg-hero` | `heroCardClass` | on the existing shadcn `Card` |
| `.pg-group`, `-head`, `-icon`, `-title` | `<CapabilityGroup open icon title>` | still a native `<details>`/`<summary>` |
| `.pg-group-off` | `<CapabilityGroupNote>` | |
| `.pg-model` (+`.active`) | `<ModelRow active>` | `div`; caller keeps `role`/`tabIndex`/handlers |
| `.pg-model.pg-model-off` | `<ModelRow state="off">` | |
| `.pg-model-absent .pg-model-name` | `<ModelName muted>` | |
| `.pg-model-head` / `-name` / `-size` / `-live` / `-full` / `-foot` / `-task` / `-why` | `<ModelRowHead>` / `<ModelName>` / `<ModelSize>` / `<ModelLive>` / `<ModelFull>` / `<ModelFoot>` / `<ModelTask>` / `<ModelWhy>` | |
| `.pg-model-dl` | `<ModelDownloadButton>` | |
| `.pg-dl-ring*` (incl. idle spin) | `<ProgressRing value={0–1 \| null} />` | `null` spins the quarter arc |
| `.pg-hero-dl*` | `<DownloadSwapRoot>` › `<DownloadSwapIcon>` + `<DownloadSwap>` › `<DownloadSwapLive>` / `<DownloadSwapStop>` / `<DownloadSwapBytes>` | the hover/focus-within swap is `group-*` on the root |
| `.pg-work` (+`.has-config`) | `workGridClass(open)` | className-only swap for the five stage roots |
| `.pg-work-card` | `stageWorkCardClass` | add to the stage's `Card` |
| `.pg-work-head` / `-title` | `<StageHeader>` / `<StageTitle>` | |
| `.pg-cog` (+`.active`) | `<ConfigCog active>` | |
| `.pg-config-card` (+`.is-closing`, `.no-entry`) | `<ConfigRail closing animated>` | |
| `.pg-config-inner` | `configCardClass` | on the settings `Card` |
| `.pg-config-chips` | `<ConfigChips>` | |
| `.pg-chips`, `.pg-chip` (+`.active`) | `<Chips options active onPick>` | shadcn `ToggleGroup` + base UI `Toggle` |
| `.pg-composer` (+`.pg-composer-stack`) | `<Composer layout="row" \| "stacked">` | keeps the `pg-composer` **tour hook** |
| `.pg-composer textarea` / `input` | `composerTextareaClass` / `composerInputClass` / `composerStackTextareaClass` | |
| `.pg-composer-side` | `<ComposerSide>` (`flat` for the stacked floor) | |
| `.pg-composer-foot` | `<ComposerFoot>` | |
| `.pg-ghost-btn` | `<GhostButton>` | |
| `.pg-clear`, `.pg-clear-corner` | `<ClearButton placement="inline" \| "corner" \| "bare">` | `bare` is the audio row's |
| `.btn.btn-primary/.btn-secondary` + `.pg-send` | `<StageButton variant="primary" \| "secondary">` | shadcn `Button`; keeps the `pg-send` **tour hook** |
| `.pg-kbd` | `<ComposerKbd>` | shadcn `Kbd`, plate removed |
| `.pg-attach-row` / `.pg-attach` / `.pg-attach-open` / `.pg-attach-drop` / `.pg-attach-note` | `<AttachRow>` / `<AttachChip>` / `<AttachOpen>` / `<AttachDrop>` / `<AttachNote>` | `AttachOpen` styles its own `<img>` |
| `.pg-attach-btn` (+`.active`) | `<AttachButton active>` / `attachButtonVariants()` for a `<label>` | |
| `.pg-lightbox` | `<Lightbox open onClose label>` | shadcn `Dialog`; **Esc and click-outside are the Dialog's** |
| `.pg-lightbox img` | `lightboxImageClass` | keep the caller's `stopPropagation` |
| `.pg-lightbox-close` | `<LightboxClose>` | |
| `.pg-webcam-box` / `.pg-webcam-video` | `<LightboxBox>` / `webcamVideoClass` | |
| `.pg-answer-block` / `-label` / `-provenance` | `<AnswerBlock>` / `<AnswerLabel>` / `<AnswerProvenance>` | `AnswerBlock` keeps `pg-answer-block` as a **tour hook** |
| `.pg-answer` | `<AnswerCard>` | |
| `.pg-copy-btn` | `<CopyButton>` | `copyButtonVariants({variant:"scrim"})` for `.pg-image-save` on an `<a download>` |
| `.pg-turn-foot` | `<TurnFoot>` | |
| `.pg-cursor` | `<Cursor>` | |
| `.pg-think` / `-body` | `<ThinkBlock>` / `<ThinkBody>` | |
| `.pg-status` / `.pg-error` / `.pg-blocked-ask` | `<StageStatus>` / `<StageError>` / `<BlockedAsk>` | |
| `.pg-slot*` | `<ResultSlot icon note>` | shadcn `Empty` |
| `.pg-starters` / `-grid` / `-card` / `-icon` / `-rotate` | `starterRowClass` / `starterGridClass` / `<StarterPill>` / `<StarterIcon>` / `<StarterRotate>` | the two class strings so the caller keeps its measuring `ref` |
| `.pg-bar` / `-fill` | `<ProgressBar value={0–100} />` | shadcn `Progress` |
| `.pg-image-result` / `-frame` / `img,video` / `-readfailed` / `-wait` / `-caption` | `<MediaResult>` / `<MediaFrame>` / `mediaClass` / `<MediaReadFailed>` / `<MediaWait>` / `<MediaCaption>` | `MediaWait` is shadcn `Skeleton` with the shimmer |
| `.pg-seed` | `<SeedButton>` | |
| `.pg-image-strip` (+`img/video.active/.disabled`) | `<MediaStrip>` + `mediaStripItemClass({active,disabled})` | |
| `.pg-dropzone` (+`.dragging`, `.busy`) | `<Dropzone dragging busy>` | |
| `.pg-drop-copy` / `-title` / `-sub` / `.pg-browse` | `<DropCopy>` / `<DropTitle>` / `<DropSub>` / `<BrowseLabel>` | `BrowseLabel` hides its own file input |
| `.pg-rec-btn` (+`.live`) / `-dot` / `-square` | `<RecordButton live>` / `<RecordDot>` / `<RecordSquare>` | |
| `.pg-recording` / `.pg-rec-info` / `-time` / `-hint` | `<RecordingRow>` / `<RecordInfo>` / `<RecordTime>` / `<RecordHint>` | |
| `.pg-meter` / `-bar` (+`.lit`) | `<LevelMeter>` + `levelMeterBarClass(lit)` | |
| `.pg-audio-row` / `-meta` / `-label` / `-name` / `.pg-audio` | `<AudioRow>` / `<AudioMeta>` / `<AudioLabel>` / `<AudioName>` / `audioPlayerClass` | |
| `.pg-segments` / `.pg-segment` / `-time` / `-text` | `<SegmentList>` / `<Segment>` / `<SegmentTime>` / `<SegmentText>` | |
| `.pg-transcript-text` (+`-empty`) | `<TranscriptText empty>` | |
| `.pg-embed` / `-results` / `-row` / `-bar` / `-text` / `-score` | `embedStageClass` / `<EmbedResults>` / `<EmbedRow media>` / `<EmbedBar>` / `<EmbedText>` / `<EmbedScore>` | `media` is the thumbnails' centre alignment |
| `.pg-embed-pictures` / `-picture-row` / `-picture-name` / `.pg-embed-thumb` | `<EmbedPictures>` / `<EmbedPictureRow>` / `<EmbedPictureName>` / `embedThumbClass` | |
| `.pg-md*` | `markdownClass` | one container class; `markdownCodeClass`, `markdownCodeHeadClass` for the fenced block |

## Four rules to migrate by

1. **`font: inherit` is longhands here, never the shorthand.** Tailwind emits an
   arbitrary property (`[font-weight:inherit]`) *after* the utilities for the
   same property, so the arbitrary one always wins and tailwind-merge does not
   deduplicate the pair. Take `INHERIT_FONT_FAMILY` / `INHERIT_FONT_FACE` /
   `INHERIT_FONT` / `INHERIT_FONT_ALL` (`classes.ts`) — whichever one leaves the
   metric you are setting alone. `[font-variant:inherit]` is deliberately absent:
   it is a shorthand, and it silently killed `tabular-nums` on the chips.
2. **A named `text-*` also sets a line-height.** `text-sm` is
   `font-size: .875rem; line-height: var(--tw-leading, 1.4286)`, where the old
   rule set font-size and left leading at `normal`. Use `text-[14px]` unless the
   original named a line-height too (then any `leading-*` wins, whatever the
   order).
3. **Never pair `border-solid` with a one-side width utility.** `border-solid`
   sets `border-style` on all four sides, and a side whose width was never named
   then draws at the initial `medium` — 3px. `border-r` / `border-l-2` /
   `border-b` already set their own side's style; only add `border-solid` /
   `border-dashed` next to an all-sides `border`, `border-2`, `border-[1.5px]`.
4. **`border: 0` is `border-none`, not `border-0`.** `border-0` sets
   `border-style: solid` with a zero width; the original left the style at
   `none`. Both are invisible, but only one matches the computed style.

## Three tour hooks that must survive

`platform/lib/tours/ai.ts` drives the AI tour off live selectors, and
`registry.test.ts` pins them: `.pg-side`, `.pg-composer`,
`.pg-composer textarea`, `.pg-composer .pg-send`, `.pg-answer-block`. Those five
class names stay on their elements as **hooks with no style of their own**
(`TOUR_MODEL_RAIL`, `TOUR_COMPOSER`, `TOUR_SEND`, `TOUR_ANSWER_BLOCK`). Do not
drop them when `ai-playground.css` goes; retarget the tour first if you want
them gone.

## Known deviations from `main`

- Tailwind's `hover:` variant compiles to `@media (hover: hover) { &:hover }`;
  the old plain `:hover` had no such gate. Identical on a pointer device. Where
  the rule was `:hover:not(:disabled)` the composites use the arbitrary variant
  `[&:hover:not(:disabled)]:…`, which is **not** media-gated — same selector as
  before.
- `Chips` is a base UI `ToggleGroup`, which is a composite: one chip is tabbable
  and the arrow keys move between them, where the old row was five separately
  tabbable buttons. `aria-pressed`, `title`, `role="group"` and the click
  behaviour are unchanged.
- `Lightbox` is a `Dialog` with `modal={false}` — no focus trap and no scroll
  lock, so nothing shifts behind the picture. Escape and click-outside are the
  Dialog's; the callers' own window `keydown` listeners are harmless beside it.
- `ProgressBar` and `ProgressRing` inherit base UI's `role="progressbar"` and
  its aria value attributes, which the old bare `<span>`s did not have.
- `rounded-[999px]` / `rounded-[50%]` are spelled out rather than `rounded-full`
  so the computed `border-radius` matches the old value exactly.
