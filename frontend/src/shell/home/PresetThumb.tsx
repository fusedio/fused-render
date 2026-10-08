// A preset's shape at thumbnail size: its tiles as filled blocks on the same
// 8-unit grid. The build box and the big tiles take the accent tint.
import { memo, useMemo, type CSSProperties } from "react";
import { GRID_COLS, dimsOf, presetLayout, rowsUsed, type PresetId } from "./layout";

export const PresetThumb = memo(function PresetThumb({ id }: { id: PresetId }) {
  const { tiles, rows } = useMemo(() => {
    const l = presetLayout(id);
    return {
      rows: rowsUsed(l.widgets),
      tiles: l.widgets.map((w) => {
        const d = dimsOf(w);
        return { id: w.id, x: w.x, y: w.y, ...d, hero: w.source === "build" || (d.cols >= 6 && d.rows >= 4) };
      }),
    };
  }, [id]);
  return (
    <span
      className="hw-preset-thumb"
      aria-hidden="true"
      style={{ gridTemplateColumns: `repeat(${GRID_COLS}, 1fr)`, gridTemplateRows: `repeat(${rows}, 1fr)` } as CSSProperties}
    >
      {tiles.map((t) => (
        <i
          key={t.id}
          className={t.hero ? "is-hero" : undefined}
          style={{ gridColumn: `${t.x + 1} / span ${t.cols}`, gridRow: `${t.y + 1} / span ${t.rows}` }}
        />
      ))}
    </span>
  );
});
