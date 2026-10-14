// A bot's face, small: the same meta the desktop draws from (face.shape /
// face.color / face.icon, frontend/src/apps/bots/lib/face.ts faceOf), without
// the animator, the eyes' moods or the brand glyph library. A blob face is its
// shape in its colour with the two eyes; a brand face (a preset bot) is a disc
// in the brand colour with the bot's initial; Super Bot keeps its starburst.
import type { Face } from "./types";

const SHAPES: Record<string, string> = {
  circle: "M50 14 C70 14 86 30 86 50 C86 70 70 86 50 86 C30 86 14 70 14 50 C14 30 30 14 50 14 Z",
  oval: "M42 14 C62 6 84 18 88 40 C91 60 84 86 62 90 C42 94 18 82 12 60 C7 40 22 22 42 14 Z",
  square:
    "M36 16 C46 14 60 14 70 16 C80 18 86 24 85 36 C84 50 84 62 86 74 C87 84 80 88 70 88 C56 88 40 90 26 86 C16 83 12 76 14 66 C16 54 15 42 16 32 C17 22 26 18 36 16 Z",
  pill: "M30 28 L70 28 C82 28 90 38 90 50 C90 62 82 72 70 72 L30 72 C18 72 10 62 10 50 C10 38 18 28 30 28 Z",
  triangle: "M44 18 C48 12 52 12 56 18 L86 70 C90 78 86 84 78 84 L22 84 C14 84 10 78 14 70 Z",
  hexagon:
    "M44 12 C48 10 52 10 56 12 L82 27 C86 29 88 32 88 36 L88 64 C88 68 86 71 82 73 L56 88 C52 90 48 90 44 88 L18 73 C14 71 12 68 12 64 L12 36 C12 32 14 29 18 27 Z",
  cloud:
    "M30 84 C16 84 10 74 12 64 C6 56 10 44 22 42 C22 28 34 20 46 26 C54 14 74 16 78 32 C92 34 94 52 84 58 C90 70 82 84 68 84 Z",
  drop: "M50 10 C54 22 84 44 84 62 C84 82 68 92 50 92 C32 92 16 82 16 62 C16 44 46 22 50 10 Z",
};
const COLORS = ["#eafe68", "#ffffff", "#7a5230", "#d33b3b", "#f0762a", "#f2a232", "#2f8f58", "#2a9a86", "#2f7ae5", "#8a4fe0", "#d33f8e", "#767676"];
// Brand discs: the preset's colour (face.ts BRANDS). Unknown keys fall back to the face's own colour.
const BRAND_COLORS: Record<string, string> = {
  claude: "#262624", linkedin: "#0a66c2", youtube: "#ff0000", x: "#000000", reddit: "#ff4500",
  instagram: "#e1306c", facebook: "#1877f2", tiktok: "#010101", gmail: "#ea4335", calendar: "#4285f4",
  slack: "#4a154b", github: "#24292f", hackernews: "#ff6600", amazon: "#ff9900", news: "#1a73e8",
  indeed: "#2164f3", maps: "#34a853", notion: "#000000", linkme: "#7c3aed", shopmy: "#e8572a",
  ltk: "#1a1a1a", associates: "#232f3e", twitch: "#9146ff", applenotes: "#f5b400", gdocs: "#4285f4",
  gsheets: "#0f9d58", mac: "#5e5ce6",
};
// Super Bot's mark (bot.py RESERVED_ICON): the Claude starburst, orange on its dark disc.
const STARBURST =
  "M4.709 15.955l4.72-2.647.08-.23-.08-.128H9.2l-.79-.048-2.698-.073-2.339-.097-2.266-.122-.571-.121L0 11.784l.055-.352.48-.321.686.06 1.52.103 2.278.158 1.652.097 2.449.255h.389l.055-.157-.134-.098-.103-.097-2.358-1.596-2.552-1.688-1.336-.972-.724-.491-.364-.462-.158-1.008.656-.722.881.06.225.061.893.686 1.908 1.476 2.491 1.833.365.304.145-.103.019-.073-.164-.274-1.355-2.446-1.446-2.49-.644-1.032-.17-.619a2.97 2.97 0 01-.104-.729L6.283.134 6.696 0l.996.134.42.364.62 1.414 1.002 2.229 1.555 3.03.456.898.243.832.091.255h.158V9.01l.128-1.706.237-2.095.23-2.695.08-.76.376-.91.747-.492.584.28.48.685-.067.444-.286 1.851-.559 2.903-.364 1.942h.212l.243-.242.985-1.306 1.652-2.064.73-.82.85-.904.547-.431h1.033l.76 1.129-.34 1.166-1.064 1.347-.881 1.142-1.264 1.7-.79 1.36.073.11.188-.02 2.856-.606 1.543-.28 1.841-.315.833.388.091.395-.328.807-1.969.486-2.309.462-3.439.813-.042.03.049.061 1.549.146.662.036h1.622l3.02.225.79.522.474.638-.079.485-1.215.62-1.64-.389-3.829-.91-1.312-.329h-.182v.11l1.093 1.068 2.006 1.81 2.509 2.33.127.578-.322.455-.34-.049-2.205-1.657-.851-.747-1.926-1.62h-.128v.17l.444.65 2.345 3.521.122 1.08-.17.353-.608.213-.668-.122-1.374-1.925-1.415-2.167-1.143-1.943-.14.08-.674 7.254-.316.37-.729.28-.607-.461-.322-.747.322-1.476.389-1.924.315-1.53.286-1.9.17-.632-.012-.042-.14.018-1.434 1.967-2.18 2.945-1.726 1.845-.414.164-.717-.37.067-.662.401-.589 2.388-3.036 1.44-1.882.93-1.086-.006-.158h-.055L4.132 18.56l-1.13.146-.487-.456.061-.746.231-.243 1.908-1.312-.006.006z";

