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

/** The `image`/`width`/`height` fields of one render request.
 *
 *  With a base image the SERVER derives the size from that image — fit the
 *  longest side to 1024, keep the shape (AI-9f) — which is a better default
 *  than any of this stage's, so the pair is left OFF rather than sent. Leaving
 *  it off is not just a nicety: sending the stage's own 480x272 would resize a
 *  photograph down to a thumbnail on the way through the edit. The moment
 *  somebody picks a size themselves (`sizeFromImage` false), theirs is sent
 *  and the derivation is out of the way. */
export function imageFields(
  base: AttachedImage | null,
  sizeFromImage: boolean,
  width: number,
  height: number,
): { image?: string; width?: number; height?: number } {
  if (!base) return { width, height };
  if (sizeFromImage) return { image: base.path };
  return { image: base.path, width, height };
}
