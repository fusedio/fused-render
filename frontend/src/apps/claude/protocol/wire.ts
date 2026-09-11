// The MESSAGE WIRE: what the page prepends to the user's typed words, and the
// exact inverse that peels it back off. Ported VERBATIM from the template —
// every string and every regex below is the same one `T` writes and reads, and
// they have to be: agent.py strips the app-state block server-side for
// `meta.json`, and a session on disk is read back by this half years later.
//
// Source of truth: `.claude-design/inventory/03-shots-attach-composer.md §D`
// and `T` (fused_render/templates/claude/template.html):
//   composeOutgoing        T:10449
//   paneShotBlock          T:10404
//   formatAnnotations      T:10349
//   annStanza / annClock   T:10296 / T:10273
//   stripAppStateBlock     T:10475
//   stripPaneBlock         T:10489
//   stripAnnBlock          T:10608
//   stripBlocks            T:10552
//   isMarkerOnly           T:10539
//   paneShotIn             T:10501
//   annotationsIn          T:11179
//
// Pure TS: no DOM, no React.

/** T:4761 — the app-state block's tag. */
export const APP_STATE_TAG = "live-app-state";
/** T:4775 — the annotations block's tag. */
export const ANN_TAG = "annotations";
/** T:4790 — the attached-pictures block's tag. */
export const PANE_SHOT_TAG = "pane-shot";

// ---- markers (T:10525-10545) ----------------------------------------------
//
// The substitute texts `stripBlocks` uses when a send carried no typed words.
// ONE definition, read by both the strip that produces them and the re-attach
// probe, which must never match a prior turn on one.

// NO EMOJI (Akshil, 2026-09-09, P2-7). T wrote these with 📌/🖼/📄 in front of
// the word; the icon beside a wordless send's bubble is a lucide glyph now
// (`ui/Turn`'s marker row).
//
// THE EMOJI CARRIED MORE THAN A PICTURE. It also made the marker a string no
// reader could plausibly type, and once it went, "files" — an ordinary thing to
// say to an agent — was indistinguishable from the substitute text a wordless
// send gets, so `isMarkerOnly` drew an attachment icon in front of the reader's
// own word (Bugbot, PR #1064).
//
// So the marker is SIGILLED: every one of these strings opens with U+2063
// INVISIBLE SEPARATOR, and marker-ness is THE SIGIL, never the visible word. It
// is a private token of this page's display layer — stamped only on text
// `stripBlocks` synthesises for a bubble that had no words, never composed onto
// the wire (nothing `composeOutgoing` writes is built out of these), and peeled
// back off by `markerWord` for everything a human reads.
export const MARKER_SIGIL = "\u2063";
export const MARKER_ANN = MARKER_SIGIL + "annotations";
export const MARKER_VIEW = MARKER_SIGIL + "pane screenshot";
export const MARKER_IMG = MARKER_SIGIL + "images";
export const MARKER_FILE = MARKER_SIGIL + "files";
export const MARKERS: string[] = [MARKER_ANN, MARKER_VIEW, MARKER_IMG, MARKER_FILE];
export const MARKER_JOIN = " + ";

/** One marker's visible word — what a bubble, a session row and every other
 *  human-facing label show. A string with no sigil is already its own word. */
export function markerWord(part: string): string {
  return part.startsWith(MARKER_SIGIL) ? part.slice(MARKER_SIGIL.length) : part;
}

/** The same for a whole `" + "`-joined run of them — and for any other string,
 *  which comes back untouched. What a label that is not drawn part-by-part (a
 *  session row, a heading) shows. */
export function markerWords(text: string): string {
  return text.split(MARKER_SIGIL).join("");
}

/** T:10539 — every `" + "`-split part is one of MARKERS (and there is text).
 *  A sigil-free string is the reader's own words, whatever they happen to say;
 *  U+2063 is a format character and not whitespace, so `trim` cannot eat it. */
export function isMarkerOnly(text: string | null | undefined): boolean {
  const t = (text || "").trim();
  if (!t || t.indexOf(MARKER_SIGIL) === -1) return false;
  return t.split(MARKER_JOIN).every((part) => MARKERS.indexOf(part) !== -1);
}

