// The card a specialist's `show` tool puts in its main chat (docs/specialists.md §15), drawn in place of that
// tool's row: one line with the kind, the title and an Open button, in the shape of the Bots thread's app card.
// Collapsed it mounts nothing of the work, so a long chat never runs a workbench frame per card; Open raises the
// shared overlay (platform/ui/ViewModal), which draws the node or canvas live, and closing unmounts it.
import { useState } from "react";

import { ViewKindIcon, ViewModal, viewSubtitle } from "@platform/ui/ViewModal";

import type { ViewCardRef } from "../specialists/view-card";
import { topDocumentBody } from "./topContainer";

export function ViewCard({ view }: { view: ViewCardRef }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="c-viewcard" data-view={view.path}>
      <span className="c-viewcard-ico" aria-hidden="true">
        <ViewKindIcon kind={view.kind} />
      </span>
      <div className="c-viewcard-txt">
        <b>{view.title}</b>
        <small title={view.path}>{[view.note, viewSubtitle(view)].filter(Boolean).join(" · ")}</small>
      </div>
      <button type="button" className="c-viewcard-open" onClick={() => setOpen(true)}>
        Open
      </button>
      {open ? <ViewModal view={view} onClose={() => setOpen(false)} getContainer={topDocumentBody} /> : null}
    </div>
  );
}

export default ViewCard;
