// A specialist's face. `face` (specialist.json) is either a string or the bots'
// face object `{shape, color, icon}` (the shipped Canvas seed: a teal hexagon).
// A face with a known shape is drawn as that shape in its colour with two eyes,
// the way the Bots page draws a bot (apps/bots/lib/face.ts — the paths are
// copied here because an app may not import another app). Anything else is a
// disc in its colour with the name's initial(s): the fallback.
import type { SpecialistFace } from "../../../platform/lib/specialists-api";

const PALETTE = ["#1f8f7e", "#3b6fd8", "#9a5bd6", "#c4683b", "#2f8f3f", "#b8456b", "#6b7a1f", "#2f7f9f"];

/** The bots' face shapes in a 100-unit box (apps/bots/lib/face.ts FACE_SHAPES). */
export const FACE_SHAPES: Record<string, string> = {
  circle: "M50 14 C70 14 86 30 86 50 C86 70 70 86 50 86 C30 86 14 70 14 50 C14 30 30 14 50 14 Z",
  oval: "M42 14 C62 6 84 18 88 40 C91 60 84 86 62 90 C42 94 18 82 12 60 C7 40 22 22 42 14 Z",
  square:
    "M36 16 C46 14 60 14 70 16 C80 18 86 24 85 36 C84 50 84 62 86 74 C87 84 80 88 70 88 C56 88 40 90 26 86 C16 83 12 76 14 66 C16 54 15 42 16 32 C17 22 26 18 36 16 Z",
  pill: "M30 28 L70 28 C82 28 90 38 90 50 C90 62 82 72 70 72 L30 72 C18 72 10 62 10 50 C10 38 18 28 30 28 Z",
  triangle: "M44 18 C48 12 52 12 56 18 L86 70 C90 78 86 84 78 84 L22 84 C14 84 10 78 14 70 Z",
  hexagon:
    "M44 12 C48 10 52 10 56 12 L82 27 C86 29 88 32 88 36 L88 64 C88 68 86 71 82 73 L56 88 C52 90 48 90 44 88 L18 73 C14 71 12 68 12 64 L12 36 C12 32 14 29 18 27 Z",
  cloud: "M30 84 C16 84 10 74 12 64 C6 56 10 44 22 42 C22 28 34 20 46 26 C54 14 74 16 78 32 C92 34 94 52 84 58 C90 70 82 84 68 84 Z",
  drop: "M50 10 C54 22 84 44 84 62 C84 82 68 92 50 92 C32 92 16 82 16 62 C16 44 46 22 50 10 Z",
};

export function faceColor(face: SpecialistFace | undefined, id: string): string {
  const raw = typeof face === "string" ? face : face && typeof face === "object" ? face.color : "";
  const c = typeof raw === "string" ? raw.trim() : "";
  if (/^#([0-9a-f]{3}|[0-9a-f]{6})$/i.test(c)) return c;
  let h = 0;
  for (const ch of id || "x") h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
}

/** The face's shape when it names one we can draw, else null (the letter fallback). */
export function faceShape(face: SpecialistFace | undefined): string | null {
  const s = face && typeof face === "object" ? face.shape : "";
  return s && Object.prototype.hasOwnProperty.call(FACE_SHAPES, s) ? s : null;
}

/** The face as an SVG document (for a CSS background: the chat's reply avatar). */
export function faceSvg(face: SpecialistFace | undefined, id: string, letters: string): string {
  const color = faceColor(face, id);
  const shape = faceShape(face);
  const inner = shape
    ? `<path d="${FACE_SHAPES[shape]}" fill="${color}"/>` +
      `<ellipse cx="37" cy="31" rx="9" ry="4.5" fill="#fff" opacity=".28" transform="rotate(-28 37 31)"/>` +
      `<rect x="41" y="43" width="5" height="12" rx="2.5" fill="#0f172a"/><rect x="54" y="43" width="5" height="12" rx="2.5" fill="#0f172a"/>`
    : `<circle cx="50" cy="50" r="46" fill="${color}"/>` +
      `<text x="50" y="50" dy=".35em" text-anchor="middle" font-family="system-ui, sans-serif" font-weight="600" font-size="${letters.length > 1 ? 36 : 44}" fill="#fff">${escapeXml(letters)}</text>`;
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">${inner}</svg>`;
}
const escapeXml = (s: string) => s.replace(/[<>&"']/g, (c) => `&#${c.charCodeAt(0)};`);

/** A `url("data:…")` for CSS. */
export function faceCssUrl(face: SpecialistFace | undefined, id: string, letters: string): string {
  return `url("data:image/svg+xml,${encodeURIComponent(faceSvg(face, id, letters))}")`;
}

/**
 * The fallback's letters: one, or two when another name starts with the same
 * letter ("QA1-Alpha" and "QA2-Beta" → "QA" / "QB"; "Canvas" and "Charts" →
 * "Ca" / "Ch").
 */
export function initialsFor(name: string, others: readonly string[] = []): string {
  const n = name.trim();
  const first = (n[0] || "?").toUpperCase();
  const clash = others.some((o) => o.trim() !== n && (o.trim()[0] || "").toUpperCase() === first);
  if (!clash) return first;
  const words = n.split(/[^\p{L}\p{N}]+/u).filter(Boolean);
  if (words.length > 1) return first + words[1][0].toUpperCase();
  return first + (n[1] || "").toLowerCase();
}

export function Face({
  id,
  name,
  face,
  size = 40,
  letters,
}: {
  id: string;
  name: string;
  face?: SpecialistFace;
  size?: number;
  /** The fallback's letters (initialsFor); default the first letter. */
  letters?: string;
}) {
  const color = faceColor(face, id);
  const shape = faceShape(face);
  if (shape) {
    return (
      <svg className="spx-face is-shape" aria-hidden="true" viewBox="0 0 100 100" width={size} height={size}>
        <path d={FACE_SHAPES[shape]} fill={color} />
        <ellipse cx="37" cy="31" rx="9" ry="4.5" fill="#fff" opacity=".28" transform="rotate(-28 37 31)" />
        <rect x="41" y="43" width="5" height="12" rx="2.5" fill="#0f172a" />
        <rect x="54" y="43" width="5" height="12" rx="2.5" fill="#0f172a" />
      </svg>
    );
  }
  const text = letters || (name.trim()[0] || "?").toUpperCase();
  return (
    <span
      className="spx-face"
      aria-hidden="true"
      style={{ width: size, height: size, background: color, fontSize: Math.round(size * (text.length > 1 ? 0.36 : 0.42)) }}
    >
      {text}
    </span>
  );
}
