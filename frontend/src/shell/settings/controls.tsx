// Which component draws which setting. Typed against `SettingId`, so a row
// added to the registry without a control here does not compile.
import type { ComponentType } from "react";
import type { SettingId } from "./registry";
import type { PrefControlProps } from "./SettingRow";
import {
  AppearanceControl,
  AutoUpdateControl,
  LauncherHotkeyControl,
  LauncherRowControl,
  NativeWindowsControl,
  ThemeControl,
} from "./general";
import {
  AgentSigninPointer,
  ClaudeAgentControl,
  ClaudeModelPointer,
  CopilotAgentControl,
  DefaultAgentControl,
  EnginesPointer,
} from "./ai";
import {
  CanvasesControl,
  GithubLinkControl,
  GitSyncControl,
  LivePreviewsControl,
  MarketplaceControl,
  MonitorControl,
  NotifyTerminalControl,
  ProjectQueueControl,
  ReaderControl,
  ShareSheetControl,
  TaskRecapsControl,
  ChatHeadsupControl,
} from "./toggles";
import {
  IndexingControl,
  LowImpactControl,
  RankedSearchControl,
  RebuildIndexControl,
  SkippedFoldersControl,
} from "./indexing";
import { LanControl } from "./lan";
import { FusedAccountControl, HuggingFaceControl, WorkbenchEnvControl } from "./accounts";
import {
  ClaudeCatalogControl,
  ClaudeHistoryBlock,
  ClaudeMcpBlock,
  ClaudeMemoryBlock,
  ClaudePluginsBlock,
  ClaudeSkillsBlock,
  ClaudeStatuslineBlock,
} from "./claude";
import {
  CallLogControl,
  CallParamsControl,
  CallRetentionControl,
  DiagnosticsControl,
  IndexQueryControl,
} from "./advanced";

export const CONTROLS: Record<SettingId, ComponentType<PrefControlProps>> = {
  appearance: AppearanceControl,
  theme: ThemeControl,
  "native-windows": NativeWindowsControl,
  "launcher-hotkey": LauncherHotkeyControl,
  "launcher-row": LauncherRowControl,
  "auto-update": AutoUpdateControl,
  "agent-claude": ClaudeAgentControl,
  "agent-copilot": CopilotAgentControl,
  "agent-default": DefaultAgentControl,
  "claude-model": ClaudeModelPointer,
  engines: EnginesPointer,
  "claude-plugins": ClaudePluginsBlock,
  "claude-memory": ClaudeMemoryBlock,
  "claude-skills": ClaudeSkillsBlock,
  "claude-statusline": ClaudeStatuslineBlock,
  "claude-mcp": ClaudeMcpBlock,
  "claude-catalog": ClaudeCatalogControl,
  "claude-history": ClaudeHistoryBlock,
  "project-queue": ProjectQueueControl,
  "task-recaps": TaskRecapsControl,
  "chat-headsup": ChatHeadsupControl,
  "notify-terminal": NotifyTerminalControl,
  "git-sync": GitSyncControl,
  "share-sheet": ShareSheetControl,
  "github-link": GithubLinkControl,
  "live-previews": LivePreviewsControl,
  indexing: IndexingControl,
  "ranked-search": RankedSearchControl,
  "low-impact": LowImpactControl,
  "skipped-folders": SkippedFoldersControl,
  "rebuild-index": RebuildIndexControl,
  canvases: CanvasesControl,
  marketplace: MarketplaceControl,
  monitor: MonitorControl,
  reader: ReaderControl,
  lan: LanControl,
  "fused-account": FusedAccountControl,
  "workbench-env": WorkbenchEnvControl,
  huggingface: HuggingFaceControl,
  "agent-signin": AgentSigninPointer,
  "call-log": CallLogControl,
  "call-params": CallParamsControl,
  "call-retention": CallRetentionControl,
  "index-query": IndexQueryControl,
  diagnostics: DiagnosticsControl,
};
