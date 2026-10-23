// Which component draws which setting. Typed against `SettingId`, so a row
// added to the registry without a control here does not compile.
import type { ComponentType } from "react";
import type { SettingId } from "./registry";
import type { PrefControlProps } from "./SettingRow";
import {
  AppearanceControl,
  AutoUpdateControl,
  ChromiumWindowsControl,
  LauncherHotkeyControl,
  LauncherRowControl,
  NativeWindowsControl,
  ThemeControl,
} from "./general";
import {
  AgentSigninPointer,
  ClaudeAgentControl,
  ClaudeModelPointer,
  CodexAgentControl,
  CopilotAgentControl,
  DefaultAgentControl,
  EnginesPointer,
  GeminiAgentControl,
} from "./ai";
import {
  CanvasesControl,
  CanvasesEmbedProxyControl,
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
  TaskSummaryTitlesControl,
  ClaudeStrictMcpControl,
  ClaudeDefaultPermissionControl,
} from "./toggles";
import {
  IndexingControl,
  LowImpactControl,
  RankedSearchControl,
  RebuildIndexControl,
  SkippedFoldersControl,
} from "./indexing";
import { LanControl } from "./lan";
import { ClaudeCliMissing } from "./claude";
import { FusedAccountControl, HuggingFaceControl, WorkbenchEnvControl } from "./accounts";
import {
  CallLogControl,
  CallParamsControl,
  CallRetentionControl,
  ClaudeMaxSessionsControl,
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
  "agent-codex": CodexAgentControl,
  "agent-gemini": GeminiAgentControl,
  "agent-default": DefaultAgentControl,
  "claude-model": ClaudeModelPointer,
  "claude-cli-missing": ClaudeCliMissing,
  engines: EnginesPointer,
  "project-queue": ProjectQueueControl,
  "task-recaps": TaskRecapsControl,
  "task-summary-titles": TaskSummaryTitlesControl,
  "claude-strict-mcp": ClaudeStrictMcpControl,
  "claude-default-permission": ClaudeDefaultPermissionControl,
  "claude-max-sessions": ClaudeMaxSessionsControl,
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
  "canvases-embed-proxy": CanvasesEmbedProxyControl,
  marketplace: MarketplaceControl,
  monitor: MonitorControl,
  reader: ReaderControl,
  "chromium-windows": ChromiumWindowsControl,
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
