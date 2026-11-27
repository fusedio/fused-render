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
  ClaudeAgentControl,
  CodexAgentControl,
  CopilotAgentControl,
  DefaultAgentControl,
  GeminiAgentControl,
  OpencodeAgentControl,
  CursorAgentControl,
} from "./ai";
import {
  CanvasesControl,
  CanvasesEmbedProxyControl,
  RailExtrasControl,
  LeftRailPeekControl,
  GitSyncControl,
  LivePreviewsControl,
  MonitorControl,
  NotifyTerminalControl,
  ProjectQueueControl,
  ReaderControl,
  ShareSheetControl,
  ChatRecapControl,
  BreakoutControl,
  ExpertsControl,
  TaskSummaryTitlesControl,
  ClaudeStrictMcpControl,
  ContribRemixControl,
  ClaudeDefaultPermissionControl,
} from "./toggles";
import {
  IndexingControl,
  LowImpactControl,
  RankedSearchControl,
  RebuildIndexControl,
  SkippedFoldersControl,
} from "./indexing";
import { LanControl, LanRemoteControl } from "./lan";
import { ConnectionsPointer } from "../Connections";
import { ClaudeCliMissing } from "./claude";
import {
  HuggingFaceControl,
  WorkbenchCustomControl,
  WorkbenchProdControl,
} from "./accounts";
import {
  CallLogControl,
  CallParamsControl,
  CallRetentionControl,
  ClaudeMaxSessionsControl,
  DiagnosticsControl,
  UdfSavedControl,
  IndexQueryControl,
} from "./advanced";
import { CanvasScreenshotsControl, CanvasScreenshotsFallbackControl } from "./screenshots";

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
  "agent-opencode": OpencodeAgentControl,
  "agent-cursor": CursorAgentControl,
  "agent-default": DefaultAgentControl,
  "claude-cli-missing": ClaudeCliMissing,
  "project-queue": ProjectQueueControl,
  "chat-recap": ChatRecapControl,
  breakout: BreakoutControl,
  experts: ExpertsControl,
  "task-summary-titles": TaskSummaryTitlesControl,
  "claude-strict-mcp": ClaudeStrictMcpControl,
  "claude-default-permission": ClaudeDefaultPermissionControl,
  "canvas-screenshots": CanvasScreenshotsControl,
  "canvas-screenshots-fallback": CanvasScreenshotsFallbackControl,
  "claude-max-sessions": ClaudeMaxSessionsControl,
  "notify-terminal": NotifyTerminalControl,
  "git-sync": GitSyncControl,
  "share-sheet": ShareSheetControl,
  "live-previews": LivePreviewsControl,
  "contrib-remix": ContribRemixControl,
  indexing: IndexingControl,
  "ranked-search": RankedSearchControl,
  "low-impact": LowImpactControl,
  "skipped-folders": SkippedFoldersControl,
  "rebuild-index": RebuildIndexControl,
  canvases: CanvasesControl,
  "canvases-embed-proxy": CanvasesEmbedProxyControl,
  "rail-extras": RailExtrasControl,
  "left-rail-peek": LeftRailPeekControl,
  monitor: MonitorControl,
  reader: ReaderControl,
  "chromium-windows": ChromiumWindowsControl,
  lan: LanControl,
  "lan-remote": LanRemoteControl,
  connections: ConnectionsPointer,
  "workbench-prod": WorkbenchProdControl,
  "workbench-custom": WorkbenchCustomControl,
  huggingface: HuggingFaceControl,
  "call-log": CallLogControl,
  "call-params": CallParamsControl,
  "call-retention": CallRetentionControl,
  "index-query": IndexQueryControl,
  "udf-saved": UdfSavedControl,
  diagnostics: DiagnosticsControl,
};
