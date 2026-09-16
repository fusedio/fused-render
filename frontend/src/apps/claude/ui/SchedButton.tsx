// "Schedule this as a task" — the HANDOFF, and the handoff is one button
// (T:11940-12035, 12075-12128).
//
// The composer used to try to schedule on its own; everything past a bare
// deferral is the /tasks page's form, so what travels is what the page cannot
// know and this composer always does: WHICH DRAFT this is, and where to come
// back to. That is the whole of the URL now — `?new=1&draft=<chat key>&from=…`
// — because the words themselves no longer travel at all.
//
// WHY NOTHING TRAVELS ANY MORE (design "Drafts: one record, one key, versioned,
// pushed", §1). The hop used to carry the sentence three ways at once: a
// sessionStorage stash, a `?message=` param, and a `draft:<id>` the task form
// minted on arrival — three copies of one half-written thing, and every bug in
// this feature was two of them disagreeing. The draft IS a server record with a
// key; the key is all that has to cross a navigation, and both ends open the
// same record.
//
// WHAT THE BUTTON STILL DOES BEFORE LEAVING is copy the tray into the task-shots
// dir (`copyToTaskShots`) and save that onto the record, because a chat
// attachment's path is a tempdir on a 12 h TTL and `POST /api/schedule` refuses
// any path outside `schedule.shots_dir()`. The bytes are the one thing a key
// cannot stand in for.
import { useCallback, useEffect, useRef, useState } from "react";
import { Popover, PopoverTrigger } from "@platform/shadcn/ui/popover";
import { rawUrl, uploadTaskShot } from "@platform/lib/api";
import { notify } from "@platform/lib/notifications";
import type { Attachment } from "../shots/types";
import { SchedConfirm } from "./SchedConfirm";
import { chatDraftKey, saveChatDraft } from "@platform/lib/drafts";
import type { DraftAttachment } from "@platform/lib/drafts";
import { schedulerUrl } from "../sched/scheduled";
import { useDismissOnWindow } from "./useDismissOnWindow";

export interface SchedButtonProps {
  file: string | null;
  /** "" on the landing page, which is correct rather than missing: there is no
   *  session yet, and the store reads "" as "start a new one" (T:12022). */
  sessionId: string;
  /** Read at CONTINUE time and not at open time, so a paste made with the
   *  confirm already up still travels (T:12094). */
  draft(): string;
  /** The tray, read at CONTINUE time for the same reason `draft` is: a picture
   *  attached while the confirm was up is part of what the user is scheduling
   *  (owner E2E R1, F4 (2026-09-10)). Absent on a composer with no tray. */
  attachments?(): readonly Attachment[];
  /** Where "Back to chat" has to land — the host's own path. */
  back: string;
  /** A pending scheduled message shuts this door as well as the composer's
   *  (`schedBlocked`, PR4). Never true for the landing card (T:16851). */
  disabled?: boolean;
  /**
   * WHY it is refusing, when the reason is one the reader can act on — the nav
   * lock's `NAV_LOCKED_REASON` (T:6896's "Finish or discard the notes first"),
   * or the schedule block's own sentence (T:17237-17246).
   *
   * It rides the `title` AND the spoken name, because `disabled` takes the
   * button out of tab order: the title is then unreachable by keyboard and the
   * name is all a reader browsing this row will hear. The name stays FIRST —
   * what the control is, then why it is off — so the button is still
   * identifiable while it is refusing. The Back button's twin refusal says the
   * same sentence the same two ways (`ClaudeChat`).
   */
  disabledReason?: string;
  /** Cancel puts the focus back in the box the draft is in. */
  onCancel?(): void;
  onNavigate?(url: string): void;
}

/** T:16860-16861 — the pristine wording, in ONE place: a re-word here cannot
 *  leave a refusal restoring a tooltip nobody writes any more. */
export const SCHED_LABEL = "Schedule this as a task";

/** T:4184-4188 / 4243-4247, verbatim. */
function CalendarIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden="true"
    >
      <rect
        x="2"
        y="3.2"
        width="12"
        height="10.6"
        rx="1.6"
        stroke="currentColor"
        strokeWidth="1.3"
      />
      <path
        d="M2 6.6h12M5.4 1.8v2.6M10.6 1.8v2.6"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
      />
    </svg>
  );
}

