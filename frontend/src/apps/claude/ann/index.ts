// THE ANNOTATION SUBSYSTEM (PR3, inventory 02 §A's 52 capabilities).
// Behaviour source: `.claude-design/inventory/02-annotations-narrow.md`, citing
// `fused_render/templates/claude/template.html` as `T`. See `README.md` for the
// seams the integrator has to wire.
export type {
  AnnAnchor,
  AnnKind,
  AnnLayout,
  AnnMode,
  AnnRecorder,
  AnnTool,
  Annotation,
} from "./types";
export {
  ANN_ARMED_TITLE,
  ANN_BAR,
  ANN_BAR_TOKENS,
  ANN_LAYER_MARK,
  ANN_OFFSCREEN_DETACHED,
  ANN_OFFSCREEN_SCROLLED,
  ANN_TARGET_MARK,
  ANN_TARGET_POLL_MS,
  ANN_XO_SCROLL,
} from "./types";

export {
  ANN_BAR_H,
  ANN_PIN_CLAMP,
  ANN_POP_H,
  ANN_POP_OFFSET,
  ANN_POP_W,
  badgeXY,
  barFolds,
  barNeed,
  chipEditXY,
  clockOf,
  contentBox,
  contentBoxOf,
  elementPinXY,
  intrinsicOf,
  iuivAt,
  labelFor,
  pageXY,
  pathOf,
  pinAt,
  pointXY,
  popAt,
  rectOf,
  resolveIn,
  stampOf,
  type AnnRect,
  type BarMetrics,
  type ScrollSource,
  type StageBox,
} from "./geometry";

export {
  ANN_MODE_PARAM,
  ANN_PARAM,
  createAnnStore,
  parseAnnotations,
  serializeAnnotations,
  type AnnStore,
  type AnnStoreOptions,
} from "./store";

export { createAnnTarget, type AnnTarget, type AnnTargetOptions } from "./target";

export {
  ANN_LAYER_CSS,
  createRenderQueue,
  createXOLayer,
  hideHl,
  injectLayer,
  paintPins,
  pinSpotOf,
  placeHl,
  removeLayer,
  type AnnLayerBindings,
  type PinPaint,
  type XOLayerOptions,
} from "./layer";

export { isWired, wireTarget, type WireTargetDeps } from "./wire-target";

export {
  createAnnMode,
  escapeAction,
  type AnnModeDeps,
  type AnnModeMachine,
} from "./mode";

export {
  applyOverview,
  badgesFor,
  overviewFor,
  type AnnMark,
  type OverviewContext,
  type OverviewResult,
} from "./overview";

export {
  AnnBar,
  barClock,
  barFit,
  barTheme,
  buildBarNode,
  createBarPush,
  disposeBarNode,
  paintBar,
  rewireBar,
  type AnnBarHandlers,
  type AnnBarProps,
  type BarPaintState,
} from "./AnnBar";

export {
  ANN_DISMISS_EXEMPT,
  ANN_KEY_EVENTS,
  AnnPopover,
  buildPopNode,
  buildToolNode,
  closeComposer,
  createToolDoors,
  dismissesComposer,
  isOpen,
  isPortaled,
  openComposer,
  openEditor,
  paintTool,
  placeholderFor,
  placePop,
  portalPop,
  toolFromClick,
  unportalPop,
  type AnnPopoverHandlers,
  type AnnPopoverProps,
  type PlacePopOptions,
} from "./AnnPopover";

export { AnnPins, type AnnPinsProps } from "./AnnPins";
export { AnnChips, chipsOf, type AnnChipItem, type AnnChipsProps } from "./AnnChips";

export {
  recClockText,
  seatsAria,
  useAnnotations,
  type AnnotationsApi,
  type UseAnnotationsOptions,
} from "./useAnnotations";

// ── the voice walkthrough (`rec*`, `transcribe*`) ──────────────────────────
// The recorder plugs into the coordinator through the `AnnRecorder` seam in
// `types.ts`; these are what the integrator builds it out of.
export {
  assignWords,
  COMMENT_SEAT_WHILE_RECORDING,
  COMMENT_SEAT_WHILE_SETTLING,
  createRecorder,
  recClock,
  recIdleName,
  recSeatName,
  REC_MIN_SECONDS,
  REC_TICK_MS,
  type RecAnchor,
  type RecAnnotation,
  type Recorder,
  type RecorderDeps,
  type RecModePort,
  type RecNotesPort,
  type RecSnapshot,
  type RecState,
  type SeatName,
} from "./rec";
export { commentSeatName, RecControls, type RecControlsProps } from "./RecControls";
export {
  ASR_CAPABILITY,
  asrCapability,
  transcribe,
  warmTranscriber,
  type Transcript,
  type TranscribeOptions,
  type TranscriptWord,
} from "./transcribe";
