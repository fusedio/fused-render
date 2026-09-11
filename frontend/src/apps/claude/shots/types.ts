// Shared contract between the screenshot/attachment pipeline (shots/*.ts, pure
// TS) and the attachment UI (ui/AttachTray, ui/ShotViewer, ui/SentPop,
// receipts). Extend, never rename. Source: inventory 03 §D/§E (T:9019-11750).

/** T:11366 shotKindFor — what the agent is told it is looking at. */
export type ShotKind = "pane" | "overview" | "image" | "file";

/** A pending or sent attachment. Exactly one of `thumb`/`size` is set when
 *  drawable vs not (T:11603). */
export interface Attachment {
  /** Stable client id (chip key, revoke handle). */
  id: string;
  kind: ShotKind;
  /** Absolute path the agent reads (`view` on the wire); null when saving failed. */
  view: string | null;
  /** Object URL or `rawUrl(view)` when drawable. */
  thumb?: string;
  /** Bytes when not drawable (file / undecodable image). */
  size?: number;
  /** Display name for image/file kinds (T:16519 `name`). */
  name?: string;
  /** What the picture does NOT show (T: viewNote = caveats + trust line). */
  viewNote?: string;
  /** Refusal reason when `view` is null (T: why "could not be saved"). */
  why?: string;
  /** Real-path drag: no upload happened, the path is the user's own (T:11644). */
  brought?: boolean;
  /** Pane seat is unique (T:11309) — the tray replaces the previous one. */
  seat?: "pane";
  /** True while capture/upload is in flight (chip shows spinner/disabled). */
  pending?: boolean;
}

/** Wire entry inside `<pane-shot>` — mirrors protocol/wire.ts PaneShotEntry. */
export interface PaneShotEntry {
  kind: ShotKind;
  view: string | null;
  viewNote?: string;
  why?: string;
  name?: string;
  size?: number;
}

/** Receipt row under a sent user turn (T:10815 annsum / shotReceipt). */
export interface Receipt {
  /** The `Attachment.id` this receipt was built from, when it was built from
   *  one. THE IDENTITY THE UI MATCHES ON: every refusal has `view: null`, so a
   *  kind+view match cannot tell two failed pictures apart (PR2 review). Absent
   *  on a receipt rebuilt from the wire — nothing there is discardable. */
  id?: string;
  kind: ShotKind;
  label: string; // "screenshot attached" | "image attached: name" | shotFailLabel…
  view: string | null;
  thumb?: string;
  /** Set once a HEAD probe says the file is gone (T:10903 pruned detection). */
  pruned?: boolean;
  viewNote?: string;
  /** What to DISPLAY: a blob URL this session, `rawUrl(view)` for a restored
   *  turn. A FILE never gets one — an `<img>` pointed at a .csv is a
   *  broken-image glyph, which reads as a bug (T:10820). */
  src?: string;
  /** The HEAD target for a row with nothing to draw: a picture's pruned copy
   *  announces itself through the `<img>`'s own error, a glyph row has to ASK
   *  (T:10871). Set only by the restore — a live send's bytes were written
   *  seconds ago, so a fresh turn costs no request. */
  probe?: string;
  name?: string;
  size?: number;
  why?: string;
}

export interface CaptureResult {
  blob: Blob | null;
  width: number;
  height: number;
  /** Caveat sentences joined "; " (T:9275 shotPaneNote, 9304 shotImageNote, 10052 blank regions). */
  notes: string[];
  /** Which path produced it (T:9958 order). */
  via: "native" | "tab" | "dom" | "none";
  /** The style walk's verdict, which is what picks the closing trust line:
   *  bounded doubt keeps the reassurance, unbounded doubt replaces it with
   *  corroboration (T:9332). Always `false` on the native and tab paths. */
  incomplete?: PaneBitmap["incomplete"];
  /** Object URL for the encoded bytes — a screenshot nobody can look at before
   *  it goes out is one nobody can check (T:10101). Owned by the caller: pass it
   *  to `revoke` when the attachment goes. */
  thumb?: string;
  /** Set when nothing could be captured or encoded, as the sentence the chip and
   *  the wire's `viewNote` carry (T:10093, 10098). */
  why?: string;
}

