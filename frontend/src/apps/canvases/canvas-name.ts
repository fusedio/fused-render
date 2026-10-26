// New-canvas name rules — the hosted workbench's own (CreateCanvasModal +
// UDFNameInput + canvas-name-generator in the application repo), so a name
// this page accepts is one the workbench would have accepted, and the error
// the user reads is the one they would read there. One fused-render-only
// rule on top: the server's `_NAME_RE` caps a name at 128 characters.

/** The server's `_NAME_RE` (fused_render/canvases.py), verbatim. */
export const CANVAS_NAME_RE = /^[A-Za-z0-9_]{1,128}$/;
export const CANVAS_NAME_MAX = 128;

/** What typing lands in the box: spaces become underscores (UDFNameInput's
 *  "make it easier to type conformant names"), everything outside the name
 *  alphabet is dropped. The user never sees an illegal character. */
export function sanitizeCanvasName(raw: string): string {
  return raw.replace(/\s/g, "_").replace(/[^A-Za-z0-9_]/g, "");
}

/** The error to show under the input for `name`, or null when it is
 *  creatable. `existing` is the listing's names; the duplicate check is
 *  case-insensitive like the workbench's. */
export function validateCanvasName(
  name: string,
  existing: readonly string[],
): string | null {
  const trimmed = name.trim();
  if (!trimmed) return "Canvas name cannot be empty";
  if (trimmed.length > CANVAS_NAME_MAX)
    return `Canvas names can be at most ${CANVAS_NAME_MAX} characters`;
  if (!CANVAS_NAME_RE.test(trimmed))
    return "Canvas names may only use letters, digits, and underscores";
  const lower = trimmed.toLowerCase();
  if (existing.some((n) => n.trim().toLowerCase() === lower))
    return "A canvas with this name already exists";
  return null;
}

/** The workbench's suggested name: `canvas_<n>` with n one past the highest
 *  `canvas_<n>` already present, bumped until free (case-insensitive). */
export function generateCanvasName(existing: readonly string[]): string {
  const names = existing.map((n) => n.trim()).filter((n) => n.length > 0);
  const numbers = names
    .map((n) => /^canvas_(\d+)$/i.exec(n))
    .map((m) => (m ? parseInt(m[1], 10) : 0))
    .filter((n) => n > 0);
  let current = numbers.length > 0 ? Math.max(...numbers) + 1 : 1;
  const taken = (candidate: string) =>
    names.some((n) => n.toLowerCase() === candidate.toLowerCase());
  let candidate = `canvas_${current}`;
  while (taken(candidate)) {
    current += 1;
    candidate = `canvas_${current}`;
  }
  return candidate;
}
