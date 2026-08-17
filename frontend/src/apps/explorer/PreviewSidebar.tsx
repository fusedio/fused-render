// The file preview's right-hand SIDEBAR: the companion modes (`claude`, `git`
// — lib/mode-visibility's SIDEBAR_MODES) rendered BESIDE the content
// pane instead of in place of it.
//
// Why it exists: those are not other ways of looking at a file, they are things
// you do while looking at it. As ordinary `_mode` entries they were mutually
// exclusive with the view they are about — asking Claude about a .png meant
// giving the .png up — and the chat template answered that by framing its own
// copy of the preview in its own left half, i.e. a second, differently-run
// preview of the same file nested inside the first one's window.
//
// `git` is the one this column does not get from the file: a working tree belongs
// to the FOLDER, so the entry is borrowed from the file's parent directory and
// aimed there (apps/explorer/lib/dir-mode.ts). Nothing in here knows that — the
// entry list and the `src` both arrive as props — but it is why an entry can be
// `pending` for a reason the file's own gate verdicts do not explain.
//
// Owned by TemplatePreview (Preview.tsx): the mode partition, the `_side` URL
// param and the iframe URL shape are all its, so this component is the split's
// right-hand column and the drag that sizes it, and nothing else.
//
// WHERE IT SITS is the layout's whole point. The split is PAGE-LEVEL: the left
// column is the crumb bar AND the content under it, the right column is this, and
// the divider runs the full height of the window between them. So the crumb bar
// ends AT the divider, and this column's header row is the top of the window on
// its side — the two strips reading as one bar split by a seam, exactly as the
// listing and its preview pane do over a folder.
//
// It got there the wrong way first: rendered inside `.preview-body`, i.e. UNDER
// the crumb bar, which then spanned the whole window and left this column's header
// a bar-height below the left column's. Page-level means being a sibling of the
// entire left column, and that column belongs to StatView (shell/App.tsx) — hence
// the portal (preview-side-slot.ts). Rendered as a FRAGMENT of two flex items (the
// divider and the column) into a `display: contents` slot, so both end up direct
// flex children of the split container.
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { modeTitle } from "@platform/lib/mode-name";
import { ModeMenu } from "@apps/explorer/BarMenu";
import { SideCloseButton } from "@apps/explorer/SideChrome";
import {
  publishPreviewSideSlot,
  retractPreviewSideSlot,
} from "@apps/explorer/preview-side-slot";
import {
  clampSideWidth,
  defaultSideWidth,
  openingSideWidth,
  MIN_W,
  CONTENT_MIN_W,
} from "@apps/explorer/lib/side-width";
import { getSideWidth, setSideWidth } from "@apps/explorer/lib/side-store";

// The split container's class, and the drag's frame of reference. Looked up from
// the divider with `closest` rather than handed down as a ref: the two live in
// different components now (StatView owns the container, this owns the handle),
// and a ref would have to be threaded through the portal to get here.
const SPLIT_SEL = ".stat-split";

// The page-level split's right-hand slot, rendered by StatView beside the left
// column. `display: contents` (explorer.css) so the portaled divider and column
// are flex children of the split itself, and so an EMPTY slot — every route, every
// folder, every file with no companion mode — contributes nothing at all.
export function PreviewSideSlot() {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (el) publishPreviewSideSlot(el);
    return () => retractPreviewSideSlot(el);
  }, []);
  return <div className="stat-side-slot" ref={ref} />;
}

// The width the column OPENS at, and its floors, live in lib/side-width — a
// share of the split container (30% normally, 50% on a small one) clamped into
// the two floors. A DRAGGED width lives in lib/side-store, a module variable for
// the life of the document: nothing is written to storage of any kind, so a drag
// holds across the shell's navigation (this component remounts per file) and a
// refresh gets the layout's answer again. That module's header argues the policy.

export interface SidebarEntry {
  mode: string;
  icon: ReactNode;
  // Condition.py gate not yet resolved (CT-12) — listed, not selectable.
  pending?: boolean;
  // This file cannot show the companion, and this is why (mode-visibility's
  // canned reasons). Listed, disabled, tooltipped — never selectable, and never
  // the `active` one.
  disabledReason?: string;
}

