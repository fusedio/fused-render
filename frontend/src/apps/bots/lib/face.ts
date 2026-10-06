// Avatar face data (OpenBot core.js): shape and color from bot.face if picked, else hashed from the bot id (stable
// across renames); a brand icon (preset bots) swaps the blob for a disc with the mark; mood follows status. components/Face.tsx draws it; components/faceAnim.ts animates it.
import type { Bot, Face } from "./api";

export const FACE_SHAPES: Record<string, string> = {
  circle:   "M50 14 C70 14 86 30 86 50 C86 70 70 86 50 86 C30 86 14 70 14 50 C14 30 30 14 50 14 Z",
  oval:     "M42 14 C62 6 84 18 88 40 C91 60 84 86 62 90 C42 94 18 82 12 60 C7 40 22 22 42 14 Z",  /* tilted, sagging blob */
  square:   "M36 16 C46 14 60 14 70 16 C80 18 86 24 85 36 C84 50 84 62 86 74 C87 84 80 88 70 88 C56 88 40 90 26 86 C16 83 12 76 14 66 C16 54 15 42 16 32 C17 22 26 18 36 16 Z",  /* softened, melting box */
  pill:     "M30 28 L70 28 C82 28 90 38 90 50 C90 62 82 72 70 72 L30 72 C18 72 10 62 10 50 C10 38 18 28 30 28 Z",
  triangle: "M44 18 C48 12 52 12 56 18 L86 70 C90 78 86 84 78 84 L22 84 C14 84 10 78 14 70 Z",
  hexagon:  "M44 12 C48 10 52 10 56 12 L82 27 C86 29 88 32 88 36 L88 64 C88 68 86 71 82 73 L56 88 C52 90 48 90 44 88 L18 73 C14 71 12 68 12 64 L12 36 C12 32 14 29 18 27 Z",
  cloud:    "M30 84 C16 84 10 74 12 64 C6 56 10 44 22 42 C22 28 34 20 46 26 C54 14 74 16 78 32 C92 34 94 52 84 58 C90 70 82 84 68 84 Z",
  drop:     "M50 10 C54 22 84 44 84 62 C84 82 68 92 50 92 C32 92 16 82 16 62 C16 44 46 22 50 10 Z",
};
export const FACE_COLORS = ["#eafe68", "#ffffff", "#7a5230", "#d33b3b", "#f0762a", "#f2a232", "#2f8f58", "#2a9a86", "#2f7ae5", "#8a4fe0", "#d33f8e", "#767676"];
// Brand avatars: a white mark on a disc, for bots made from a preset (presets/<key>). Keys match the preset folder names.
// Glyphs are hand-drawn simplified marks in a 24-unit box, not the official logo files; the page has no network to fetch any.
// glyph(c) is SVG inner markup; `c` is the disc colour, for the cut-outs.
export interface Brand { name: string; color: string; glyph: (c: string) => string; /** Not offered by the picker: one bot kind owns it (Super Bot). */ locked?: boolean }
export const BRANDS: Record<string, Brand> = {
  // Super Bot's mark (bot.py RESERVED_ICON): the Claude starburst itself (its 24-unit path), orange on a dark disc as
  // the official icon is, scaled to sit inside the disc. Locked to that one bot; the backend refuses it on any other.
  claude:    { name: "Super Bot", color: "#262624", locked: true, glyph: () => `<g transform="translate(-0.6 -0.6) scale(1.05)" fill="#d97757"><path d="M4.709 15.955l4.72-2.647.08-.23-.08-.128H9.2l-.79-.048-2.698-.073-2.339-.097-2.266-.122-.571-.121L0 11.784l.055-.352.48-.321.686.06 1.52.103 2.278.158 1.652.097 2.449.255h.389l.055-.157-.134-.098-.103-.097-2.358-1.596-2.552-1.688-1.336-.972-.724-.491-.364-.462-.158-1.008.656-.722.881.06.225.061.893.686 1.908 1.476 2.491 1.833.365.304.145-.103.019-.073-.164-.274-1.355-2.446-1.446-2.49-.644-1.032-.17-.619a2.97 2.97 0 01-.104-.729L6.283.134 6.696 0l.996.134.42.364.62 1.414 1.002 2.229 1.555 3.03.456.898.243.832.091.255h.158V9.01l.128-1.706.237-2.095.23-2.695.08-.76.376-.91.747-.492.584.28.48.685-.067.444-.286 1.851-.559 2.903-.364 1.942h.212l.243-.242.985-1.306 1.652-2.064.73-.82.85-.904.547-.431h1.033l.76 1.129-.34 1.166-1.064 1.347-.881 1.142-1.264 1.7-.79 1.36.073.11.188-.02 2.856-.606 1.543-.28 1.841-.315.833.388.091.395-.328.807-1.969.486-2.309.462-3.439.813-.042.03.049.061 1.549.146.662.036h1.622l3.02.225.79.522.474.638-.079.485-1.215.62-1.64-.389-3.829-.91-1.312-.329h-.182v.11l1.093 1.068 2.006 1.81 2.509 2.33.127.578-.322.455-.34-.049-2.205-1.657-.851-.747-1.926-1.62h-.128v.17l.444.65 2.345 3.521.122 1.08-.17.353-.608.213-.668-.122-1.374-1.925-1.415-2.167-1.143-1.943-.14.08-.674 7.254-.316.37-.729.28-.607-.461-.322-.747.322-1.476.389-1.924.315-1.53.286-1.9.17-.632-.012-.042-.14.018-1.434 1.967-2.18 2.945-1.726 1.845-.414.164-.717-.37.067-.662.401-.589 2.388-3.036 1.44-1.882.93-1.086-.006-.158h-.055L4.132 18.56l-1.13.146-.487-.456.061-.746.231-.243 1.908-1.312-.006.006z"/></g>` },
  linkedin:  { name: "LinkedIn",  color: "#0a66c2", glyph: () => `<circle cx="6.5" cy="6" r="1.7"/><rect x="5" y="9" width="3" height="10" rx=".6"/><path d="M10.5 9h2.9v1.5c.6-1 1.8-1.8 3.4-1.8 3 0 4.2 1.8 4.2 4.6V19h-3v-5.1c0-1.5-.5-2.5-1.9-2.5-1.3 0-2.1.9-2.1 2.5V19h-3z"/>` },
  youtube:   { name: "YouTube",   color: "#ff0000", glyph: c => `<rect x="2.5" y="6" width="19" height="12.5" rx="4"/><path d="M10 9.2v6.1l5.3-3.05z" fill="${c}"/>` },
  x:         { name: "X",         color: "#000000", glyph: () => `<path d="M5 4h4l10 16h-4z"/><path d="M18.5 4L5.5 20" stroke="#fff" stroke-width="1.8" fill="none"/>` },
  reddit:    { name: "Reddit",    color: "#ff4500", glyph: c => `<ellipse cx="12" cy="14" rx="7.5" ry="5"/><circle cx="12" cy="5.2" r="1.3"/><path d="M12 6.5V9" stroke="#fff" stroke-width="1.3"/><circle cx="4.6" cy="11.8" r="1.6"/><circle cx="19.4" cy="11.8" r="1.6"/><circle cx="9.3" cy="13.6" r="1.1" fill="${c}"/><circle cx="14.7" cy="13.6" r="1.1" fill="${c}"/><path d="M9.3 16.3c1.5 1.2 3.9 1.2 5.4 0" stroke="${c}" stroke-width="1" fill="none" stroke-linecap="round"/>` },
  instagram: { name: "Instagram", color: "#e1306c", glyph: () => `<rect x="4" y="4" width="16" height="16" rx="5" fill="none" stroke="#fff" stroke-width="2"/><circle cx="12" cy="12" r="3.6" fill="none" stroke="#fff" stroke-width="2"/><circle cx="16.8" cy="7.2" r="1.1"/>` },
  facebook:  { name: "Facebook",  color: "#1877f2", glyph: () => `<path d="M13.4 20v-7h2.4l.4-2.8h-2.8V8.5c0-.8.3-1.4 1.4-1.4h1.5V4.6c-.3 0-1.2-.1-2.2-.1-2.2 0-3.6 1.3-3.6 3.7v2h-2.4V13h2.4v7z"/>` },
  tiktok:    { name: "TikTok",    color: "#010101", glyph: () => `<path d="M13.5 4h2.6c.2 1.9 1.4 3.3 3.4 3.5v2.7c-1.3 0-2.4-.4-3.4-1v5.6a4.9 4.9 0 1 1-4.9-4.9h.6v2.8h-.6a2.1 2.1 0 1 0 2.3 2.1z"/>` },
  gmail:     { name: "Gmail",     color: "#ea4335", glyph: c => `<rect x="3" y="6" width="18" height="13" rx="2"/><path d="M4 7.5l8 6 8-6" fill="none" stroke="${c}" stroke-width="1.8" stroke-linejoin="round"/>` },
  calendar:  { name: "Calendar",  color: "#4285f4", glyph: c => `<rect x="3.5" y="5" width="17" height="15" rx="2.5"/><rect x="3.5" y="5" width="17" height="4.5" rx="2.5" fill="${c}" opacity=".35"/><path d="M7.5 3.5v3M16.5 3.5v3" stroke="#fff" stroke-width="2" stroke-linecap="round"/><text x="12" y="17.6" font-size="7.5" font-weight="700" text-anchor="middle" fill="${c}" font-family="system-ui, sans-serif">31</text>` },
  slack:     { name: "Slack",     color: "#4a154b", glyph: () => `<path d="M9.5 4.5v15M14.5 4.5v15M4.5 9.5h15M4.5 14.5h15" stroke="#fff" stroke-width="2.6" stroke-linecap="round" fill="none"/>` },
  github:    { name: "GitHub",    color: "#24292f", glyph: c => `<path d="M12 4.5c-4.2 0-7.5 3.3-7.5 7.5 0 3.3 2.1 6.1 5.1 7.1v-2.4c-2.1.4-2.6-1-2.6-1-.3-.9-.8-1.1-.8-1.1-.7-.5.1-.5.1-.5.8.1 1.2.8 1.2.8.7 1.2 1.8.9 2.2.7.1-.5.3-.9.5-1.1-1.7-.2-3.4-.8-3.4-3.7 0-.8.3-1.5.8-2-.1-.2-.3-1 .1-2 0 0 .6-.2 2.1.8a7.3 7.3 0 0 1 3.8 0c1.5-1 2.1-.8 2.1-.8.4 1 .2 1.8.1 2 .5.5.8 1.2.8 2 0 2.9-1.7 3.5-3.4 3.7.3.2.5.7.5 1.4v2.3c3-1 5.1-3.8 5.1-7.1 0-4.2-3.3-7.5-7.5-7.5z"/>` },
  hackernews:{ name: "Hacker News", color: "#ff6600", glyph: () => `<path d="M6 4.5h2.8l3.2 6 3.2-6H18l-4.7 8.4v6.6h-2.6v-6.6z"/>` },
  amazon:    { name: "Amazon",    color: "#ff9900", glyph: c => `<text x="12" y="14.5" font-size="13" font-weight="700" text-anchor="middle" fill="#fff" font-family="system-ui, sans-serif">a</text><path d="M5.5 16.5c3.8 2.6 9.2 2.6 13 0" fill="none" stroke="#fff" stroke-width="1.6" stroke-linecap="round"/><path d="M16.3 15.4l2.4.8-.5 2.4" fill="none" stroke="#fff" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>` },
  news:      { name: "News",      color: "#1a73e8", glyph: c => `<rect x="3.5" y="5" width="17" height="14" rx="2"/><rect x="6" y="7.5" width="5.5" height="4.5" rx=".8" fill="${c}"/><path d="M13.5 8h4.5M13.5 10.5h4.5M6 14.5h12M6 16.8h9" stroke="${c}" stroke-width="1.2" stroke-linecap="round"/>` },
  indeed:    { name: "Indeed",    color: "#2164f3", glyph: () => `<circle cx="13.2" cy="5.8" r="2.3"/><rect x="11.2" y="9.5" width="4" height="10.5" rx="1"/><path d="M7 9.2c1-2.6 3.4-4.4 6.2-4.6" fill="none" stroke="#fff" stroke-width="1.6" stroke-linecap="round"/>` },
  maps:      { name: "Maps",      color: "#34a853", glyph: c => `<path d="M12 3.5a6 6 0 0 0-6 6c0 4.4 6 10.5 6 10.5s6-6.1 6-10.5a6 6 0 0 0-6-6z"/><circle cx="12" cy="9.5" r="2.4" fill="${c}"/>` },
  notion:    { name: "Notion",    color: "#000000", glyph: c => `<rect x="4" y="4" width="16" height="16" rx="2.5"/><path d="M8 16.5v-9l7 9v-9" fill="none" stroke="${c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>` },
  linkme:    { name: "Linkme",    color: "#7c3aed", glyph: () => `<path d="M10 14.5l4-4M9.2 9.3l1.4-1.4a3 3 0 0 1 4.2 4.2l-1.4 1.4M14.8 14.7l-1.4 1.4a3 3 0 0 1-4.2-4.2l1.4-1.4" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/>` },
  shopmy:    { name: "ShopMy",    color: "#e8572a", glyph: c => `<path d="M6 8.5h12l-.9 10.5H6.9z"/><path d="M9 8.5V7a3 3 0 0 1 6 0v1.5" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="14" r="1.6" fill="${c}"/>` },
  ltk:       { name: "LTK",       color: "#1a1a1a", glyph: () => `<text x="12" y="15.6" font-size="8.5" font-weight="800" text-anchor="middle" fill="#fff" font-family="system-ui, sans-serif" letter-spacing=".3">LTK</text>` },
  associates:{ name: "Amazon Associates", color: "#232f3e", glyph: () => `<text x="12" y="14" font-size="12" font-weight="700" text-anchor="middle" fill="#ff9900" font-family="system-ui, sans-serif">a</text><path d="M5.5 16.5c3.8 2.6 9.2 2.6 13 0" fill="none" stroke="#ff9900" stroke-width="1.6" stroke-linecap="round"/><path d="M16.3 15.4l2.4.8-.5 2.4" fill="none" stroke="#ff9900" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/><circle cx="18.5" cy="6" r="2.6" fill="#ff9900"/><text x="18.5" y="7.3" font-size="4" font-weight="800" text-anchor="middle" fill="#232f3e" font-family="system-ui, sans-serif">%</text>` },
  twitch:    { name: "Twitch",    color: "#9146ff", glyph: c => `<path d="M6.5 4h12.5v9.2l-3.7 3.7h-3.2l-2.3 2.3v-2.3H6.5z"/><path d="M11 8v4M15 8v4" stroke="${c}" stroke-width="1.6" stroke-linecap="round"/>` },
  applenotes:{ name: "Apple Notes", color: "#f5b400", glyph: c => `<rect x="5" y="4" width="14" height="16" rx="2.5"/><rect x="5" y="4" width="14" height="4.5" rx="2.5" fill="${c}" opacity=".45"/><path d="M8 12h8M8 15h8M8 18h5" stroke="${c}" stroke-width="1.3" stroke-linecap="round"/>` },
  gdocs:     { name: "Google Docs", color: "#4285f4", glyph: c => `<path d="M6 3.5h8l4.5 4.5v12.5H6z"/><path d="M14 3.5V8h4.5" fill="${c}" opacity=".45"/><path d="M8.5 12h7M8.5 14.8h7M8.5 17.6h4.5" stroke="${c}" stroke-width="1.3" stroke-linecap="round"/>` },
  gsheets:   { name: "Google Sheets", color: "#0f9d58", glyph: c => `<path d="M6 3.5h8l4.5 4.5v12.5H6z"/><path d="M14 3.5V8h4.5" fill="${c}" opacity=".45"/><path d="M8.5 11.5h7v6.5h-7zM8.5 14.7h7M12 11.5v6.5" fill="none" stroke="${c}" stroke-width="1.2" stroke-linejoin="round"/>` },
  mac:       { name: "Mac Automation", color: "#5e5ce6", glyph: c => `<rect x="4" y="5" width="16" height="11" rx="2"/><path d="M7.5 9l2.2 1.7L7.5 12.4" fill="none" stroke="${c}" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/><path d="M11.5 12.4h4" stroke="${c}" stroke-width="1.4" stroke-linecap="round"/><path d="M9 19h6" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>` },
};

