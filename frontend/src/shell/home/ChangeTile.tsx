// The "Change" popover: what a tile (or an empty slot) should show. Every source
// is listed; the ones that cannot fill this footprint are disabled with the
// reason underneath. Folder and page picks need a choice, so they hand off to
// the add sheet (onPick) instead of swapping directly.
import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { createPortal } from "react-dom";
import { useDismissOnOutside } from "@platform/lib/dismissOnOutside";
import { SourceIcon } from "./AddWidgetPanel";
import { FormatPicks, ShowChips, SizeChips, SortChips } from "./Pickers";
import { FIXED_ROWS, MAX_WIDGETS, SOURCES, allowedSizes, dimsOf, rectOf, sizesFor, sourceFits, type TileTarget, type WidgetSource } from "./layout";
import { placePopover, type Box, type Placement } from "./popoverPlace";
import type { HomeLayoutApi } from "./useHomeLayout";

const NEEDS_CHOICE = new Set<WidgetSource>(["folder", "app"]);
const SOURCE_KEYS = Object.keys(SOURCES) as WidgetSource[];

/** The visible content area around `anchor`: the nearest scrolling or clipping
    ancestor above the tile grid (a tile clips its own children, so it is skipped),
    cut to the window. */
function contentBounds(anchor: HTMLElement): Box {
  const win: Box = { left: 0, top: 0, right: window.innerWidth, bottom: window.innerHeight };
  let el = (anchor.closest(".hw-grid") ?? anchor).parentElement;
  while (el && el !== document.body) {
    const o = getComputedStyle(el).overflowY;
    if (o === "auto" || o === "scroll" || o === "hidden" || o === "overlay") {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) {
        return { left: Math.max(win.left, r.left), top: Math.max(win.top, r.top), right: Math.min(win.right, r.right), bottom: Math.min(win.bottom, r.bottom) };
      }
    }
    el = el.parentElement;
  }
  return win;
}

/** The popover is rendered in a portal on the body, positioned fixed against
    its anchor (its parent in the React tree's DOM), so no tile's overflow can
    clip it. The anchor of its anchor element: a press inside the anchor is
    the anchor's own toggle, so only presses outside it dismiss. */
