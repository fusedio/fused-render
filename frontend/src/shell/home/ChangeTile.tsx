// The "Change" popover: what a tile (or an empty slot) should show. Every source
// is listed; the ones that cannot fill this footprint are disabled with the
// reason underneath. Folder and page picks need a choice, so they hand off to
// the add sheet (onPick) instead of swapping directly.
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useDismissOnOutside } from "@platform/lib/dismissOnOutside";
import { SourceIcon } from "./AddWidgetPanel";
import { FormatPicks, ShowChips, SortChips } from "./Pickers";
import { MAX_WIDGETS, SOURCES, rectOf, sourceFits, type TileTarget, type WidgetSource } from "./layout";
import type { HomeLayoutApi } from "./useHomeLayout";

const NEEDS_CHOICE = new Set<WidgetSource>(["folder", "app"]);
const SOURCE_KEYS = Object.keys(SOURCES) as WidgetSource[];

/** The popover is a child of its anchor element: a press inside the anchor is
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
  const [alignLeft, setAlignLeft] = useState(anchorAlign === "left");
  const [up, setUp] = useState(false);
  const { layout } = api;
  // Read the live tile so a format pick shows as chosen while the card stays open.
  const widget = target.kind === "swap" ? (layout.widgets.find((w) => w.id === target.widget.id) ?? target.widget) : null;
  const rect = target.kind === "swap" ? rectOf(widget ?? target.widget) : target.rect;

  // The anchor (the popover's parent) counts as "inside" for outside-click
  // dismissal, so the anchor button's own click is not a dismissal.
  const anchorRef = useRef<HTMLElement | null>(null);
  useLayoutEffect(() => {
    anchorRef.current = root.current?.parentElement ?? null;
  }, []);
  useDismissOnOutside(anchorRef, true, onClose);

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

  // Right-aligned under the anchor; one near the left edge would push the
  // 440px card off-screen, so flip it to grow rightwards there.
  useLayoutEffect(() => {
    const anchor = root.current?.parentElement;
    if (anchorAlign) setAlignLeft(anchorAlign === "left");
    else if (anchor) setAlignLeft(anchor.getBoundingClientRect().right - 440 < 8);
    const pop = root.current;
    if (anchor && pop) {
      const b = anchor.getBoundingClientRect();
      const h = pop.offsetHeight;
      setUp(b.bottom + h > window.innerHeight - 40 && b.top > window.innerHeight - b.bottom);
    }
  }, [anchorAlign]);

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
  return (
    <div
      ref={root}
      className={"hw-pop" + (alignLeft ? " is-left" : "") + (up ? " is-up" : "")}
      role="dialog"
      aria-label={`Change ${title}`}
    >
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
      {widget && spec && (spec.formats.length > 1 || widget.source === "apps") ? (
        <>
          <div className="hw-pop-divider" />
          {spec.formats.length > 1 ? (
            <>
              <div className="hw-label">Show as</div>
              <FormatPicks source={widget.source} formats={spec.formats} value={widget.format} onChange={(f) => api.reformat(widget.id, f)} />
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
    </div>
  );
}