/** The brand keys the face picker offers: everything but the locked marks. */
export const pickableBrands = (): string[] => Object.keys(BRANDS).filter((k) => !BRANDS[k].locked);

export const hash = (name: unknown): number => {
  let h = 0;
  for (const c of String(name || "")) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return h;
};

/** What faceOf needs: a bot, or a bare `{face}` (the picker's swatches). `shot` tells a never-launched browser from one put to sleep. */
export type FaceSubject = { id?: string; name?: string; face?: Face | null; status?: Bot["status"]; browser?: Bot["browser"]; shot?: Bot["shot"] };

/** A resolved face: always a shape and colour; `icon` is a BRANDS key or "". */
export interface FaceDraft { shape: string; color: string; icon: string }

// A brand face keeps whatever hex color it was given (the brand's own, by default); a blob only takes palette colors.
export const faceOf = (b: FaceSubject): FaceDraft => {
  const h = hash(b.id || b.name), keys = Object.keys(FACE_SHAPES), f = b.face || {}, icon = f.icon && Object.prototype.hasOwnProperty.call(BRANDS, f.icon) ? f.icon : "";
  const c = f.color || "", okColor = FACE_COLORS.includes(c) || (!!icon && /^#[0-9a-f]{6}$/i.test(c));
  return {
    shape: f.shape && FACE_SHAPES[f.shape] ? f.shape : keys[h % keys.length],
    // A locked mark (Super Bot) always sits on its own disc colour: a stored colour from an earlier build (or the
    // mark's own orange) would otherwise paint the disc the same as the rays and swallow the mark.
    color: icon && BRANDS[icon].locked ? BRANDS[icon].color : okColor ? c : icon ? BRANDS[icon].color : FACE_COLORS[1 + (h >> 4) % (FACE_COLORS.length - 1)],
    icon,
  };
};

export type MoodName = "idle" | "running" | "waiting" | "paused" | "error";
export type FaceAction = "look" | "blink" | "bounce" | "gasp" | "snore" | "tear" | "spin";
/** eye: [translateY, rotate (mirrored per eye), scaleX, scaleY]; face: slump [translateY, rotate]; loop: the header routine. */
export interface Mood { eye: [number, number, number, number]; face: [number, number]; loop: [FaceAction, number][] }

// Mood per status: eye pose, face slump, and the header routine.
export const MOODS: Record<MoodName, Mood> = {
  idle:    { eye: [0, 0, 1, 1],        face: [0, 0],  loop: [["look", 900], ["blink", 600], ["look", 1200], ["blink", 300]] },
  running: { eye: [-1, -6, 1.1, 1.1],  face: [0, 0],  loop: [["look", 400], ["blink", 300], ["bounce", 900], ["look", 500], ["blink", 300]] },
  waiting: { eye: [-3, 0, 1.5, 1.3],   face: [0, 0],  loop: [["look", 500], ["gasp", 900], ["blink", 300], ["look", 700]] },
  paused:  { eye: [2, 0, 1.2, .12],    face: [6, 8],  loop: [["snore", 1800], ["snore", 1800]] },
  error:   { eye: [4, 14, 1, .75],     face: [4, -4], loop: [["look", 900], ["blink", 700], ["tear", 1600], ["look", 1100]] },
};

// "paused" (eyes shut, snoring) means the browser went to sleep: it was up once (a shot exists, the same rule the
// preview pane uses) and is down now. A fresh bot has never had a browser and stays awake while it says hi.
export const moodOf = (b: FaceSubject): MoodName =>
  b.status === "idle" && !!b.shot && !b.browser?.running ? "paused" : b.status && b.status in MOODS ? (b.status as MoodName) : "idle";

/** What a drawn face depends on: a change remounts the SVG (and restarts the header routine). */
export const fkey = (b: FaceSubject): string => [b.id, moodOf(b), ...Object.values(faceOf(b))].join(":");

// Excitement props from the emoji builder: four rainbow ribbon arcs that draw on while the face spins, and seven sparks.
export const RIBBONS: { stroke: string; d: string }[] = ([["#ff6fb5", 40], ["#8dff5e", 46], ["#ffb347", 36], ["#5fe0ff", 50]] as [string, number][])
  .map(([c, r]) => ({ stroke: c, d: `M${50 + r * .85} ${50 + r * .4} A${r} ${r} 0 0 0 ${50 - r * .9} ${50 - r * .5}` }));
export const SPARKS = { d: "M0 -4 L1.1 -1.1 L4 0 L1.1 1.1 L0 4 L-1.1 1.1 L-4 0 L-1.1 -1.1 Z", count: 7 };

// A reaction plays on the face (👍 blink, 😂 🎉 bounce, ❤️ spin, 😮 gasp, 👎 tear), then it settles back into its status mood.
export const FACE_RX: Record<string, [MoodName, ...FaceAction[]]> = {
  "👍": ["idle", "blink", "look", "blink"], "😂": ["idle", "bounce", "blink"], "🎉": ["idle", "bounce"],
  "❤️": ["running", "spin"], "😮": ["waiting", "gasp"], "👎": ["error", "tear"],
};

// After a pick the picker preview walks happy → sad → surprised → excited → sleepy, then settles back to happy.
export const PICK_CYCLE: [MoodName, FaceAction][] = [["idle", "blink"], ["error", "tear"], ["waiting", "gasp"], ["running", "spin"], ["paused", "snore"]];