export function ChangeTile({
  api,
  target,
  title,
  anchorAlign,
  onClose,
  onPick,
}: {
  api: HomeLayoutApi;
  target: TileTarget;
  title: string;
  /** Which edge of the anchor the card hangs from; absent = whichever fits on screen. */
  anchorAlign?: "left" | "right";
  onClose: () => void;
  /** A source that needs a further choice (folder, page). */
  onPick: (source: WidgetSource) => void;
}) {
  const root = useRef<HTMLDivElement>(null);
  const marker = useRef<HTMLSpanElement>(null);
  const [pos, setPos] = useState<Placement | null>(null);
  const { layout } = api;
  // Read the live tile so a format pick shows as chosen while the card stays open.
  const widget = target.kind === "swap" ? (layout.widgets.find((w) => w.id === target.widget.id) ?? target.widget) : null;
  const rect = target.kind === "swap" ? rectOf(widget ?? target.widget) : target.rect;

  // The anchor and the portaled card both count as "inside" for outside-click
  // dismissal, so the anchor button's own click is not a dismissal.
  const hostRef = useRef<HTMLElement | null>(null);
  if (!hostRef.current) {
    hostRef.current = {
      contains: (n: Node | null) => !!(marker.current?.parentElement?.contains(n) || root.current?.contains(n)),
    } as unknown as HTMLElement;
  }
  useDismissOnOutside(hostRef, true, onClose);

  // Latest onClose, so the Esc listener subscribes once. The hook does not
  // stop propagation or restore focus, so Esc stays local.
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const key = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        closeRef.current();
        root.current?.parentElement?.querySelector<HTMLElement>("button")?.focus();
      }
    };
    document.addEventListener("keydown", key, true);
    return () => document.removeEventListener("keydown", key, true);
  }, []);

  // Hang the card from the anchor, keep it inside the visible content area, flip
  // above when it does not fit below, and cap its height (it scrolls inside).
  const place = () => {
    const anchor = marker.current?.parentElement;
    const pop = root.current;
    if (!anchor || !pop) return;
    const next = placePopover({
      anchor: anchor.getBoundingClientRect(),
      bounds: contentBounds(anchor),
      size: { width: 440, height: pop.scrollHeight + (pop.offsetHeight - pop.clientHeight) },
      alignLeft: anchorAlign === "left",
    });
    setPos((p) => (p && p.left === next.left && p.top === next.top && p.maxHeight === next.maxHeight && p.width === next.width ? p : next));
  };
  useLayoutEffect(place);
  useEffect(() => {
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  });

  const pick = (s: WidgetSource) => {
    if (NEEDS_CHOICE.has(s)) {
      onPick(s);
      return;
    }
    if (widget) api.swap(widget.id, s);
    else api.fill(rect, s);
    onClose();
  };

  const spec = widget ? SOURCES[widget.source] : null;
  const style: CSSProperties = pos
    ? { left: pos.left, top: pos.top, width: pos.width, maxHeight: pos.maxHeight }
    : { left: 0, top: 0, visibility: "hidden" };
  return (
    <>
    <span ref={marker} hidden />
    {createPortal(
    <div ref={root} className="hw-pop" style={style} role="dialog" aria-label={`Change ${title}`}>
      <div className="hw-label">Show in this tile</div>
      <div className="hw-po-list">
        {SOURCE_KEYS.map((s) => {
          const fit = sourceFits(layout, rect, s, widget?.id);
          const current = widget?.source === s;
          const homeFull = target.kind === "fill" && layout.widgets.length >= MAX_WIDGETS;
          const disabled = !fit.ok || homeFull;
          const why = homeFull ? "Home is full" : fit.ok ? null : fit.reason;
          const choice = NEEDS_CHOICE.has(s);
          return (
            <button
              key={s}
              type="button"
              className={"hw-po" + (current ? " is-on" : "")}
              disabled={disabled}
              aria-current={current ? "true" : undefined}
              onClick={() => (current && !choice ? onClose() : pick(s))}
            >
              <SourceIcon source={s} />
              <span className="hw-po-text">
                <span className="hw-po-name">
                  {SOURCES[s].label}
                  {choice ? "…" : ""}
                </span>
                {why ? <span className="hw-po-why">{why}</span> : null}
              </span>
              {current ? <span className="hw-po-check" aria-hidden="true">✓</span> : null}
            </button>
          );
        })}
      </div>
      {widget && spec && (spec.formats.length > 1 || widget.source === "apps" || spec.sizes.length > 1) ? (
        <>
          <div className="hw-pop-divider" />
          {spec.formats.length > 1 ? (
            <>
              <div className="hw-label">Show as</div>
              <FormatPicks source={widget.source} formats={spec.formats} value={widget.format} rows={dimsOf(widget).rows} onChange={(f) => api.reformat(widget.id, f)} />
            </>
          ) : null}
          {spec.sizes.length > 1 && !FIXED_ROWS[widget.source] ? (
            <>
              <div className="hw-label">Size</div>
              <SizeChips
                source={widget.source}
                sizes={sizesFor(widget.source, widget.format)}
                allowed={allowedSizes(layout, widget.id)}
                value={widget.cols === undefined ? widget.size : undefined}
                onChange={(s) => api.resize(widget.id, s)}
              />
            </>
          ) : null}
          {widget.source === "apps" ? (
            <>
              <div className="hw-label">Sort by</div>
              <SortChips value={widget.sort ?? "opened"} onChange={(s) => api.resort(widget.id, s)} />
            </>
          ) : null}
          {widget.source === "tasks" && widget.format !== "count" ? (
            <>
              <div className="hw-label">Show</div>
              <ShowChips value={widget.show ?? "open_done"} onChange={(s) => api.reshow(widget.id, s)} />
            </>
          ) : null}
        </>
      ) : null}
    </div>,
    document.body,
    )}
    </>
  );
}
