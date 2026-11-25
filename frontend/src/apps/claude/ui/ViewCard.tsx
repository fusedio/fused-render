// The card an expert's `show` tool puts in its main chat (docs/experts.md §14), drawn in place of that
// tool's row.
//
// A whole canvas is one line with an Open button; Open raises the shared overlay (platform/ui/ViewModal).
//
// One node (a UDF, a widget, an HTML node, a note) is drawn IN the card, never in an overlay: a head line and
// one box of a fixed height. The box holds the node's live frame only when the card was born in a turn that is
// still running in front of the user. Any later drawing of the same card (the chat reopened, history loaded,
// the card scrolled far away and its frame dropped) holds a "Load live" prompt in the SAME box, so a long chat
// never runs a workbench frame per card and nothing moves when the prompt becomes the frame.
import { useEffect, useRef, useState } from "react";

import { embedUrlForFsPath, navigateUrl, urlForFsPath } from "@platform/lib/router";
import { ViewKindIcon, ViewModal, viewKindLabel, viewSubtitle } from "@platform/ui/ViewModal";

import type { ViewCardRef } from "../experts/view-card";
import { topDocumentBody } from "./topContainer";

// Which cards hold a live frame, by tool_use id, kept OUTSIDE the component: the transcript remounts a card
// when its turn settles (the row is re-keyed), and a card that was live a moment ago must come back live, not
// as the prompt. `spent` is every card that has had its one unasked load in this window; `held` is the cards
// loaded right now, each released a moment after it unmounts, so a remount in the same breath keeps the frame
// while a real leave-and-return (longer than HOLD_MS) finds the prompt.
const spent = new Set<string>();
const held = new Map<string, ReturnType<typeof setTimeout> | null>();
const HOLD_MS = 1500;
// How far past the viewport a live frame may sit before it is dropped back to the prompt.
const KEEP_MARGIN = "2000px";

export interface ViewCardProps {
  view: ViewCardRef;
  /** The tool_use id of the `show` call, "" when it had none. */
  id?: string;
  /** The card's turn is still running: this card is new, so its node loads without being asked. */
  live?: boolean;
}

export function ViewCard({ view, id = "", live = false }: ViewCardProps) {
  return view.kind === "canvas" ? <CanvasCard view={view} /> : <NodeCard view={view} id={id} live={live} />;
}

function Head({ view, children }: { view: ViewCardRef; children: React.ReactNode }) {
  return (
    <div className="c-viewcard-head">
      <span className="c-viewcard-ico" aria-hidden="true">
        <ViewKindIcon kind={view.kind} />
      </span>
      <div className="c-viewcard-txt">
        <b>{view.title}</b>
        <small title={view.path}>{[view.note, viewSubtitle(view)].filter(Boolean).join(" · ")}</small>
      </div>
      {children}
    </div>
  );
}

function CanvasCard({ view }: { view: ViewCardRef }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="c-viewcard" data-view={view.path}>
      <Head view={view}>
        <button type="button" className="c-viewcard-open" onClick={() => setOpen(true)}>
          Open
        </button>
      </Head>
      {open ? <ViewModal view={view} onClose={() => setOpen(false)} getContainer={topDocumentBody} /> : null}
    </div>
  );
}

function NodeCard({ view, id, live }: { view: ViewCardRef; id: string; live: boolean }) {
  const [loaded, setLoaded] = useState(() => (id ? held.has(id) || (live && !spent.has(id)) : live));
  const box = useRef<HTMLDivElement>(null);
  // Recorded in an effect, not in the initializer above: StrictMode calls an initializer twice.
  useEffect(() => {
    if (!loaded || !id) return;
    const pending = held.get(id);
    if (pending) clearTimeout(pending);
    held.set(id, null);
    spent.add(id);
    return () => {
      held.set(id, setTimeout(() => held.delete(id), HOLD_MS));
    };
  }, [loaded, id]);
  // A live frame scrolled far out of view goes back to the prompt; the box keeps its size.
  useEffect(() => {
    const el = box.current;
    if (!loaded || !el || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver(
      (entries) => {
        if (!entries[entries.length - 1]!.isIntersecting) setLoaded(false);
      },
      { rootMargin: KEEP_MARGIN },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [loaded]);
  const label = viewKindLabel(view.kind);
  return (
    <div className="c-viewcard c-viewcard-node" data-view={view.path}>
      <Head view={view}>
        <button type="button" className="c-viewcard-open" onClick={() => void navigateUrl(urlForFsPath(view.path))}>
          Open in explorer
        </button>
      </Head>
      <div className="c-viewcard-box" ref={box}>
        {loaded ? (
          <>
            <span className="c-viewcard-wait" aria-hidden="true">Loading…</span>
            <iframe className="c-viewcard-frame" src={embedUrlForFsPath(view.path)} title={`${label} ${view.title}`} />
          </>
        ) : (
          <button type="button" className="c-viewcard-load" onClick={() => setLoaded(true)}>
            <span className="c-viewcard-load-ico" aria-hidden="true">
              <ViewKindIcon kind={view.kind} size={22} />
            </span>
            <b>Load live</b>
            <small>This {label} is not running. Click to load it.</small>
          </button>
        )}
      </div>
    </div>
  );
}

export default ViewCard;