/**
 * THE CHAT'S ATTACHMENTS, COPIED INTO THE TASK-SHOTS DIR.
 *
 * The backend refuses any `attachments` path outside `schedule.shots_dir()`
 * (~/.fused-render/task-shots), and a chat attachment lives in the claude
 * template's tempdir-rooted shots dir on a 12 h TTL — so the path itself cannot
 * travel. The bytes do: read the file back through /api/fs/raw (the same URL the
 * chip's own thumbnail is drawn from) and put it up through the endpoint the
 * task form's own drop and paste already use, which is what makes the two kinds
 * of attachment indistinguishable once they are on the card.
 *
 * PENDING CHIPS ARE NOT PART OF THIS, for `take()`'s reason: their bytes are
 * still on their way, so there is nothing to copy.
 *
 * ONE FAILURE COSTS ONE ATTACHMENT. `allSettled` and not `all`: a pruned file or
 * a refused upload must not take the other two with it, and must never be the
 * reason the button does nothing at all — the words and the folder are the
 * handoff's point and they still travel (owner E2E R1, F4 (2026-09-10)).
 */
/** The name a chip falls back to when the attachment carried none. */
export function basenameOf(path: string): string {
  const cut = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  return (cut === -1 ? path : path.slice(cut + 1)) || path;
}

/** The hop's URL, built where the rows can reach it too — see
 *  `sched/scheduled.schedulerUrl`. Re-exported here because this button is
 *  where the hop is spelt in every reader's head, and in the tests. */
export { schedulerUrl };

export async function copyToTaskShots(
  items: readonly Attachment[],
): Promise<DraftAttachment[]> {
  const carry = items.filter((a) => !a.pending && !!a.view);
  if (!carry.length) return [];
  const done = await Promise.allSettled(
    carry.map(async (att): Promise<DraftAttachment> => {
      const view = att.view as string;
      const name = att.name || basenameOf(view);
      const res = await fetch(rawUrl(view));
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      const up = await uploadTaskShot(new File([blob], name, { type: blob.type }));
      return { path: up.path, name, kind: up.kind === "image" ? "image" : "file" };
    }),
  );
  // IN THE TRAY'S OWN ORDER, which `allSettled` preserves: the chips on the task
  // card then read left to right the way the chips in the composer did.
  return done.flatMap((r) => (r.status === "fulfilled" ? [r.value] : []));
}

