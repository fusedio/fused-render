// One canvas on the catalog page — a port of the hosted workbench home's
// recents card (application client: `CollectionCardComponent` as
// `home-page-home-recent.tsx` configures it), so the two gardens show the same
// card for the same canvas:
//
//   - a 10px-padded plate with a hairline border that brightens on hover, lifts
//     2px and casts a shadow;
//   - a 16:9 thumb: the uploaded preview, else one dark map tile per UDF in the
//     grid shape the count rounds up into, else "No UDFs present in the canvas";
//   - overlay actions, each fading in on hover: share link + favourite star top
//     right (the star stays up while starred), delete + open-in-new-tab bottom
//     right;
//   - a 14px weight-300 title (underscores shown as spaces) that middle-
//     truncates at rest and marquees back and forth on hover, with the UDF
//     count beside it on hover only;
//   - a spinner scrim while the canvas is being cloned to open.
//
// The whole card is a link (an anchor under the actions): a plain click opens
// in place, a modified click opens a new window — for a canvas not cloned yet
// the new-window path clones first, since its workspace page cannot open a
// canvas that has no folder.
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import previewTile from "@assets/canvas-preview-tile.png";
import { Loader2 } from "lucide-react";
import type { CanvasEntry } from "./api";

const TILE_CAP = 16;

// The gallery's grid shapes, keyed by the layout each count rounds up into: 5
// tiles use the 6 layout with an empty cell, 7 uses the 8, and so on. Copied
// from the client's `getGridTemplateByCount`.
type TileLayout = { gridTemplateColumns: string; gridTemplateRows: string };

const TILE_LAYOUTS: Record<number, TileLayout> = {
  1: { gridTemplateColumns: "1fr", gridTemplateRows: "1fr" },
  2: { gridTemplateColumns: "1fr 1fr", gridTemplateRows: "1fr" },
  3: { gridTemplateColumns: "1fr 1fr", gridTemplateRows: "1fr 1fr" },
  4: { gridTemplateColumns: "1fr 1fr", gridTemplateRows: "1fr 1fr" },
  6: { gridTemplateColumns: "1fr 1fr 1fr", gridTemplateRows: "1fr 1fr" },
  8: { gridTemplateColumns: "1fr 1fr 1fr 1fr", gridTemplateRows: "1fr 1fr" },
  9: { gridTemplateColumns: "1fr 1fr 1fr", gridTemplateRows: "1fr 1fr 1fr" },
  12: { gridTemplateColumns: "1fr 1fr 1fr 1fr", gridTemplateRows: "1fr 1fr 1fr" },
  15: { gridTemplateColumns: "1fr 1fr 1fr 1fr 1fr", gridTemplateRows: "1fr 1fr 1fr" },
  16: { gridTemplateColumns: "1fr 1fr 1fr 1fr", gridTemplateRows: "1fr 1fr 1fr 1fr" },
};

function tileLayout(count: number): TileLayout {
  const size = [1, 2, 3, 4, 6, 8, 9, 12, 15, 16].find((n) => n >= count) ?? 16;
  return TILE_LAYOUTS[size];
}

/** The workbench's display form of a canvas name (`formatCanvasNameForWorkbenchDisplay`). */
export function canvasDisplayName(name: string): string {
  return name.replace(/_/g, " ");
}

// Material / IcoMoon glyphs, path-for-path what the hosted card draws
// (react-icons MdLink, MdStar, MdStarBorder, MdDelete, ImNewTab).
const IconLink = () => (
  <svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor" aria-hidden>
    <path d="M3.9 12c0-1.71 1.39-3.1 3.1-3.1h4V7H7c-2.76 0-5 2.24-5 5s2.24 5 5 5h4v-1.9H7c-1.71 0-3.1-1.39-3.1-3.1zM8 13h8v-2H8v2zm9-6h-4v1.9h4c1.71 0 3.1 1.39 3.1 3.1s-1.39 3.1-3.1 3.1h-4V17h4c2.76 0 5-2.24 5-5s-2.24-5-5-5z" />
  </svg>
);
const IconStar = () => (
  <svg viewBox="0 0 24 24" width="1em" height="1em" fill="currentColor" aria-hidden>
    <path d="M12 17.27 18.18 21l-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21z" />
  </svg>
);
const IconStarBorder = () => (
  <svg viewBox="0 0 24 24" width="1em" height="1em" fill="currentColor" aria-hidden>
    <path d="m22 9.24-7.19-.62L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21 12 17.27 18.18 21l-1.63-7.03L22 9.24zM12 15.4l-3.76 2.27 1-4.28-3.32-2.88 4.38-.38L12 6.1l1.71 4.04 4.38.38-3.32 2.88 1 4.28L12 15.4z" />
  </svg>
);
const IconDelete = () => (
  <svg viewBox="0 0 24 24" width="1em" height="1em" fill="currentColor" aria-hidden>
    <path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z" />
  </svg>
);
const IconNewTab = () => (
  <svg viewBox="0 0 16 16" width="12" height="12" fill="currentColor" aria-hidden>
    <path d="M3 1v12h12v-12h-12zM14 12h-10v-10h10v10zM2 14v-10.5l-1-1v12.5h12.5l-1-1h-10.5z" />
    <path d="M5.5 4l2.5 2.5-3 3 1.5 1.5 3-3 2.5 2.5v-6.5z" />
  </svg>
);

