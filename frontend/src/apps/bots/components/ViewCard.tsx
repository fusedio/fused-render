// The card an expert's `show` tool puts under its line (docs/experts.md §17): a canvas, or one node of a canvas
// (a UDF, a widget, an HTML node, a note). Collapsed it is one row and renders nothing of the work itself, so a
// long thread never runs a workbench frame per message. "Open" raises ONE overlay that draws the thing the way
// the explorer does when the same file is opened there: a node through the `_node` native view
// (platform/ui/NativeView, the canvases app's NodeView: the hosted workbench in a frame, cloned and synced as
// needed), a whole canvas as its workspace page in a chrome-free frame. Nothing is mounted until it is opened,
// and closing unmounts it (the frame stops).
import { useState } from "react";
import { KIND_ICON, ViewModal, viewSubtitle, type ViewRef } from "@platform/ui/ViewModal";

export { ViewModal, canvasFrameUrl, canvasPageUrl, viewKindLabel, viewOf, viewSubtitle } from "@platform/ui/ViewModal";

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