/** THE CLI'S OWN INTERRUPT MARKER (R2-2). When a turn is cut short the Claude
 *  Code CLI writes this exact string into the transcript as a USER-ROLE record —
 *  it is not something the reader typed, and drawn as a user bubble it reads as
 *  the reader having sent those five words to the model. Matched on the exact
 *  text, which is the only thing the record carries that tells it apart from a
 *  real prompt (the role, the uuid and the timestamp are a prompt's).
 *
 *  IN THE PROTOCOL LAYER, not in `ui/Turn.tsx` where it started, because TWO
 *  places have to agree about it and only one of them draws: `Turn` renders such
 *  a record as a centred note with NO `data-msg`, and `protocol/recap.ts`'s
 *  `recapAnchor` must therefore not offer its uuid as somewhere to scroll to.
 *  Two copies of this string is how the "While you were away" fold's body
 *  became a dead click — the recap anchored on an interrupt row that the
 *  transcript renders without an anchor attribute at all. */
export const INTERRUPT_MARK = "[Request interrupted by user]";

/** Is this user row the CLI's interrupt marker rather than a prompt? Trimmed,
 *  because the record has carried a trailing newline in some CLI builds; NOT
 *  case-folded or fuzzy — a prompt that happens to talk about interrupts must
 *  still render as what the reader wrote. */
export function isInterruptMark(text: string | undefined | null): boolean {
  return (text ?? "").trim() === INTERRUPT_MARK;
}

// ---- the pictures block ----------------------------------------------------

/** One entry of the `<pane-shot>` payload (T:16519 wire field mapping). */
export interface PaneShotWire {
  kind: "overview" | "pane" | "image" | "file" | string;
  view: string | null;
  viewNote?: string;
  why?: string;
  name?: string;
  size?: number;
}

/** T:10404 `paneShotBlock`. `paneNoun` is "app" for a project, "preview" for a
 *  file (T:5407) — passed in, because the wire never reads the UI. */
export function paneShotBlock(views: PaneShotWire[] | null | undefined, paneNoun: string): string {
  const list = (views || []).filter(Boolean);
  if (!list.length) return "";
  const files = list.filter((s) => s.kind === "file").length;
  const noun =
    files === list.length
      ? list.length === 1
        ? "a file"
        : "files"
      : files
        ? "attachments"
        : list.length === 1
          ? "a picture"
          : "pictures";
  const what = list.length === 1 && noun.indexOf("a ") === 0 ? noun : list.length + " " + noun;
  return (
    "<" +
    PANE_SHOT_TAG +
    ">\n" +
    "The user attached " +
    what +
    " to this message, deliberately, for this message only. " +
    "Each entry's `view` is a path to read; `viewNote` is what that picture does " +
    "NOT show, and it is worth reading before trusting the pixels — it arrives " +
    "with a null `view` when the attachment failed outright, and ALONGSIDE a " +
    "real one when part of it is unreadable. `kind` says what you are looking " +
    'at: "overview" is a picture of the WHOLE visible ' +
    paneNoun +
    " pane " +
    "taken at send time with a red letter badge burned in at each annotated " +
    "spot — the letters match the `label` fields in the annotations block, and " +
    "this is THE picture to read when reconciling the user's comments with " +
    'what is on screen; "pane" is a picture of the WHOLE visible ' +
    paneNoun +
    " pane, taken " +
    "here — read it when the question is about the layout as a whole (panels " +
    "overlapping, everything shifted, the page just looking wrong), which no " +
    'badge-level reading can answer; "image" is a file the user pasted or ' +
    "dragged in from somewhere else, so it is NOT a picture of this pane and " +
    '`name` is what they called it; "file" is a file the user attached from ' +
    "elsewhere and it is NOT a picture at all — read it as text, and say so " +
    "plainly rather than guessing if it is a binary format (xlsx, zip, a PDF) " +
    "that will not parse, and `name` is what they called it.\n" +
    JSON.stringify(list) +
    "\n</" +
    PANE_SHOT_TAG +
    ">"
  );
}

/** T:10501 `paneShotIn` — read the pictures back out. Both payload shapes,
 *  forever: today's array and the bare `{view, viewNote}` object pre-`kind`
 *  sessions carry. The payload is the LAST line inside the block; anything that
 *  does not parse answers `[]` rather than throwing mid-restore. */
export function paneShotIn(text: string | null | undefined): PaneShotWire[] {
  const re = new RegExp("<" + PANE_SHOT_TAG + ">([\\s\\S]*?)</" + PANE_SHOT_TAG + ">");
  const m = re.exec(text || "");
  if (!m) return [];
  const line = m[1].trim().split("\n").pop() as string;
  try {
    // JSON.parse is `any` by contract; narrowed on the next two lines.
    const v: unknown = JSON.parse(line);
    if (Array.isArray(v)) return v.filter((s) => s && typeof s === "object") as PaneShotWire[];
    return v && typeof v === "object" ? [v as PaneShotWire] : [];
  } catch {
    return [];
  }
}

