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
    `&back=${encodeURIComponent(link.back)}`
  );
}
