// The canvas workspace's RIGHT PANE while a node is selected in the embedded
// workbench: a header (close + a two-tab strip) over either the node's Claude
// chat or its Code tab.
//
// The header is the explorer file sidebar's idiom (SideChrome: the panel glyph
// on the seam, shadcn Tabs in the line variant at the tail) built from the same
// PLATFORM parts, not imported from apps/explorer — the boundary rule
// (scripts/check-boundaries.mjs) keeps one app out of another's modules, and
// PreviewSidebar itself is portaled into the explorer's stat-split slot and
// bound to its side stores anyway. Here the column is the workspace's own
// right pane: CanvasWorkspace owns the divider and the width (owner call: the
// node sidebar inherits the canvas split, no width of its own).
//
//   Claude  — ChatMount on `<clone>/<udfName>.py` (chat-only; the workbench
//             frame beside it is the annotate target). Its `entry` prop is
//             what files the session under THIS file's Recent list: the
//             workbench frame is cross-origin, so the chat cannot read a pane
//             and would otherwise record a folder chat (ClaudeChat `entry`).
//   Code    — fused-render's own `code` template framed on the same file,
//             exactly the URL shape the explorer's sidebar builds
//             (`/render?path=<template>&_file=<file>`). It is WRITABLE with
//             autosave (owner call): with the workbench's editor hidden in
//             embed v2, this tab is the user's editor for the node, and its
//             saves ride the same sync loop Claude's edits do.
import type { ReactNode } from "react";
import { ChatMount } from "@apps/claude";
import { rawUrl, type TemplateEntry } from "@platform/lib/api";
import { modeTitle } from "@platform/lib/mode-name";
import { Tabs, TabsList, TabsTrigger } from "@platform/shadcn/ui/tabs";
import PanelIcon from "@platform/ui/PanelIcon";

export type NodeTab = "claude" | "code";

export interface NodeSidebarProps {
  /** `<clone>/<udfName>.py` (or the widget's `.json`). */
  file: string;
  /** The node's display name (the UDF name), for the header. */
  udfName: string;
  /** Whether the file exists in the clone yet. Before the first pull lands
   *  (a `?node=` restored on a fresh clone) it may not: the column then holds
   *  a note instead of framing a template on a path that 404s. */
  exists: boolean;
  /** The `code` template's registry entry for this file, or null while
   *  unknown / when the registry offers none. */
  codeTemplate: TemplateEntry | null;
  /** The `claude` template entry, for the tab's icon. */
  claudeTemplate: TemplateEntry | null;
  tab: NodeTab;
  onTab: (tab: NodeTab) => void;
  onClose: () => void;
  /** The sibling workbench iframe (CanvasWorkspace's `frameRef`). */
  annotateTarget: () => HTMLIFrameElement | null;
  /** Remount key for the chat (a new node = a fresh chat boot). */
  chatKey: string;
}

/** The registry icon as the mode menus draw it (a masked span), or the
 *  explorer's one-letter placeholder when the template ships none. */
function modeIcon(entry: TemplateEntry | null, mode: string): ReactNode {
  if (!entry || entry.icon === null) {
    return <span className="mode-icon-placeholder">{mode.charAt(0).toUpperCase()}</span>;
  }
  const mask = `url("${rawUrl(entry.icon)}")`;
  return <span className="mode-icon-mask" style={{ WebkitMaskImage: mask, maskImage: mask }} />;
}

export default function NodeSidebar(props: NodeSidebarProps) {
  const codeSrc =
    props.exists && props.codeTemplate?.path
      ? `/render?path=${encodeURIComponent(props.codeTemplate.path)}` +
        `&_file=${encodeURIComponent(props.file)}`
      : null;
  const codeReason = !props.exists
    ? "Waiting for the sync to bring this file down"
    : !props.codeTemplate?.path
      ? "No code view is registered for this file"
      : undefined;
  const closeLabel = "Hide the " + modeTitle(props.tab) + " panel";
  return (
    <aside
      className="canvas-node-side"
      aria-label={modeTitle(props.tab) + " sidebar for " + props.udfName}
    >
      <div className="preview-side-header">
        <button
          type="button"
          className="bar-ctl bar-ctl-icon side-close"
          title={closeLabel}
          aria-label={closeLabel}
          onClick={props.onClose}
        >
          <PanelIcon side="right" />
        </button>
        <span className="canvas-node-side-name" title={props.file}>
          {props.udfName}
        </span>
        <div className="side-header-tail">
          <Tabs
            value={props.tab}
            onValueChange={(v) => {
              if (v === "code" || v === "claude") props.onTab(v);
            }}
            className="side-tabs self-stretch"
          >
            <TabsList
              variant="line"
              aria-label="Node panel"
              className="h-full justify-start gap-3 rounded-none border-b-0 p-0"
            >
              <TabsTrigger value="claude" className="flex-none px-2 text-[12px]">
                <span className="mode-menu-icon" data-icon="inline-start">
                  {modeIcon(props.claudeTemplate, "claude")}
                </span>
                {modeTitle("claude")}
              </TabsTrigger>
              <TabsTrigger
                value="code"
                className="flex-none px-2 text-[12px]"
                disabled={!!codeReason}
                title={codeReason}
              >
                <span className="mode-menu-icon" data-icon="inline-start">
                  {modeIcon(props.codeTemplate, "code")}
                </span>
                {modeTitle("code")}
              </TabsTrigger>
            </TabsList>
          </Tabs>
        </div>
      </div>
      {props.tab === "claude" ? (
        <ChatMount
          key={props.chatKey}
          file={props.file}
          chatOnly
          paramsSource="url"
          entry={props.file}
          annotateTarget={props.annotateTarget}
        />
      ) : codeSrc ? (
        <iframe
          key={props.file}
          className="preview-side-frame"
          src={codeSrc}
          title={`Code: ${props.udfName}`}
        />
      ) : (
        <div className="preview-resolving">
          <span className="mode-icon-spinner" />
          {codeReason}…
        </div>
      )}
    </aside>
  );
}