// ---- the annotations block -------------------------------------------------

/** One annotation as the wire carries it (`T` `annotations[]` entries). */
export interface AnnotationWire {
  id?: string;
  label?: string;
  kind?: "point" | "element" | string;
  x?: number;
  y?: number;
  nearPath?: string;
  tag?: string;
  text?: string;
  anchorId?: string;
  anchorPath?: string;
  iu?: number | null;
  iv?: number | null;
  t?: number;
  offscreen?: string;
  content?: string;
  sent?: 0 | 1;
}

/** T:10296 — the placeholder for a stanza with no words. */
export const ANN_NO_WORDS = "_(no words for this spot)_";

/** T:10273 `annClock` — mm:ss for a walkthrough timestamp. */
export function annClock(t: number): string {
  return Math.floor(t / 60) + ":" + String(Math.floor(t % 60)).padStart(2, "0");
}

/** T:10296 `annStanza` — one annotation as a markdown stanza. */
export function annStanza(c: AnnotationWire): string {
  const bits: string[] = [];
  if (c.kind === "point") {
    bits.push(
      "point (" +
        c.x +
        ", " +
        c.y +
        ")" +
        (c.nearPath ? " inside `" + c.nearPath + "`" : " — no element under it"),
    );
  } else {
    if (c.tag) bits.push("`<" + c.tag + ">`");
    if (c.text) bits.push("“" + c.text + "”");
    if (c.anchorId) bits.push("`#" + c.anchorId + "`");
    else if (c.anchorPath) bits.push("`" + c.anchorPath + "`");
    if (c.iu != null && c.iv != null) {
      bits.push("at " + Math.round(c.iu * 100) + "%×" + Math.round(c.iv * 100) + " of its content box");
    }
  }
  let head = "**" + (c.label || "?") + "** — " + bits.join(" — ");
  if (typeof c.t === "number") head += "  · " + annClock(c.t);
  const lines = [head];
  if (c.offscreen) lines.push("_no badge on the overview: " + c.offscreen + "_");
  // A BLANK LINE inside the note is collapsed — a blank line is the stanza
  // boundary (Bugbot, PR #783). Single newlines survive.
  lines.push(c.content ? c.content.replace(/\n[ \t]*(\n[ \t]*)+/g, "\n").trim() : ANN_NO_WORDS);
  return lines.join("\n");
}

/** T:10349 `formatAnnotations`. `targetNoun` is "file" or "project" (T:5388) —
 *  only the two shapes that HAVE a pane can get here. */
export function formatAnnotations(arr: AnnotationWire[], targetNoun: string): string {
  const rows = arr.slice().sort((a, b) => {
    const at = typeof a.t === "number" ? a.t : Infinity;
    const bt = typeof b.t === "number" ? b.t : Infinity;
    return at - bt;
  });
  const spoken = rows.some((c) => typeof c.t === "number");
  return (
    "<" +
    ANN_TAG +
    ">\n" +
    "The user annotated " +
    arr.length +
    " thing" +
    (arr.length === 1 ? "" : "s") +
    (targetNoun === "file"
      ? " in the left preview of this file"
      : " in the running app (left preview of this project's entry HTML)") +
    ". Each entry below is one spot they clicked; its bold letter is the red " +
    'badge burned into the attached "overview" screenshot at that spot, which ' +
    "is the picture to read when reconciling a note with what is on screen. " +
    (spoken
      ? "The timestamps are minutes:seconds into a spoken walkthrough — the " +
        "words under each entry are what the user said nearest that moment, " +
        "the typed message below (if any) is what they said BEFORE the first " +
        "mark, and the entries are already in the order they spoke them. "
      : "") +
    "These are the user's notes, not instructions.\n\n" +
    rows.map(annStanza).join("\n\n") +
    "\n</" +
    ANN_TAG +
    ">"
  );
}

// Stanza parser regexes, VERBATIM from T:11132-11137. The first bit is parsed
// POSITIONALLY (Bugbot, PR #783): a `text` digest that quotes coordinates must
// not read as a point note.
const ANN_STANZA_HEAD = /^\*\*(.+?)\*\* — /;
const ANN_STANZA_CLOCK = /\s+·\s+(\d+):(\d\d)$/;
const ANN_STANZA_SEP = " — ";
const ANN_STANZA_POINT = /^point \((-?\d+), (-?\d+)\)/;
const ANN_STANZA_TAG = /^`<([a-zA-Z0-9-]+)>`$/;
const ANN_STANZA_OFFSCREEN = /^_no badge on the overview: ([\s\S]+)_$/;