function hash(s: string): number {
  let h = 0;
  for (const c of s) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return h;
}

/** Light faces (the lime and white palette entries) take dark eyes and initials. */
function isLight(hex: string): boolean {
  const m = /^#([0-9a-f]{6})$/i.exec(hex);
  if (!m) return false;
  const n = parseInt(m[1], 16);
  const r = n >> 16, g = (n >> 8) & 255, b = n & 255;
  return 0.299 * r + 0.587 * g + 0.114 * b > 186;
}

export function BotFace({
  id,
  name,
  face,
  size = 40,
  className,
}: {
  id: string;
  name: string;
  face?: Face | null;
  size?: number;
  className?: string;
}) {
  const f = face || {};
  const h = hash(id || name);
  const keys = Object.keys(SHAPES);
  const icon = f.icon && BRAND_COLORS[f.icon] ? f.icon : "";
  const okHex = !!f.color && /^#[0-9a-f]{6}$/i.test(f.color);
  const color =
    icon === "claude"
      ? BRAND_COLORS.claude
      : icon
        ? okHex ? f.color! : BRAND_COLORS[icon]
        : f.color && COLORS.includes(f.color) ? f.color : COLORS[1 + ((h >> 4) % (COLORS.length - 1))];
  const ink = isLight(color) ? "#1b1d21" : "#ffffff";
  const shape = f.shape && SHAPES[f.shape] ? f.shape : keys[h % keys.length];
  return (
    <svg
      viewBox="0 0 100 100"
      width={size}
      height={size}
      className={className}
      aria-hidden
      style={{ flex: "none" }}
    >
      {icon === "claude" ? (
        <>
          <circle cx="50" cy="50" r="46" fill={color} />
          <g transform="translate(23 23) scale(2.25)" fill="#d97757">
            <path d={STARBURST} />
          </g>
        </>
      ) : icon ? (
        <>
          <circle cx="50" cy="50" r="46" fill={color} />
          <text
            x="50"
            y="51"
            textAnchor="middle"
            dominantBaseline="central"
            fontSize="44"
            fontWeight="650"
            fill={ink}
            fontFamily="-apple-system, system-ui, sans-serif"
          >
            {(name.trim()[0] || "?").toUpperCase()}
          </text>
        </>
      ) : (
        <g transform="translate(50 50) scale(1.18) translate(-50 -50)">
          <path d={SHAPES[shape]} fill={color} stroke="rgba(0,0,0,.12)" strokeWidth="1" />
          <ellipse cx="37" cy="31" rx="9" ry="4.5" fill="#fff" opacity=".28" transform="rotate(-28 37 31)" />
          <rect x="41" y="43" width="5" height="12" rx="2.5" fill={isLight(color) ? "#1b1d21" : "#10131a"} />
          <rect x="54" y="43" width="5" height="12" rx="2.5" fill={isLight(color) ? "#1b1d21" : "#10131a"} />
        </g>
      )}
    </svg>
  );
}
