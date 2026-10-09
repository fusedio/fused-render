// The pickers on the tile Change card: size chips (layout glyph + label) and
// format picks (scaled preview thumbnail + caption).
import { FormatPreview, previewKind } from "./FormatPreview";
import { APPS_SORTS, CELL, FORMAT_LABELS, SIZE_LABELS, dimsFor, TASKS_SHOWS, type AppsSort, type TasksShow, type WidgetFormat, type WidgetSize, type WidgetSource } from "./layout";
import { SizeGlyph } from "./SizeGlyph";

export function SortChips({ value, onChange }: { value: AppsSort; onChange: (s: AppsSort) => void }) {
  return (
    <div className="hw-chips" role="radiogroup" aria-label="Sort by">
      {APPS_SORTS.map((s) => (
        <button key={s.value} type="button" role="radio" aria-checked={s.value === value}
          className={"hw-sizechip" + (s.value === value ? " is-on" : "")} onClick={() => onChange(s.value)}>
          {s.label}
        </button>
      ))}
    </div>
  );
}

export function ShowChips({ value, onChange }: { value: TasksShow; onChange: (s: TasksShow) => void }) {
  return (
    <div className="hw-chips" role="radiogroup" aria-label="Show">
      {TASKS_SHOWS.map((s) => (
        <button key={s.value} type="button" role="radio" aria-checked={s.value === value}
          className={"hw-sizechip" + (s.value === value ? " is-on" : "")} onClick={() => onChange(s.value)}>
          {s.label}
        </button>
      ))}
    </div>
  );
}

const SIZE_ORDER: WidgetSize[] = ["1x1", "2x1", "1x2", "2x2", "4x1"];

/** Chips always read Small, Half, Large, Full row, whatever order a source lists
    its sizes in (the first entry is only the default). */
export function sortSizes(sizes: WidgetSize[]): WidgetSize[] {
  return SIZE_ORDER.filter((s) => sizes.includes(s));
}

/** "2×2" in whole cells; empty for a fixed-row source whose height is a fraction of a cell. */
export function cellDims(source: WidgetSource, size: WidgetSize): string {
  const d = dimsFor(source, size);
  return d.rows % CELL === 0 ? `${d.cols / CELL}\u00d7${d.rows / CELL}` : "";
}

export function SizeChips({
  sizes,
  value,
  onChange,
  allowed,
  source,
}: {
  sizes: WidgetSize[];
  value?: WidgetSize;
  onChange: (s: WidgetSize) => void;
  /** Sizes that currently fit; the rest render disabled. */
  allowed: WidgetSize[];
  /** Each chip shows its cell dimensions (Large 2×2). */
  source: WidgetSource;
}) {
  return (
    <div className="hw-chips" role="radiogroup" aria-label="Size">
      {sortSizes(sizes).map((s) => (
        <button
          key={s}
          type="button"
          role="radio"
          aria-checked={s === value}
          className={"hw-sizechip" + (s === value ? " is-on" : "")}
          disabled={!allowed.includes(s)}
          title={!allowed.includes(s) ? "No room here" : undefined}
          onClick={() => onChange(s)}
        >
          <SizeGlyph size={s} scale={0.7} />
          {SIZE_LABELS[s]}
          <span className="hw-sizechip-dims">{cellDims(source, s)}</span>
        </button>
      ))}
    </div>
  );
}

/** Natural widths differ per illustration, so each gets its own zoom. */
export function thumbZoom(source: WidgetSource, format: WidgetFormat): number {
  const k = previewKind(source, format);
  return k.startsWith("cards") ? 0.24 : k === "board" ? 0.27 : 0.32;
}

/** Large version for the add sheet's stage. */
export function stageZoom(source: WidgetSource, format: WidgetFormat): number {
  const k = previewKind(source, format);
  return k.startsWith("cards") ? 1 : k === "board" ? 1.2 : 1.4;
}

export function FormatPicks({
  source,
  formats,
  value,
  onChange,
  disabled,
}: {
  source: WidgetSource;
  formats: WidgetFormat[];
  value: WidgetFormat;
  onChange: (f: WidgetFormat) => void;
  /** True for a format that cannot be picked on this tile. */
  disabled?: (f: WidgetFormat) => boolean;
}) {
  return (
    <div className="hw-picks" role="radiogroup" aria-label="Show as">
      {formats.map((f) => {
        const off = f !== value && !!disabled?.(f);
        return (
          <button
            key={f}
            type="button"
            role="radio"
            aria-checked={f === value}
            className={"hw-pick" + (f === value ? " is-on" : "")}
            disabled={off}
            title={off ? "Needs a taller tile" : undefined}
            onClick={() => onChange(f)}
          >
            <span className="hw-thumb">
              <span className="hw-thumb-in" style={{ zoom: thumbZoom(source, f) }}>
                <FormatPreview source={source} format={f} />
              </span>
            </span>
            <span className="hw-pick-cap">{FORMAT_LABELS[f]}</span>
          </button>
        );
      })}
    </div>
  );
}
