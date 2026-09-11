// The Schedule hand-off: what travels to /tasks, and how the half-written
// draft survives the trip (T:11973-12030, openScheduler).
//
// Leaving unloads the document, so the draft is stashed in sessionStorage —
// SESSION and not local: a draft is a fact about this tab's errand, not about
// the machine. The key is verbatim.

/** T:12013. */
export function draftKey(file: string | null): string {
  return `fused:chatdraft:${file ?? ""}`;
}

/** Stash it, and never let a storage refusal break the navigation. */
export function stashDraft(file: string | null, draft: string): void {
  try {
    sessionStorage.setItem(draftKey(file), draft || "");
  } catch {
    // Storage denied — the draft just doesn't survive the trip.
  }
}

/** The other half of the round trip: read it back and SPEND it either way, so
 *  a second composer mount never re-fills over something newer (T:12105-12118). */
export function takeDraft(file: string | null): string {
  try {
    const key = draftKey(file);
    const saved = sessionStorage.getItem(key);
    if (saved === null) return "";
    sessionStorage.removeItem(key);
    return saved;
  } catch {
    return "";
  }
}

/**
 * ONE ATTACHMENT, AS THE TASK FORM STORES IT — the `attachments` row of POST
 * /api/schedule, and the same three fields `restoredAttachments` reads back off
 * a saved entry. `path` is always task-shots-resident: the chat's own copy lives
 * in its tempdir shots dir on a 12 h TTL, which the backend will not accept, so
 * the handoff re-uploads the bytes and carries the answer (owner E2E R1, F4
 * (2026-09-10)).
 */
export interface SchedAttachment {
  path: string;
  name: string;
  kind: "image" | "file";
}

/** The draft's key with a distinct suffix: the two halves of the handoff are
 *  spent independently, so they cannot share one row (owner E2E R1, F4
 *  (2026-09-10)). */
export function attachKey(file: string | null): string {
  return `fused:chatshots:${file ?? ""}`;
}

/** Same contract as `stashDraft`: a storage refusal costs the round trip, never
 *  the navigation. */
export function stashAttachments(
  file: string | null,
  list: readonly SchedAttachment[],
): void {
  try {
    if (!list.length) {
      sessionStorage.removeItem(attachKey(file));
      return;
    }
    sessionStorage.setItem(attachKey(file), JSON.stringify(list));
  } catch {
    // Storage denied — the chips just don't come back with the draft.
  }
}

/** SPENT on read, exactly like `takeDraft`: a second composer mount must not
 *  re-fill the tray over what the user has attached since. */
export function takeAttachments(file: string | null): SchedAttachment[] {
  try {
    const key = attachKey(file);
    const saved = sessionStorage.getItem(key);
    if (saved === null) return [];
    sessionStorage.removeItem(key);
    return parseAttachmentsParam(saved);
  } catch {
    return [];
  }
}

/**
 * The `&attachments=` param, or the stashed row, turned into a list — and it is
 * the same function for both because both are strings someone else wrote: a URL
 * a user edited, a sessionStorage row from an older build. BAD JSON IS AN EMPTY
 * LIST, never a throw: this runs inside the effect that opens the task form, and
 * a parse error there cost the whole handoff (owner E2E R1, F4 (2026-09-10)).
 */
export function parseAttachmentsParam(raw: string | null): SchedAttachment[] {
  if (!raw) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!Array.isArray(parsed)) return [];
  const out: SchedAttachment[] = [];
  for (const row of parsed) {
    if (!row || typeof row !== "object") continue;
    const a = row as Partial<SchedAttachment>;
    if (typeof a.path !== "string" || !a.path) continue;
    out.push({
      path: a.path,
      name: typeof a.name === "string" && a.name ? a.name : basenameOf(a.path),
      // Anything that is not the word "image" is a glyph, which is the same
      // floor `restoredAttachments` applies to a stored entry.
      kind: a.kind === "image" ? "image" : "file",
    });
  }
  return out;
}

/** The name a chip falls back to when the attachment carried none. */
export function basenameOf(path: string): string {
  const cut = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  return (cut === -1 ? path : path.slice(cut + 1)) || path;
}

/** `/tasks` (T:11968). */
export const SCHEDULE_URL = "/tasks";

export interface SchedulerLink {
  file: string | null;
  draft: string;
  sessionId: string;
  /** Where "Back to chat" has to land — the HOST's path, supplied by the host
   *  because a native chat has no `window.top` split to make (T:11979). */
  back: string;
  /** The deep link's second errand; no caller in the template passes it any
   *  more, but it is the link's shape rather than a private argument (T:12007). */
  editId?: string;
  /** What the composer's tray was holding, already copied into the task-shots
   *  dir. In the URL as well as the stash because the form is opened FROM the
   *  URL — the stash is the way back (owner E2E R1, F4 (2026-09-10)). */
  attachments?: readonly SchedAttachment[];
}

/** `new=1` is what makes the hop feel like one control rather than two: the
 *  form opens immediately, so the click lands on a filled-in dialog
 *  (T:12019-12029). Model / effort / approvals deliberately do NOT travel —
 *  a task runs unattended and the page owns those answers. */
export function schedulerUrl(link: SchedulerLink): string {
  return (
    `${SCHEDULE_URL}?new=1` +
    (link.editId ? `&edit=${encodeURIComponent(link.editId)}` : "") +
    `&target=${encodeURIComponent(link.file ?? "")}` +
    `&message=${encodeURIComponent(link.draft || "")}` +
    `&session_id=${encodeURIComponent(link.sessionId || "")}` +
    `&back=${encodeURIComponent(link.back)}` +
    // ONLY WHEN THERE IS SOMETHING TO SAY: a `&attachments=%5B%5D` on every
    // handoff is a URL that claims the trip carried files it did not.
    (link.attachments && link.attachments.length
      ? `&attachments=${encodeURIComponent(JSON.stringify(link.attachments))}`
      : "")
  );
}