// Names this short never marquee (the client's MARQUEE_MAX_CHARS_WITHOUT_ANIMATION).
const MARQUEE_MIN_CHARS = 22;
const MARQUEE_PX_PER_SEC = 45;
const MARQUEE_PAUSE_SEC = 1;

let measureCtx: CanvasRenderingContext2D | null = null;

/** As much of `text` as fits in `width` px, cut from the MIDDLE (the client's
 *  MiddleTruncatedText): the start and the end of a canvas name are the parts
 *  that tell two copies apart. */
function middleTruncate(text: string, width: number, font: string): string {
  measureCtx ??= document.createElement("canvas").getContext("2d");
  if (!measureCtx || width <= 0) return text;
  measureCtx.font = font;
  const fits = (s: string) => measureCtx!.measureText(s).width <= width;
  if (fits(text)) return text;
  let lo = 0;
  let hi = text.length;
  while (lo < hi) {
    const keep = Math.ceil((lo + hi) / 2);
    const head = Math.ceil(keep / 2);
    const cand = text.slice(0, head) + "…" + text.slice(text.length - (keep - head));
    if (fits(cand)) lo = keep;
    else hi = keep - 1;
  }
  const head = Math.ceil(lo / 2);
  return text.slice(0, head) + "…" + text.slice(text.length - (lo - head));
}

function prefersReducedMotion(): boolean {
  return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
}

/** The title line: middle-truncated at rest; on hover, a name that overflows
 *  (and is long enough to be worth it) slides to its end and back at a
 *  constant speed, pausing a second per round — the client's
 *  BackAndForthMarqueeText. */
function MarqueeTitle({ text, active }: { text: string; active: boolean }) {
  const boxRef = useRef<HTMLSpanElement>(null);
  const runRef = useRef<HTMLSpanElement>(null);
  const [shown, setShown] = useState(text);
  const [overflow, setOverflow] = useState(0);

  const measure = useCallback(() => {
    const box = boxRef.current;
    const run = runRef.current;
    if (!box || !run) return;
    const full = run.scrollWidth;
    const width = box.clientWidth;
    setOverflow(Math.max(0, Math.ceil(full - width)));
    const cs = getComputedStyle(box);
    setShown(middleTruncate(text, width, `${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`));
  }, [text]);

  useLayoutEffect(() => {
    measure();
    const box = boxRef.current;
    if (!box) return;
    const ro = new ResizeObserver(measure);
    ro.observe(box);
    return () => ro.disconnect();
  }, [measure]);

  const marquee =
    active && overflow > 0 && text.length >= MARQUEE_MIN_CHARS && !prefersReducedMotion();

  useEffect(() => {
    const run = runRef.current;
    if (!marquee || !run) return;
    const leg = overflow / MARQUEE_PX_PER_SEC;
    const cycle = 2 * leg + MARQUEE_PAUSE_SEC;
    const anim = run.animate(
      [
        { transform: "translateX(0)", offset: 0 },
        { transform: `translateX(-${overflow}px)`, offset: leg / cycle },
        { transform: "translateX(0)", offset: (2 * leg) / cycle },
        { transform: "translateX(0)", offset: 1 },
      ],
      { duration: cycle * 1000, iterations: Infinity, easing: "linear" },
    );
    return () => anim.cancel();
  }, [marquee, overflow]);

  return (
    <span ref={boxRef} className="canvas-card-name" title={overflow > 0 && !marquee ? text : undefined}>
      {/* The full run is always mounted (it is what overflow is measured on);
          at rest it hides behind the middle-truncated copy. */}
      <span ref={runRef} className={"canvas-card-name-run" + (marquee ? "" : " is-measure")}>
        {text}
      </span>
      {!marquee && <span className="canvas-card-name-fit">{shown}</span>}
    </span>
  );
}

export interface CanvasCardProps {
  canvas: CanvasEntry;
  href: string;
  /** Preview image URL (listing's public one or the signed one), if any. */
  thumb: string | null;
  /** This canvas is being cloned/opened: spinner scrim, clicks ignored. */
  loading: boolean;
  /** Another card (or the create form) is busy: clicks ignored. */
  disabled: boolean;
  /** Favourite state; undefined hides the star (no id, or favourites unknown). */
  isFavourite: boolean | undefined;
  /** `<workbench origin>/canvas/<token>`, or null when there is no share token. */
  shareUrl: string | null;
  onOpen: () => void;
  onOpenInNewTab: () => void;
  onDelete: () => void;
  onToggleFavourite: () => void;
}

