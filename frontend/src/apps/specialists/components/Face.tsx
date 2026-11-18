// A specialist's face: a disc in its colour with its initial. No emoji, no
// picture to fail: the face is a colour.
// CONTRACT: `face` (specialist.json, PLAN B.1) has no fixed format in the plan.
// A `#rrggbb` / `#rgb` value is used as the disc's colour; anything else (or
// empty) picks a stable colour from the id.
const PALETTE = ["#1f8f7e", "#3b6fd8", "#9a5bd6", "#c4683b", "#2f8f3f", "#b8456b", "#6b7a1f", "#2f7f9f"];

export function faceColor(face: string | undefined, id: string): string {
  if (face && /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.test(face.trim())) return face.trim();
  let h = 0;
  for (const ch of id || "x") h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
}

export function Face({ id, name, face, size = 40 }: { id: string; name: string; face?: string; size?: number }) {
  const initial = (name.trim()[0] || "?").toUpperCase();
  return (
    <span
      className="spx-face"
      aria-hidden="true"
      style={{ width: size, height: size, background: faceColor(face, id), fontSize: Math.round(size * 0.42) }}
    >
      {initial}
    </span>
  );
}