export default function PreviewSidebar({
  entries,
  active,
  src,
  onSelect,
  onClose,
}: {
  // The switcher's whole list: every companion, in SIDEBAR_MODES order, the ones
  // this file cannot show disabled and carrying their reason.
  entries: SidebarEntry[];
  // The one being shown — always one of the SELECTABLE entries, never a disabled
  // placeholder (Preview resolves `_side` against the short list; lib/preview-side).
  active: string;
  // Its /render URL, or null while its gate is still resolving.
  src: string | null;
  onSelect: (mode: string) => void;
  // Clears `_side`. The title bar's opener is hidden while this column is up
  // (SideChrome writes the split down), so this is the only way out of it.
  onClose: () => void;
}) {
  // A width the user DRAGGED earlier in this document leads (lib/side-store) — that
  // is what makes the divider hold still while you walk from file to file, since
  // this component remounts on every one of those hops. It leads, it does not win
  // outright: the layout effect below still measures and clamps it into THIS
  // container's floors before the first paint.
  //
  // Failing that, seeded from the VIEWPORT, because a state initialiser runs
  // before there is any layout to measure. It is a stand-in only — the layout
  // effect below replaces it with the container's own answer before the browser
  // paints, so this value reaches the screen only if the container cannot be
  // measured at all (detached, display:none), where the viewport is the honest
  // guess.
  const [width, setWidth] = useState(
    () =>
      getSideWidth() ??
      defaultSideWidth(typeof window === "undefined" ? 0 : window.innerWidth),
  );
  // The divider, and through it the split container (see SPLIT_SEL).
  const dividerRef = useRef<HTMLDivElement>(null);
  const splitEl = () => dividerRef.current?.closest<HTMLElement>(SPLIT_SEL) ?? null;

  // The real opening width, measured. useLayoutEffect and not useEffect: the refs
  // are attached and the container laid out by now, and React flushes this before
  // paint, so the seeded viewport width never reaches the screen and the column
  // does not open at one width and jump to another.
  //
  // A STORED WIDTH GOES THROUGH THIS TOO, and that is the point of `openingSideWidth`
  // rather than a `getSideWidth()` early return: a width dragged in one window is not
  // necessarily a width that fits in this one, and skipping the measurement meant the
  // column painted at the raw stored pixels and only met the floors in the resize
  // effect below, which runs AFTER paint. (Drag wide, navigate to a folder so this
  // unmounts, shrink the window, open a file.) Null answers "container unmeasurable",
  // where the seeded viewport guess is the honest value and is left alone.
  //
  // Mount-only, and deliberately not re-run on container resize: the observer below
  // never widens past the standing choice, so a window the user widens keeps the
  // width they are looking at instead of springing back to the share.
  useLayoutEffect(() => {
    const w = splitEl()?.getBoundingClientRect().width ?? 0;
    const opening = openingSideWidth(getSideWidth(), w);
    if (opening !== null) setWidth(opening);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // A window narrower than the two floors together must not leave the content
  // column at nothing: clamp on every container resize, not only on drag.
  //
  // The clamp never WRITES the module store — narrowing what is on screen is not
  // the user choosing a narrower column — but it does READ it, which is what lets
  // the column go back to the dragged width when the room returns. The whole rule,
  // and why it is both directions, is `clampSideWidth` (lib/side-width).
  useEffect(() => {
    const el = splitEl();
    if (!el) return;
    const clamp = () => {
      const containerW = el.getBoundingClientRect().width;
      setWidth((w) => clampSideWidth(w, getSideWidth(), containerW));
    };
    clamp();
    const ro = new ResizeObserver(clamp);
    ro.observe(el);
    return () => ro.disconnect();
    // Mount-only: the container is the page-level split, which outlives this
    // component (StatView owns it), so there is nothing here to re-resolve.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Pointer capture, like the listing's divider (listing/pane.ts): without it
  // the drag dies the moment the cursor crosses into either iframe, which is
  // most of what is on either side of this handle.
  const onDividerPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    const divider = e.currentTarget;
    divider.setPointerCapture(e.pointerId);
    divider.classList.add("dragging");
    // What the drag has moved the divider to, if it moved at all. Recorded to the
    // module store on POINTER-UP and not on every move: a COMPLETED drag is the
    // choice (lib/side-store), and a click on the divider that moves nothing must
    // not turn the measured default into a remembered number.
    let dragged: number | null = null;
    const onMove = (ev: PointerEvent) => {
      const rect = splitEl()?.getBoundingClientRect();
      if (!rect) return;
      const max = rect.width - CONTENT_MIN_W;
      if (max < MIN_W) return; // container too narrow to express a split
      const next = Math.min(max, Math.max(MIN_W, rect.right - ev.clientX));
      dragged = next;
      setWidth((w) => (w === next ? w : next));
    };
    const onUp = () => {
      if (dragged !== null) setSideWidth(dragged);
      divider.classList.remove("dragging");
      divider.removeEventListener("pointermove", onMove);
      divider.removeEventListener("pointerup", onUp);
      divider.removeEventListener("pointercancel", onUp);
    };
    divider.addEventListener("pointermove", onMove);
    divider.addEventListener("pointerup", onUp);
    divider.addEventListener("pointercancel", onUp);
  };

  return (
    <>
      <div
        className="preview-side-divider"
        ref={dividerRef}
        onPointerDown={onDividerPointerDown}
        role="separator"
        aria-orientation="vertical"
      />
      <aside
        className="preview-side"
        style={{ flexBasis: width }}
        aria-label={modeTitle(active) + " sidebar"}
      >
        <div className="preview-side-header">
          {/* Leftmost, on the seam (SideChrome): the way out of this column, and
              while it is up, the only one — the title bar's opener is not
              rendered. The listing pane's header opens with the same button. */}
          <SideCloseButton what={modeTitle(active)} onClick={onClose} />
          {/* The shared mode control, over the companions, at the strip's far end
              (the tail's auto margin packs it there — .side-header-tail, the same
              wrapper the listing pane's header uses).

              ALWAYS a menu now. This used to fall back to a flat, unclickable
              label whenever `entries` was down to one, because BarMenu hides a
              one-row menu and the strip would otherwise have been a lone chevron
              over an unlabelled document. The list no longer shrinks: `entries`
              is all three companions on every file, the unavailable ones disabled
              and carrying the reason (lib/preview-side's `menu`), so there is
              always a menu to draw and always more in it than the mode you are
              looking at. */}
          <div className="side-header-tail">
            <ModeMenu entries={entries} active={active} onSelect={onSelect} />
          </div>
        </div>
        {src === null ? (
          /* The chosen sidebar mode is gate-pending (CT-12): hold the column
             rather than frame a template whose condition may deny this file. */
          <div className="preview-resolving">
            <span className="mode-icon-spinner" />
            Checking if this view applies…
          </div>
        ) : (
          /* Keyed on the mode, so a switch replaces the document outright. No
             held-frame cross-fade here (unlike the content pane): the sidebar is
             a narrow column of chrome-heavy tools, and the two of them look
             nothing alike — there is no illusion of continuity to protect. */
          <iframe
            key={active}
            className="preview-side-frame"
            src={src}
            title={modeTitle(active)}
          />
        )}
      </aside>
    </>
  );
}