export function CanvasCard({
  canvas,
  href,
  thumb,
  loading,
  disabled,
  isFavourite,
  shareUrl,
  onOpen,
  onOpenInNewTab,
  onDelete,
  onToggleFavourite,
}: CanvasCardProps) {
  const [hovered, setHovered] = useState(false);
  const [brokenThumb, setBrokenThumb] = useState<string | null>(null);
  const title = canvasDisplayName(canvas.name);
  // The clone's own *.py count wins when we have one (it sees local edits not
  // pushed yet); otherwise the listing's count. Null only on the bare-name CLI
  // fallback — that card still gets one tile, standing for "a canvas".
  const nUdfs = canvas.n_udfs ?? canvas.n_code_udfs ?? null;
  const udfCount = nUdfs ?? 0;
  const tiles = nUdfs === null ? 1 : Math.min(nUdfs, TILE_CAP);
  const inert = loading || disabled;

  const stop = (fn: () => void) => (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (!inert) fn();
  };

  return (
    <div
      className={"canvas-card" + (hovered ? " is-hovered" : "") + (inert ? " is-inert" : "")}
      onMouseOver={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      <a
        className="canvas-card-link"
        href={href}
        aria-label={`Open ${title}`}
        onClick={(e) => {
          if (inert) {
            e.preventDefault();
            return;
          }
          const modified = e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0;
          if (modified) {
            // A cloned canvas's workspace opens as-is: let the browser (or
            // the native shell's new-window policy) take the link. An
            // uncloned one has no folder for that page to open yet.
            if (!canvas.cloned) {
              e.preventDefault();
              onOpenInNewTab();
            }
            return;
          }
          e.preventDefault();
          onOpen();
        }}
        // Middle click never reaches onClick; same rule as a modified click.
        onAuxClick={(e) => {
          if (e.button !== 1) return;
          if (inert || !canvas.cloned) e.preventDefault();
          if (!inert && !canvas.cloned) onOpenInNewTab();
        }}
      />
      <div className="canvas-card-thumb">
        {thumb && thumb !== brokenThumb ? (
          <img
            className="canvas-card-img"
            src={thumb}
            alt=""
            loading="lazy"
            onError={() => setBrokenThumb(thumb)}
          />
        ) : tiles > 0 ? (
          <span className="canvas-card-tiles" style={tileLayout(tiles)}>
            {Array.from({ length: tiles }, (_, i) => (
              <img key={i} className="canvas-card-tile" src={previewTile} alt="" />
            ))}
          </span>
        ) : (
          <span className="canvas-card-noshot">No UDFs present in the canvas</span>
        )}
        {(isFavourite !== undefined || shareUrl) && (
          <span className="canvas-card-actions is-top">
            {shareUrl && (
              <button
                type="button"
                className="canvas-card-action is-hover-only is-secondary"
                title="Open share link"
                aria-label="Open share link"
                onClick={stop(() => window.open(shareUrl, "_blank", "noopener,noreferrer"))}
              >
                <IconLink />
              </button>
            )}
            {isFavourite !== undefined && (
              <button
                type="button"
                className={
                  "canvas-card-action is-secondary" +
                  (isFavourite ? " is-starred" : " is-hover-only")
                }
                title={isFavourite ? "Remove from favourites" : "Add to favourites"}
                aria-label={isFavourite ? "Remove from favourites" : "Add to favourites"}
                aria-pressed={isFavourite}
                onClick={stop(onToggleFavourite)}
              >
                {isFavourite ? <IconStar /> : <IconStarBorder />}
              </button>
            )}
          </span>
        )}
        <span className="canvas-card-actions is-bottom">
          <button
            type="button"
            className="canvas-card-action is-hover-only"
            title="Delete canvas"
            aria-label="Delete canvas"
            onClick={stop(onDelete)}
          >
            <IconDelete />
          </button>
          <button
            type="button"
            className="canvas-card-action is-hover-only"
            title="Open canvas in new tab"
            aria-label="Open canvas in new tab"
            onClick={stop(onOpenInNewTab)}
          >
            <IconNewTab />
          </button>
        </span>
      </div>
      <div className="canvas-card-body">
        <MarqueeTitle text={title} active={hovered} />
        {hovered && (
          <span className="canvas-card-count">
            {udfCount} UDF{udfCount === 1 ? "" : "s"}
          </span>
        )}
      </div>
      {loading && (
        <div className="canvas-card-scrim" aria-busy="true" aria-label="Opening canvas">
          <Loader2 className="canvas-card-spinner" aria-hidden />
        </div>
      )}
    </div>
  );
}
