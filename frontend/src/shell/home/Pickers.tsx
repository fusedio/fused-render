// The two pickers the edit popover and the add sheet share: size chips (layout
// glyph + label) and format picks (scaled preview thumbnail + caption).
import { FormatPreview, previewKind } from "./FormatPreview";
import { FORMAT_LABELS, SIZE_LABELS, type WidgetFormat, type WidgetSize, type WidgetSource } from "./layout";
import { SizeGlyph } from "./SizeGlyph";

export function SizeChips({
  sizes,
  value,
  onChange,
  label = "Size",
}: {
  sizes: WidgetSize[];
  value: WidgetSize;
  onChange: (s: WidgetSize) => void;
  label?: string;
}) {
  return (
    <div className="hw-chips" role="radiogroup" aria-label={label}>
      {sizes.map((s) => (
        <button
          key={s}
          type="button"
          role="radio"
          aria-checked={s === value}
          className={"hw-sizechip" + (s === value ? " is-on" : "")}
          onClick={() => onChange(s)}
        >
          <SizeGlyph size={s} scale={0.7} />
          {SIZE_LABELS[s]}
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
}: {
  source: WidgetSource;
  formats: WidgetFormat[];
  value: WidgetFormat;
  onChange: (f: WidgetFormat) => void;
}) {
  return (
    <div className="hw-picks" role="radiogroup" aria-label="Show as">
      {formats.map((f) => (
        <button
          key={f}
          type="button"
          role="radio"
          aria-checked={f === value}
          className={"hw-pick" + (f === value ? " is-on" : "")}
          onClick={() => onChange(f)}
        >
          <span className="hw-thumb">
            <span className="hw-thumb-in" style={{ zoom: thumbZoom(source, f) }}>
              <FormatPreview source={source} format={f} />
            </span>
          </span>
          <span className="hw-pick-cap">{FORMAT_LABELS[f]}</span>
        </button>
      ))}
    </div>
  );
}
