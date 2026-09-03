// One gallery tile on the Canvases listing: a flat square card — thumb on
// top, name, the UDF count (or the clone hint), last-modified underneath.
//
// A canvas with no uploaded preview gets the hosted gallery's stand-in: one
// dark map tile per UDF, laid out in a grid, so the card still reads as a
// canvas of N things instead of an empty box. The tile is the workbench's own
// `preview_thumbnail_1.png` (fused-magic S3, main_marketing_website/), vendored
// into the bundle rather than hot-linked — this app runs locally and a card
// that needs the network to look right is a card that breaks offline.
import previewTile from "@assets/canvas-preview-tile.png";
import { cn } from "@platform/lib/utils";
import { Tiny } from "@platform/ui/flow/Typography";
import type { CanvasEntry } from "./api";

const TILE_CAP = 16;

// The gallery's grid shapes, keyed by the layout each count rounds up into: 5
// tiles use the 6 layout with an empty cell, 7 uses the 8, and so on. Copied
// from the client's `getGridTemplateByCount` so the two gardens match.
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
  15: {
    gridTemplateColumns: "1fr 1fr 1fr 1fr 1fr",
    gridTemplateRows: "1fr 1fr 1fr",
  },
  16: {
    gridTemplateColumns: "1fr 1fr 1fr 1fr",
    gridTemplateRows: "1fr 1fr 1fr 1fr",
  },
};

function tileLayout(count: number): TileLayout {
  const size = [1, 2, 3, 4, 6, 8, 9, 12, 15, 16].find((n) => n >= count) ?? 16;
  return TILE_LAYOUTS[size];
}

// Full locale date+time, seconds and four-digit year included — the same string
// the hosted workbench's gallery prints under a canvas name.
function formatModified(mtime: number): string {
  return new Date(mtime * 1000).toLocaleString();
}

export function CanvasCard({
  canvas,
  thumb,
  broken,
  onBroken,
  cloning,
  disabled,
  onOpen,
}: {
  canvas: CanvasEntry;
  /** Preview URL, or null when the card falls back to map tiles. */
  thumb: string | null;
  /** Preview URLs that failed to load (expired presigned URL, deleted asset). */
  broken: Set<string>;
  onBroken: (url: string) => void;
  cloning: boolean;
  disabled: boolean;
  onOpen: () => void;
}) {
  // Local clone mtime when we have one, else the control plane's
  // last_updated — the same expression the listing's sort orders by.
  const modified = canvas.mtime ?? canvas.updated_at;
  // The clone's own *.py count wins when we have one (it sees local edits the
  // workbench hasn't been pushed yet); otherwise the listing's count, which
  // exists for every canvas in the account.
  const nUdfs = canvas.n_udfs ?? canvas.n_code_udfs ?? null;
  // An account whose listing predates the count field (or came from the
  // bare-name CLI fallback) still gets a map rather than an empty box — one
  // tile, standing for "a canvas", not for a count.
  const tiles = nUdfs === null ? 1 : Math.min(nUdfs, TILE_CAP);
  const showThumb = thumb !== null && !broken.has(thumb);

  return (
    <button
      type="button"
      className={cn(
        "group/canvas flex flex-col overflow-hidden rounded-lg bg-card text-left text-sm text-card-foreground ring-1 ring-foreground/10 outline-none",
        "motion-safe:transition-shadow hover:ring-foreground/25 focus-visible:ring-[3px] focus-visible:ring-ring/50",
        "disabled:pointer-events-none disabled:opacity-50",
      )}
      onClick={onOpen}
      disabled={disabled}
    >
      <span className="relative flex aspect-video items-center justify-center overflow-hidden bg-muted">
        {showThumb ? (
          <img
            className="absolute inset-0 size-full object-cover"
            src={thumb}
            alt=""
            loading="lazy"
            onError={() => onBroken(thumb)}
          />
        ) : tiles > 0 ? (
          <span className="absolute inset-0 grid gap-px" style={tileLayout(tiles)}>
            {Array.from({ length: tiles }, (_, i) => (
              <img
                key={i}
                className="size-full min-h-0 min-w-0 object-cover"
                src={previewTile}
                alt=""
              />
            ))}
          </span>
        ) : (
          // Only a canvas with zero UDFs lands here — there is nothing to tile.
          <Tiny>No UDFs present in the canvas</Tiny>
        )}
      </span>
      <span className="flex flex-col gap-0.5 px-3 py-2">
        <span className="truncate font-medium" title={canvas.name}>
          {canvas.name}
        </span>
        <Tiny>
          {cloning
            ? "Cloning…"
            : nUdfs === null
              ? "Not cloned yet — click to clone & open"
              : `${nUdfs} UDF${nUdfs === 1 ? "" : "s"}${
                  // The count now exists before the clone does, but an
                  // uncloned card still needs to say what a click will do —
                  // it is the only affordance it has.
                  canvas.cloned ? "" : " · click to clone & open"
                }`}
        </Tiny>
        {modified !== null && <Tiny>Last modified: {formatModified(modified)}</Tiny>}
      </span>
    </button>
  );
}
