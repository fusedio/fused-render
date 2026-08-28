// Who may be handed a base image, and what that does to the request (AI-9f).
//
// Two rules, in one module, because the image stage has to apply BOTH of them
// twice — once to decide what to draw and once to decide what to send — and a
// copy of either in the JSX is how an attach button comes to exist for a model
// whose every request then 400s.
//
// Neither rule decides WHETHER a model can be edited: that is the server's
// `acceptsImage`, computed from the resolved engine and the model's own edit
// variant (the same two gates `/api/ai/image` refuses with). These only read
// its answer.

/** A base image, once its bytes are on disk — the route takes a PATH. */
export interface AttachedImage {
  path: string;
  name: string;
}

/** Does this model take a base image at all — i.e. does the stage draw the
 *  attach row?
 *
 *  `=== true`, not truthy: `acceptsImage` is absent from an older server's
 *  payload, and absence has to read as no rather than as an attach button
 *  whose every request comes back 400. */
export function canEdit(acceptsImage: boolean | undefined): boolean {
  return acceptsImage === true;
}

/** The attachment this model can actually be SENT, or null.
 *
 *  `canEdit` applied to the attachment, which is a separate question from
 *  whether one exists: the photo survives a model switch on purpose (it rides
 *  the URL, so "same photo, other model" is one click) — which is exactly why
 *  the render-only model it lands on must not keep offering it. */
export function usableBase(
  acceptsImage: boolean | undefined,
  attachment: AttachedImage | null,
): AttachedImage | null {
  return canEdit(acceptsImage) ? attachment : null;
}

// -- the text stage's own names for the identical rule (AI-11j) --------------
//
// `acceptsImage` grew a second meaning once the mlx_text runner switched to
// mlx-vlm: a TEXT_GENERATION model can be handed a picture to be ASKED ABOUT,
// not only an IMAGE_GENERATION one to EDIT. The computation the server did is
// unchanged either way — one bool, one flag — but `canEdit`/`usableBase`
// genuinely do not carry here: this stage never edits anything, and a reader
// of TextStage.tsx calling `canEdit` on a chat model would read as a bug.
// Aliases, not a second copy of either rule, so the two stages cannot drift.

/** `canEdit`, under the name the text stage's own affordance reads by. */
export const canAttachImage = canEdit;

/** `usableBase`, under the text stage's own name. */
export const usableAttachment = usableBase;

/** A pixel size, both sides. */
export interface Size {
  width: number;
  height: number;
}

/** How long the longest side of an EDIT may be by default.
 *
 *  The server's own derivation is 1024 (AI-9f), which is the right default for
 *  a page calling `fused.ai.image` and the wrong one here: a 1024-wide edit is
 *  ~4.5x the pixels of this stage's own fresh-render default (480x272) and it
 *  showed — a screenshot dropped in took minutes, where the whole promise of
 *  this stage is a picture back quickly. 640 is ~2.5x cheaper than 1024 and
 *  still big enough to judge an edit by, and the sliders go to 2048 for
 *  anybody who wants the full-size one. */
export const EDIT_LONGEST_SIDE = 640;

/** The render size for an edit of a `natural`-sized picture: its own SHAPE, at
 *  a size this stage is willing to wait for.
 *
 *  Fit the longest side to `cap`, keep the aspect, snap both sides DOWN to a
 *  multiple of 16 (the pipelines require it and the route floors to it anyway,
 *  so snapping here is what keeps the number on screen the number that runs),
 *  and never go below 256 or above the picture's own size — upscaling a small
 *  photo would spend the time this whole function exists to save. */
export function fitToImage(natural: Size, cap = EDIT_LONGEST_SIDE): Size {
  const longest = Math.max(natural.width, natural.height);
  if (!longest || !Number.isFinite(longest)) return { width: cap, height: cap };
  // No upscaling: a 300px avatar is edited at 300px, not blown up to 640.
  const scale = Math.min(1, cap / longest);
  const snap = (side: number) => {
    const scaled = Math.floor(side * scale);
    return Math.max(256, scaled - (scaled % 16));
  };
  return { width: snap(natural.width), height: snap(natural.height) };
}

/** The `image`/`width`/`height` fields of one render request.
 *
 *  Four cases, and the third is the point of the whole function:
 *
 *  - no base image      -> this stage's own size, as any fresh render.
 *  - a size picked BY HAND (`sizeFromImage` false) -> theirs, unchanged.
 *  - the picture's shape at `EDIT_LONGEST_SIDE` -> sent EXPLICITLY, so the
 *    numbers the settings panel shows are the numbers that run.
 *  - the shape wanted but not KNOWN (`fitted` null: the probe has not answered,
 *    or the browser could not decode the file) -> the pair is left off and the
 *    server derives it from the file's own header (AI-9f). Slower, since that
 *    derivation caps at 1024, but never the wrong shape — where sending this
 *    stage's 480x272 as a fallback would squash a portrait flat. */
export function imageFields(
  base: AttachedImage | null,
  sizeFromImage: boolean,
  fitted: Size | null,
  width: number,
  height: number,
): { image?: string; width?: number; height?: number } {
  if (!base) return { width, height };
  if (!sizeFromImage) return { image: base.path, width, height };
  return fitted ? { image: base.path, ...fitted } : { image: base.path };
}