/* THERE IS NO ATTACHMENT SIZE CONSTANT (Akshil, 2026-09-09, P2-6). T re-encoded
   a dropped picture over 4 MiB before the upload (T:11355) — never a refusal,
   but still this app changing the bytes someone attached without being asked —
   and the owner's rule is that "any and every file, size and type must not
   matter". Only the PANE SCREENSHOT this app takes itself has a budget
   (`SHOT_IMG_MAX_BYTES` below, and the ladder in `shots/encode`): that is a
   picture it composed, so it gets to size it. */
export const SHOT_VIEW_EDGE = 1600; // T:4755
export const SHOT_VIEW_BYTES = 900 * 1024; // T:4756
export const SHOT_MAX_EDGE = 640; // T:4702 crops
export const SHOT_MAX_BYTES = 300 * 1024; // T:4706
export const SHOT_TIMEOUT_MS = 6000; // T:10225
export const SHOT_FLASH_MS = 340; // T:11232
export const FUSED_PATH_MIME = "application/x-fused-path"; // T:11644

// ── the rest of T's capture budget (T:4702-4756, 9791) ──────────────────────
/** px² below which there is nothing to look at — a 0x0 rect (display:none) or a
 *  hairline border (T:4708 SHOT_MIN_AREA). */
export const SHOT_MIN_AREA = 64;
/** Elements whose computed style one capture will inline before it gives up
 *  (T:4718 SHOT_MAX_ELEMENTS). */
export const SHOT_MAX_ELEMENTS = 3000;
/** Elements between yields to the event loop during that walk — what makes
 *  SHOT_TIMEOUT_MS able to fire at all (T:4729 SHOT_STYLE_CHUNK). */
export const SHOT_STYLE_CHUNK = 200;
/** Distinct image URLs resolved per capture (T:4741 SHOT_IMG_MAX). */
export const SHOT_IMG_MAX = 30;
/** Bytes past which a fetched image is re-encoded down to the size it is DRAWN
 *  at rather than embedded whole (T:4745 SHOT_IMG_MAX_BYTES). */
export const SHOT_IMG_MAX_BYTES = 256 * 1024;
/** The tail of the budget reserved for those fetches (T:4749 SHOT_IMG_MS). */
export const SHOT_IMG_MS = 1500;
/** Quality steps tried before any resolution is given up. JPEG is deliberately
 *  absent: measured on this UI it LOST to PNG (T:9626). */
export const SHOT_WEBP_QUALITY: number[] = [0.8, 0.6];
/** Min frame size worth photographing natively (T:9791 SHOT_NATIVE_MIN). */
export const SHOT_NATIVE_MIN = { width: 120, height: 75 };
/** The drag payload naming a real path (T:11644). */
export const SHOT_PATH_TYPE = FUSED_PATH_MIME;

/** A rasterised pane: whatever `drawImage` can read out of, plus the doubt
 *  fields every caveat writer reads (T:9958 shotPane's answer shape — the
 *  native and tab paths answer the same shape with the doubts clean). */
export interface PaneBitmap {
  canvas: CanvasImageSource;
  width: number;
  height: number;
  /** Source canvases whose pixels could not be read back (T:9554). */
  blanks: Element[];
  /** Elements the style walk finished (T:9124). */
  styled: number;
  /** One cause, never a set of booleans that can disagree (T:9215). */
  incomplete: "" | "elements" | "deadline" | "detached" | "mutated" | false;
  /** Images that could not be inlined (T:9491). */
  imagesMissing: number;
}

/** An integer rect inside a pane bitmap (T:9578 shotCropRect). */
export interface ShotRect {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** One badge burned into the overview (T:8009 annBadgeDraw). */
export interface ShotBadge {
  x: number;
  y: number;
  label: string;
}
