// The card an expert's `show` tool puts under its line (docs/experts.md §17): a canvas, or one node of a canvas
// (a UDF, a widget, an HTML node, a note). Collapsed it is one row and renders nothing of the work itself, so a
// long thread never runs a workbench frame per message. "Open" raises ONE overlay that draws the thing the way
// the explorer does when the same file is opened there: a node through the `_node` native view
// (platform/ui/NativeView, the canvases app's NodeView: the hosted workbench in a frame, cloned and synced as
// needed), a whole canvas as its workspace page in a chrome-free frame. Nothing is mounted until it is opened,
// and closing unmounts it (the frame stops).
import { useState } from "react";
import { NODE_MODE } from "@platform/lib/mode-visibility";
import { navigateUrl, urlForFsPath } from "@platform/lib/router";
import { Modal } from "@platform/ui/modal/Modal";
import { NativeView, hasNativeView } from "@platform/ui/NativeView";
import type { ViewRef } from "../lib/api";

const KIND_LABEL: Record<string, string> = { canvas: "Canvas", udf: "UDF", widget: "Widget", html: "HTML", note: "Note" };
const KIND_ICON: Record<string, string> = { canvas: "▦", udf: "ƒ", widget: "◫", html: "‹›", note: "¶" };

export const viewKindLabel = (kind: string): string => KIND_LABEL[kind] || "File";

/** A usable `view` payload, or null: the row is the server's, but an old or hand-edited events file is not trusted to be whole. */
export function viewOf(raw: unknown): ViewRef | null {
  const v = raw as Partial<ViewRef> | null | undefined;
  if (!v || typeof v !== "object" || typeof v.path !== "string" || !v.path.startsWith("/") || typeof v.kind !== "string") return null;
  return { kind: v.kind, path: v.path, title: typeof v.title === "string" && v.title ? v.title : v.path.split("/").pop() || v.path,
    canvas: typeof v.canvas === "string" ? v.canvas : "", env: typeof v.env === "string" ? v.env : "" };
}

/** The whole canvas, as its own page without the shell's chrome (`?embed=1`, router IS_QUERY_EMBED) and with its
 *  Claude pane shut (`side=off`, the canvas page's own word): the overlay is opened from a chat already. */
export const canvasFrameUrl = (v: ViewRef): string =>
  `/canvases/${encodeURIComponent(v.env)}/${encodeURIComponent(v.canvas)}?embed=1&side=off`;
export const canvasPageUrl = (v: ViewRef): string => `/canvases/${encodeURIComponent(v.env)}/${encodeURIComponent(v.canvas)}`;

/** What the card says under the title: "Widget · in Parcels_overview · prod". */
export function viewSubtitle(v: ViewRef): string {
  return [viewKindLabel(v.kind), v.kind !== "canvas" && v.canvas ? `in ${v.canvas}` : "", v.env].filter(Boolean).join(" · ");
}

/** The framed canvas page takes the keyboard as soon as it loads, so the overlay's own Escape never fires. The frame
 *  is same-origin: listen there too. A frame that will not let us in (it navigated elsewhere) keeps the ✕. */
function escFromFrame(frame: HTMLIFrameElement, onClose: () => void): void {
  try {
    frame.contentWindow?.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !e.defaultPrevented) onClose();
    });
  } catch {
    // cross-origin: nothing to listen to
  }
}

export function ViewModal({ view, onClose }: { view: ViewRef; onClose: () => void }) {
  const isCanvas = view.kind === "canvas";
  const go = (url: string) => { onClose(); void navigateUrl(url); };
  return (
    <Modal
      title={<>{view.title} <span className="bots-view-sub">{viewSubtitle(view)}</span></>}
      ariaLabel={`${viewKindLabel(view.kind)} ${view.title}`}
      onClose={onClose}
      dialogClassName="bots-view-modal"
      plainBody
      headActions={
        <>
          {/* A node's own view already has "Open in canvas" in its strip; only the whole-canvas overlay needs the door. */}
          {isCanvas && view.canvas && view.env ? <button type="button" className="bots-view-btn" onClick={() => go(canvasPageUrl(view))}>Open canvas</button> : null}
          {!isCanvas ? <button type="button" className="bots-view-btn" onClick={() => go(urlForFsPath(view.path))}>Open in explorer</button> : null}
        </>
      }
    >
      <div className="bots-view-body">
        {isCanvas ? (
          view.canvas && view.env
            ? <iframe className="bots-view-frame" src={canvasFrameUrl(view)} title={`Canvas ${view.title}`} onLoad={(ev) => escFromFrame(ev.currentTarget, onClose)} />
            : <p className="bots-view-missing">This canvas can&rsquo;t be opened from here.</p>
        ) : hasNativeView(NODE_MODE) ? (
          <NativeView mode={NODE_MODE} path={view.path} placement="content" />
        ) : (
          <p className="bots-view-missing">This view isn&rsquo;t available in this window. Use &ldquo;Open in explorer&rdquo;.</p>
        )}
      </div>
    </Modal>
  );
}

export function ViewCard({ view }: { view: ViewRef }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="appmsg viewmsg" data-view={view.path}>
      <div className="hd">
        <span className="ico" aria-hidden="true">{KIND_ICON[view.kind] || "▢"}</span>
        <div className="txt">
          <b>{view.title}</b>
          <small title={view.path}>{viewSubtitle(view)}</small>
        </div>
        <span className="abtns">
          <button className="primary" onClick={() => setOpen(true)}>Open</button>
        </span>
      </div>
      {open ? <ViewModal view={view} onClose={() => setOpen(false)} /> : null}
    </div>
  );
}
