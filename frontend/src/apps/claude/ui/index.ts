// Barrel for the chat's React surface. Composer / chrome / home exports only —
// the transcript, cards and panes append their own below.
export {
  Composer,
  ComposerCard,
  CHAT_PLACEHOLDER,
  HOME_PLACEHOLDER,
  FOOTNOTE_LEAD,
  FOOTNOTE_TAIL,
} from "./Composer";
export type {
  ComposerProps,
  ComposerCardProps,
  ComposerControls,
} from "./Composer";
export { ModelSelect } from "./ModelSelect";
export { EffortSelect } from "./EffortSelect";
export { PermissionSelect } from "./PermissionSelect";
export { PillSelect } from "./PillSelect";
export type { PillOption, PillSelectProps } from "./PillSelect";
/** The window `blur`/`resize` dismissals Base UI does not cover (T:12602,
 *  T:12605). Exported because three popovers want them and PR4's Schedule seat
 *  and confirm are two of the three. */
export { useDismissOnWindow } from "./useDismissOnWindow";
export type { DismissOnWindowOptions } from "./useDismissOnWindow";
export { SchedButton } from "./SchedButton";
export { SchedConfirm, SchedConfirmBody } from "./SchedConfirm";
export {
  draftKey,
  stashDraft,
  takeDraft,
  schedulerUrl,
  SCHEDULE_URL,
} from "./sched-draft";
export { Topbar } from "./Topbar";
export type { TopbarProps } from "./Topbar";
export { Kebab, forgetTaskCaches, knownTaskId, useTaskId } from "./Kebab";
export { ClaudeMark, CLAUDE_MARK_PATH } from "./ClaudeMark";
export type { ClaudeMarkProps } from "./ClaudeMark";
export { SentPop } from "./SentPop";
export { Home } from "./Home";
export type { HomeProps } from "./Home";
export { HomeCard } from "./HomeCard";
export type { HomeCardProps } from "./HomeCard";
export { Lists, RecentSkeleton, LIST_LABELS } from "./Lists";
export type { ListsProps } from "./Lists";
export { RecentRow } from "./RecentRow";
export { useRecentSessions } from "./useRecentSessions";
export type { RecentRowProps } from "./RecentRow";
export { ArtifactRow } from "./ArtifactRow";
export type { ArtifactRowProps } from "./ArtifactRow";
export { SnapRow } from "./SnapRow";
export type { SnapRowProps } from "./SnapRow";
export { SchedBlock } from "./SchedBlock";
export type { SchedBlockProps } from "./SchedBlock";
export { ArtStrip, useArtStrip } from "./ArtStrip";
export type { ArtStripProps, ArtStripStore } from "./ArtStrip";
export { Snapshots } from "./Snapshots";
export type { SnapshotsProps } from "./Snapshots";
export { useArtifacts } from "./useArtifacts";
export { useRepairScroll } from "./useRepairScroll";
export { useSnapshots } from "./useSnapshots";
export type { SnapshotsState } from "./useSnapshots";
export {
  computeLists,
  filledTabs,
  isAlone,
  isFilled,
  nextTab,
  LIST_NAMES,
} from "./lists-visibility";
export type { ListCounts, ListName, ListVisibility } from "./lists-visibility";
export {
  MODELS,
  MODEL_LABELS,
  EFFORTS,
  PERMISSION_MODES,
  PERMISSION_LABELS,
  PERMISSION_SHORT,
  DEFAULT_MODEL,
  DEFAULT_EFFORT,
  DEFAULT_PERMISSION,
  GROUP_LABELS,
  PILL_ARIA,
  resolveModel,
  resolveEffort,
  resolvePermission,
  useComposerDefaults,
} from "./composer-defaults";
export type { ComposerDefaults } from "./composer-defaults";
export {
  ago,
  paneChatUrl,
  paneSlashes,
  rowPane,
  sessionTitle,
} from "./list-rows";
export {
  rowNeed,
  pickRowFit,
  fitFlags,
  footnoteTight,
  pickHomeTitleStep,
  HOME_TITLE_STEPS,
  fitSelect,
  measureRowNeed,
  measureTextIn,
  readRow,
  applyLead2,
} from "./fit";
export type { RowFit, Seat, RowBox } from "./fit";

// ---- transcript / cards (owned by the transcript agent) --------------------
// Restored after this file was overwritten by mistake; if that agent's own
// barrel differs, theirs wins.
export {
  Transcript,
  ANCHOR_FLARE_MS,
  ANCHOR_SETTLE_MS,
  lastErrorKey,
  openCardIds,
} from "./Transcript";
export type { TranscriptProps, TranscriptTail } from "./Transcript";
export { Turn } from "./Turn";
export type { TurnProps } from "./Turn";
export { SegmentView } from "./SegmentView";
export type { SegmentViewProps, SegmentTail } from "./SegmentView";
export { Caret } from "./Caret";
export { MarkdownView, INNER_HTML_SITES } from "./MarkdownView";
export type { MarkdownViewProps } from "./MarkdownView";
export { ToolChip } from "./ToolChip";
export type { ToolChipProps } from "./ToolChip";
export { ThinkingView } from "./ThinkingView";
export type { ThinkingViewProps } from "./ThinkingView";
export { NoticeView } from "./NoticeView";
export { PermCard } from "./PermCard";
export type { PermCardProps } from "./PermCard";
export { QuestionCard } from "./QuestionCard";
export type { QuestionCardProps } from "./QuestionCard";
export { PlanCard } from "./PlanCard";
export type { PlanCardProps } from "./PlanCard";
export { CardStack } from "./CardStack";
export type { CardStackProps, CardActions } from "./CardStack";
export {
  WorkingLine,
  VERBS,
  retryVerb,
  activityVerb,
  activityDetail,
} from "./WorkingLine";
export type { WorkingLineProps, VerbStats } from "./WorkingLine";
export { NO_TARGET_SAID, TroubleView } from "./TroubleView";
export type { TroubleViewProps } from "./TroubleView";
export {
  CardPolicyProvider,
  createCardPolicy,
  resetCardPolicy,
  cardKey,
  useCardOpen,
  cardOverride,
  type CardPolicy,
} from "./cardPolicy";

// ---- attachments (PR2, inventory 03) --------------------------------------
export { AttachTray, ShotThumb } from "./AttachTray";
export type { AttachTrayProps } from "./AttachTray";
export { ShotViewer, ShotViewerBody } from "./ShotViewer";
export type { ShotViewerProps } from "./ShotViewer";
export { Receipts } from "./Receipts";
export type { ReceiptsProps } from "./Receipts";
export { AnnStrip } from "./AnnStrip";
export { AttachIcon, MarkerText } from "./AttachIcon";
export type { AttachIconProps } from "./AttachIcon";
export type { AnnStripProps } from "./AnnStrip";
export { useAttachments } from "./useAttachments";
export { fitStrip, useFitStrip } from "./useFitStrip";
export { mergeSendOptions, sendBlocks } from "./sendMerge";
export type {
  Attachments,
  OutgoingAttachments,
  UseAttachmentsOptions,
} from "./useAttachments";
export {
  ATTACH_API,
  failLabel,
  glyphDoor,
  liveViewable,
  prunedLabel,
  previewSrcFor,
  receiptFor,
  receiptsFromWire,
  receiptViewable,
  settleReceipts,
  shotAlt,
  shotNoun,
  shownSize,
  sizeLabel,
  toViewable,
  viewerOpens,
  SHOT_GONE,
} from "./attachApi";
export type { AttachApi, Viewable } from "./attachApi";
export { SentPopBody } from "./SentPop";
export type { SentPopProps } from "./SentPop";
