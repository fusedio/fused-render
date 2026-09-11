// The LEFT APP PANE subsystem: what it frames, how wide it is, which view it
// shows, and what the agent can read off it. Behaviour source:
// .claude-design/inventory/01-boot-appstate-pane.md (and 02's applyNarrowView),
// citing fused_render/templates/claude/template.html as `T`.
export {
  APP_STATE_UNREADABLE,
  annotateLabelFor,
  appEntrySrc,
  curLeftEntry,
  decidePane,
  footnoteFor,
  homePlaceholderFor,
  PANE_SKIP_MODES,
  paneModeIconUrl,
  paneModeLabel,
  paneModeLetter,
  paneNounFor,
  paneOfferable,
  paneSrcFor,
  shotLabelFor,
  type PaneDecision,
  type PaneDecisionInput,
  type PaneKind,
  type PaneNoun,
  type PaneSrcFlags,
  type TargetNoun,
} from "./paneUrl";

export {
  AppPane,
  enterNoPane,
  usePaneState,
  useFramedSrc,
  type AppPaneProps,
  type NoPaneSteps,
  type PaneState,
  type PaneStatus,
  type UsePaneStateOptions,
} from "./AppPane";

export {
  LeftModePicker,
  leftBarShown,
  pickerHost,
  type LeftModePickerProps,
} from "./LeftModePicker";

export { SplitDivider, type SplitDividerProps } from "./SplitDivider";
export {
  clampPct,
  pctFromPointer,
  splitPctFromParam,
  splitWidth,
  useSplit,
  SPLIT_DEFAULT,
  SPLIT_MAX,
  SPLIT_MIN,
  type SplitState,
  type UseSplitOptions,
} from "./useSplit";

export {
  narrowViewOf,
  useNarrowView,
  viewClassNames,
  viewToggleLabel,
  NARROW_MQ_QUERY,
  type NarrowView,
  type NarrowViewState,
  type UseNarrowViewOptions,
} from "./useNarrowView";
export { ViewToggle, type ViewToggleProps } from "./ViewToggle";

export {
  appParamsOf,
  clipText,
  createAppStateWatcher,
  fmtLogArg,
  outlineNode,
  searchParamsOf,
  APP_STATE_MAX_DEPTH,
  APP_STATE_MAX_LOGS,
  APP_STATE_MAX_NODES,
  APP_STATE_MAX_NODE_TEXT,
  APP_STATE_MAX_TEXT,
  APP_STATE_TAG,
  APP_STATE_WIRE_LOGS,
  CHAT_PARAMS,
  type AppLogEntry,
  type AppStateSnapshot,
  type AppStateWatcher,
  type AppStateWatcherOptions,
  type OutlineNode,
  type PathOf,
} from "./appState";

export {
  appStateTrim,
  useAppStateResponder,
  APP_STATE_MEMO_MAX,
  APP_STATE_NULL_POLLS,
  type AppStateResponderOptions,
} from "./useAppStateResponder";