export function SchedButton({
  file,
  sessionId,
  draft,
  attachments,
  back,
  disabled,
  disabledReason,
  onCancel,
  onNavigate,
}: SchedButtonProps) {
  const [open, setOpen] = useState(false);
  const why = disabled && disabledReason ? SCHED_LABEL + " — " + disabledReason : SCHED_LABEL;

  /**
   * T:17253 — A CONFIRM CAN ALREADY BE UP WHEN THE BLOCK LANDS. "Schedule this
   * as a task?" is then a question about a button that has just died, and its
   * Continue would hit the guard in `go` and do nothing visible. Take it down
   * instead, so what the reader ends up looking at is the banner.
   *
   * Only on the way IN: closing a confirm the user opened the moment the block
   * lifted would be the block reaching past its own end.
   */
  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  /**
   * A HANDOFF IS ALREADY LEAVING. The copy below is a round trip per attachment,
   * so Continue is no longer instantaneous, and a second Continue in that window
   * would upload every file twice and navigate twice. A ref and not state, for
   * `useAttachments`' `busy` reason: a boolean in state is only read as of the
   * render that closed over it, and both presses of a double click are in one
   * tick.
   */
  const leaving = useRef(false);

  const go = useCallback(() => {
    // The confirm can OUTLIVE the press that opened it — the schedule poll may
    // block this chat while the question is still on screen — so the last word
    // on whether a task may be made from here is read HERE (T:12091).
    if (disabled || leaving.current) return;
    leaving.current = true;
    const text = draft().trim();
    const tray = attachments?.() ?? [];
    setOpen(false);
    // THE KEY, NOT THE WORDS (design §1). This is the record the composer's own
    // autosave has been writing under, and it is the record the task form is
    // about to go on editing.
    const key = chatDraftKey(sessionId, file);
    const leave = (): void => {
      leaving.current = false;
      // …AND THE FOLDER THIS CHAT IS IN. A `new:<file>` key spells it; a session
      // key does not, and without it the card opened on the reader's home
      // (Akshil, 2026-09-16). This composer knows the path — it is mounted on
      // it — so it says so.
      onNavigate?.(schedulerUrl(key, back, file ?? ""));
    };
    // NOTHING TO SAVE, SO NOTHING TO WAIT FOR. An empty composer stored no
    // draft, and writing an empty record here would be a DELETE — which on a
    // chat that already carries a bound form (the ✎ chip) would throw that
    // form's time, repeat and model away on the way to a card that was about to
    // show them. So an empty hop simply leaves, in this tick, and the card opens
    // on whatever the record already holds.
    const carry = tray.filter((a) => !a.pending && !!a.view);
    if (!text && !carry.length) {
      leave();
      return;
    }
    /**
     * THE SAVE IS AWAITED, AND THE NAVIGATION IS ITS ANSWER (Bugbot, PR #1180).
     *
     * The hop used to fire the PUT and navigate in the same tick, on the theory
     * that a round trip the reader cannot see is a round trip not worth waiting
     * for. It is, because the card on the other side SEEDS FROM `GET
     * /api/drafts`: the two requests race, and when the GET won, the reader
     * arrived at a card holding the words as they were 600 ms ago — or holding
     * nothing at all on a first hop. Waiting costs one round trip on a
     * navigation; losing costs the sentence the hop was for.
     *
     * A REFUSED WRITE KEEPS THE READER IN THE CHAT. Navigating with nothing
     * saved is the same empty card by another road, and this side still has the
     * words: staying put with a toast is the only answer that loses nothing.
     *
     * ONE RETRY ON A CONFLICT, the same single retry `useAutosave` makes and for
     * the same reason: the composer's own debounced PUT can be on the wire with
     * the same `If-Match`, and one of the two is refused. `saveChatDraft` has
     * already taken the server's version by the time it answers, so the second
     * attempt states one that exists.
     */
    const hand = (carried: DraftAttachment[]): void => {
      void saveChatDraft(key, text, carried)
        .then((out) => (out.ok || !("conflict" in out) ? out : saveChatDraft(key, text, carried)))
        .then((out) => {
          if (out.ok) {
            leave();
            return;
          }
          leaving.current = false;
          notify({
            title: "Could not save that draft — you are still in the chat",
            tone: "error",
          });
        });
    };
    // AN EMPTY TRAY HAS NOTHING TO COPY. The round trip per file below is the
    // slow half; this hop is one PUT, which is the overwhelmingly common one.
    if (!carry.length) {
      hand([]);
      return;
    }
    // THE TRAY IS NOT EMPTIED. `take()` is the send's gesture; this one is a
    // handoff the user can walk back from with "Back to chat", and a tray
    // cleared here would leave them with neither copy.
    //
    // AND THE COPIES ARE WRITTEN ONTO THE RECORD, because that is where the card
    // reads them from now (there is no `?attachments=` param any more) and
    // because the chat's own paths expire: a tempdir on a 12 h TTL that
    // `POST /api/schedule` refuses outright. The record ends up holding the
    // task-shots paths, which the composer can still draw from if the reader
    // walks back.
    void copyToTaskShots(tray)
      .catch((): DraftAttachment[] => [])
      .then(hand);
  }, [disabled, draft, attachments, file, sessionId, back, onNavigate]);

  const cancel = useCallback(() => {
    setOpen(false);
    onCancel?.();
  }, [onCancel]);

  /**
   * THE OTHER HALF OF THE DISMISSAL CONTRACT (T:12146, 12149), and it matters
   * more here than on any pill: this popover's Continue NAVIGATES AWAY FROM THE
   * CONVERSATION. An orphaned confirm floating over a pane the reader has since
   * clicked into is one keypress from leaving the chat — and P4-15 just made
   * that keypress Enter.
   *
   * Closed WITHOUT `onCancel`: a blur is not a reader answering the question,
   * so there is no focus to hand back to a draft nobody left.
   */
  const dismiss = useCallback(() => setOpen(false), []);
  useDismissOnWindow(open, dismiss);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        render={
          <button
            type="button"
            className="c-pill c-schedbtn"
            aria-label={why}
            title={disabled && disabledReason ? disabledReason : SCHED_LABEL}
            disabled={disabled}
          >
            <CalendarIcon />
          </button>
        }
      />
      <SchedConfirm onGo={go} onCancel={cancel} />
    </Popover>
  );
}