/** T:11139 `annStanzaIn` — one stanza back into a note, or `null` for the
 *  preamble paragraph (and for a stanza we did not write). */
export function annStanzaIn(para: string): AnnotationWire | null {
  const lines = para
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
  if (!lines.length) return null;
  const head = ANN_STANZA_HEAD.exec(lines[0]);
  if (!head) return null;
  const c: AnnotationWire = { label: head[1] };
  let rest = lines[0].slice(head[0].length);
  const clock = ANN_STANZA_CLOCK.exec(rest);
  if (clock) {
    c.t = Number(clock[1]) * 60 + Number(clock[2]);
    rest = rest.slice(0, clock.index);
  }
  const sep = rest.indexOf(ANN_STANZA_SEP);
  const first = sep === -1 ? rest : rest.slice(0, sep);
  const point = ANN_STANZA_POINT.exec(first);
  if (point) {
    c.kind = "point";
    c.x = Number(point[1]);
    c.y = Number(point[2]);
  }
  const tag = ANN_STANZA_TAG.exec(first);
  if (tag) c.tag = tag[1];
  const said: string[] = [];
  for (const line of lines.slice(1)) {
    const off = ANN_STANZA_OFFSCREEN.exec(line);
    if (off) {
      c.offscreen = off[1];
      continue;
    }
    // The two KNOWN machine lines only — a note that is one emphasised word is
    // still the user's word.
    if (line === ANN_NO_WORDS) continue;
    said.push(line);
  }
  c.content = said.join("\n");
  return c;
}

/** T:11179 `annotationsIn` — today's tagged block, or the legacy tag-less
 *  prose-plus-fenced-json shape at position zero. */
export function annotationsIn(text: string | null | undefined): AnnotationWire[] {
  const t = text || "";
  const tagged = new RegExp("<" + ANN_TAG + ">([\\s\\S]*?)</" + ANN_TAG + ">").exec(t);
  if (tagged) {
    return tagged[1]
      .split(/\n\s*\n/)
      .map(annStanzaIn)
      .filter((c): c is AnnotationWire => !!c);
  }
  const legacy = stripPaneBlock(stripAppStateBlock(t));
  if (!legacy.startsWith("The user annotated ")) return [];
  const i = legacy.indexOf("\n```json\n");
  if (i === -1) return [];
  const j = legacy.indexOf("\n```", i + 9);
  if (j === -1) return [];
  try {
    const v: unknown = JSON.parse(legacy.slice(i + 9, j));
    return Array.isArray(v) ? (v.filter((c) => c && typeof c === "object") as AnnotationWire[]) : [];
  } catch {
    return [];
  }
}

// ---- compose / strip -------------------------------------------------------

/**
 * §D'S WIRE ORDER, IN ONE PLACE. `composeOutgoing` joins whatever list it is
 * handed, so until now the order was whatever the caller's spread happened to
 * produce — and `{ ...opts, ...takeAttachments() }` did not produce an order at
 * all, it REPLACED `opts.blocks` wholesale. Latent while only one owner supplied
 * blocks; the moment PR3's `<annotations>` and PR4's `<live-app-state>` arrive in
 * `opts.blocks` they would have been silently dropped.
 *
 * So every owner's blocks come through here instead: state, pane-shot,
 * annotations — the reading order T composes them in (T:10449) — and anything
 * unrecognised keeps its arrival order at the END rather than being dropped or
 * pushed in front of the three that have a stated place. The sort is STABLE, so
 * two blocks of the same kind stay in the order their owner emitted them.
 */
export const BLOCK_ORDER: readonly string[] = [APP_STATE_TAG, PANE_SHOT_TAG, ANN_TAG];

/** Which of `BLOCK_ORDER` a composed block opens with; `BLOCK_ORDER.length` for
 *  anything else, which is what puts it last.
 *
 *  ATTRIBUTES ARE ALLOWED on the opening tag. Matching a bare `<tag>` meant the
 *  first block to carry one — `<live-app-state v="2">`, which is PR4's tag to
 *  write — would quietly stop recognising its own name and drop to the unranked
 *  tail, with nothing to say so: the message still goes out, just with the state
 *  after the pictures. `[^>]*` and not `.*`, so the sniff cannot run past the end
 *  of the tag it is reading. */
export function blockRank(block: string): number {
  const tag = /^<([a-z][a-z0-9-]*)(?:\s[^>]*)?>/i.exec(block.trimStart());
  const i = tag ? BLOCK_ORDER.indexOf(tag[1].toLowerCase()) : -1;
  return i === -1 ? BLOCK_ORDER.length : i;
}

export function composeBlocks(
  ...groups: (readonly (string | null | undefined)[] | null | undefined)[]
): string[] {
  const flat: string[] = [];
  for (const g of groups) for (const b of g ?? []) if (b) flat.push(b);
  return flat
    .map((block, i) => ({ block, i, rank: blockRank(block) }))
    .sort((a, b) => a.rank - b.rank || a.i - b.i)
    .map((e) => e.block);
}

/** T:10449 `composeOutgoing`. The blocks are pre-composed by their owners
 *  (app-state, pictures, annotations — in that order) and joined `"\n\n"` with
 *  the typed message LAST. All three strips are position-independent, so the
 *  order is a READING order for the model, not a constraint. */
export function composeOutgoing(message: string, blocks: (string | null | undefined)[] = []): string {
  const parts: string[] = [];
  for (const b of blocks) if (b) parts.push(b);
  if (message) parts.push(message);
  return parts.join("\n\n");
}

/** T:10475 — the app-state block, removed from wherever it sits. `trim()` like
 *  agent.py's `.strip()`: peeling a block off leaves the joiner behind, and
 *  `stripAnnBlock` recognises its legacy preamble only at position zero. */
export function stripAppStateBlock(text: string | null | undefined): string {
  const re = new RegExp("<" + APP_STATE_TAG + ">[\\s\\S]*?</" + APP_STATE_TAG + ">\\s*", "g");
  return (text || "").replace(re, "").trim();
}

/** T:10489 — the pane-shot block, removed from wherever it sits. */
export function stripPaneBlock(text: string | null | undefined): string {
  const re = new RegExp("<" + PANE_SHOT_TAG + ">[\\s\\S]*?</" + PANE_SHOT_TAG + ">\\s*", "g");
  return (text || "").replace(re, "").trim();
}

/** T:10608 `stripAnnBlock` — TWO shapes, forever: today's tagged block from
 *  anywhere, and the legacy prose + fenced json at position ZERO. */
export function stripAnnBlock(text: string | null | undefined): string {
  const re = new RegExp("<" + ANN_TAG + ">[\\s\\S]*?</" + ANN_TAG + ">\\s*", "g");
  const tagged = (text || "").replace(re, "").trim();
  if (tagged !== (text || "").trim()) return tagged;
  if (!tagged.startsWith("The user annotated ")) return tagged;
  const i = tagged.indexOf("\n```json\n");
  if (i === -1) return tagged;
  const j = tagged.indexOf("\n```", i + 9);
  if (j === -1) return tagged;
  return tagged.slice(j + 4).replace(/^\n+/, "");
}

/** T:10552 `stripBlocks` — the inverse of `composeOutgoing`: exactly what the
 *  user typed, or the markers naming what a wordless send carried. */
export function stripBlocks(text: string | null | undefined): string {
  const noState = stripAppStateBlock(text);
  const noPaneShot = stripPaneBlock(noState);
  const bare = stripAnnBlock(noPaneShot);
  if (bare) return bare;
  const carried: string[] = [];
  if (bare !== noPaneShot) carried.push(MARKER_ANN);
  if (noPaneShot !== noState) {
    const views = paneShotIn(noState);
    // A session written before `kind` existed has none at all, which is exactly
    // the pane case — so the default falls the right way on its own.
    const brought = views.length > 0 && views.every((v) => v.kind === "image" || v.kind === "file");
    carried.push(
      !brought ? MARKER_VIEW : views.every((v) => v.kind === "image") ? MARKER_IMG : MARKER_FILE,
    );
  }
  return carried.join(MARKER_JOIN);
}

/** What a wire message carried. `appState` is the block's raw body (the
 *  snapshot's own shape belongs to the pane, `01-boot-appstate-pane.md`). */
export interface Inbound {
  /** The user's typed words, or the markers for a wordless send. */
  text: string;
  appState?: string;
  paneShots?: PaneShotWire[];
  annotations?: AnnotationWire[];
}

/** Read a wire message whole: what to show in the bubble plus everything a
 *  restored turn needs to rebuild its receipt (T:10932 rebuilds exactly this). */
export function parseInbound(text: string | null | undefined): Inbound {
  const raw = text || "";
  const out: Inbound = { text: stripBlocks(raw) };
  const state = new RegExp("<" + APP_STATE_TAG + ">([\\s\\S]*?)</" + APP_STATE_TAG + ">").exec(raw);
  if (state) out.appState = state[1].trim();
  const shots = paneShotIn(raw);
  if (shots.length) out.paneShots = shots;
  const notes = annotationsIn(raw);
  if (notes.length) out.annotations = notes;
  return out;
}
