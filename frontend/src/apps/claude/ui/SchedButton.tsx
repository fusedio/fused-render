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
import {
  chatDraftKey,
  composerTaskDraft,
  draftSyncer,
  newTaskDraftId,
  taskDraftKey,
} from "@platform/lib/drafts";
import type { DraftAttachment, DraftSyncer } from "@platform/lib/drafts";
import { schedulerUrl, taskDraftUrl } from "../sched/scheduled";
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
  /**
   * THE RECORD IS WRITTEN AND THE COMPOSER IS DONE WITH IT — called once, on a
   * Continue whose write landed, immediately before the hop.
   *
   * The composer does not autosave any more (Composer's own header: nothing is
   * written while the reader is in the box), so this press is what CREATES the
   * record and these words existed only in that box until it did. They are the
   * card's now, so the box is emptied: one copy, one place to edit it — and a
   * cleared composer cannot meet its own unsent-message guard on the way out
   * with a question the reader has already answered by pressing Continue.
   */
  onHandedOff?(): void;
  /**
   * WHICH SET OF WORDS THE BOX IS ON — a counter the composer bumps every time
   * it empties itself (a Send, a Discard, an answered leave dialog).
   *
   * The hop is not instantaneous: the confirm closes first, then a round trip
   * per attachment, and only then is anything written. So the number is read
   * when Continue is pressed and read AGAIN immediately before the write is
   * stated, and a hop whose words have been spent in between says nothing at
   * all. Without it a Send's `markDeleted` was followed by this hop's `setText`
   * on the same key, and the sentence the reader had just sent came back as a
   * scheduled follow-up (Bugbot 4034977395).
   */
  episode?(): number;
  /**
   * A HOP IS IN FLIGHT — true the moment Continue is accepted, false again the
   * moment it is over (aborted, refused, or navigating).
   *
   * `leaving` below only ever blocked a SECOND Continue; the box beside it
   * stayed fully live, so Send, Discard and a leave-dialog answer could each
   * spend or re-file the same words while the copies ran. The composer freezes
   * itself on this — one gesture at a time on one set of words.
   */
  onHopChange?(inFlight: boolean): void;
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
 *  where the hop is spelt in every reader's head, and in the tests.
 *  `taskDraftUrl` is its twin for the draft this button MINTS. */
export { schedulerUrl, taskDraftUrl };

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
  onHandedOff,
  episode,
  onHopChange,
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
  /**
   * THE FREEZE, SAID ONCE (Bugbot 4034977395).
   *
   * A hop that has begun owns these words until it ends, and `leaving` on its
   * own only ever said so to this button's second press. So every place that
   * moves it also dims the trigger — a live calendar in a frozen row is a lie —
   * and tells the composer, which shuts Send, the box and its own leave dialog
   * for the same window. One call, because three flags that can disagree is a
   * second copy of the bug rather than a fix for it.
   */
  const [hopping, setHopping] = useState(false);
  const hopChange = useRef(onHopChange);
  hopChange.current = onHopChange;
  const hold = useCallback((on: boolean) => {
    leaving.current = on;
    setHopping(on);
    hopChange.current?.(on);
  }, []);

  const go = useCallback(() => {
    // The confirm can OUTLIVE the press that opened it — the schedule poll may
    // block this chat while the question is still on screen — so the last word
    // on whether a task may be made from here is read HERE (T:12091).
    if (disabled || leaving.current) return;
    hold(true);
    const text = draft().trim();
    const tray = attachments?.() ?? [];
    setOpen(false);
    // The chips whose bytes actually exist — `take()`'s rule, and the only
    // ones either road below can carry (a pending upload names no file yet).
    const carry = tray.filter((a) => !a.pending && !!a.view);
    /**
     * THE WORDS THIS PRESS IS ABOUT, named by the episode the box was on when
     * it happened — and asked for again at the last moment before anything is
     * written, because everything in between is a round trip.
     */
    const era = episode?.() ?? 0;
    /** Have they been spent or re-filed since? The episode says the box emptied
     *  under this hop (a Send, a Discard, an answered dialog); `isGone` says the
     *  key's one writer has a delete out for the record — and a `setText` behind
     *  that delete is the resurrection this guard exists to stop. */
    const spent = (sync: DraftSyncer): boolean =>
      (episode?.() ?? 0) !== era || sync.isGone();
    /** NOTHING WRITTEN, NOTHING LEFT, AND THE READER TOLD WHY — every abort
     *  from here down is those same three things, so it is one sentence. */
    const stop = (why: string): void => {
      hold(false);
      notify({ title: why, tone: "error" });
    };
    /**
     * AND A FILE THAT DID NOT COPY STOPS THE WHOLE HOP (Bugbot 4034977406).
     *
     * `copyToTaskShots` is `allSettled` and hands back only what landed, so a
     * picture whose upload 500ed simply vanished — and on a session the hop then
     * `setText`s that shorter list straight over the attachments the record was
     * already holding. "Save as draft" has always failed closed on exactly this
     * (`saveDraftNow`); scheduling is the same bargain, and a task the reader
     * cannot see their file on is not the task they asked for.
     */
    const short = (carried: DraftAttachment[]): boolean =>
      carried.length !== carry.length;
    const PARTIAL = "Could not attach every file — nothing was scheduled";
    const SPENT = "Those words already left the box — nothing was scheduled";
    /**
     * A CHAT THAT HAS NEVER BEEN SENT MINTS A NEW DRAFT, EVERY PRESS (Akshil,
     * 2026-09-16).
     *
     * This used to write `new:<file>` — one record per FOLDER — and hop to it,
     * so a reader who scheduled one thing, walked back, and scheduled a second
     * thing out of the same folder replaced the first without being told. There
     * is no conversation here for a single record to be the unsent message of;
     * there is an Upcoming task, and there can be as many of those as the
     * reader writes. So the press mints `draft:<id>` (`composerTaskDraft`,
     * exactly the record the "+ New task" card makes) and opens the card on it
     * through the arm every draft row already presses (`taskDraftUrl`).
     *
     * NOTHING TO RE-STATE AFTERWARDS, which is why this road is shorter than
     * the session one below: the id is brand new, nobody else holds it, and the
     * composer behind this popover writes nothing of its own — so the record
     * cannot be overtaken between the write and the hop.
     */
    if (!sessionId) {
      const id = newTaskDraftId();
      // AN EMPTY BOX MINTS NOTHING. A draft with no words and no files is a row
      // in Upcoming saying nothing, so the press opens a blank card on this
      // folder instead and the reader fills it in there.
      if (!text && !carry.length) {
        hold(false);
        onNavigate?.(schedulerUrl("", back, file ?? ""));
        return;
      }
      const mint = (carried: DraftAttachment[]): void => {
        if (short(carried)) {
          stop(PARTIAL);
          return;
        }
        const sync = draftSyncer(taskDraftKey(id));
        if (spent(sync)) {
          stop(SPENT);
          return;
        }
        sync.setTask(composerTaskDraft(text, file ?? "", carried));
        void sync.handoff().then((out) => {
          if (!out.ok) {
            stop("Could not save that draft — you are still in the chat");
            return;
          }
          // The record is the card's now: this page stops wanting anything for
          // it, and the box it came out of is emptied (one copy, one place).
          sync.forget();
          onHandedOff?.();
          hold(false);
          // `hop`: this is a Schedule PRESS, not a draft row — the card opens
          // on now+2m and planning, exactly as the session hop's `?new=1` does
          // (Bugbot 4028344051).
          onNavigate?.(taskDraftUrl(id, back, true));
        });
      };
      if (!carry.length) {
        mint([]);
        return;
      }
      void copyToTaskShots(tray)
        .catch((): DraftAttachment[] => [])
        .then(mint);
      return;
    }
    // THE KEY, NOT THE WORDS (design §1). Nothing has been written under it yet
    // — the composer autosaves nothing any more — so this press is what CREATES
    // the record, and it is the record the task form is about to go on editing.
    const key = chatDraftKey(sessionId, file);
    const leave = (): void => {
      hold(false);
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
    if (!text && !carry.length) {
      leave();
      return;
    }
    /**
     * THE HOP SAYS WHAT THE RECORD SHOULD HOLD AND WAITS FOR THE SERVER TO HOLD
     * IT (design "one record", §1; the syncer's `handoff`).
     *
     * It used to WRITE — its own PUT, beside the composer's own PUT, on the same
     * key — and then the two had to be ordered by hand: stand the box's autosave
     * down, wait for whatever it had on the wire, state the version that write
     * made, retry once on a 409. Every one of those steps existed because there
     * were two writers. There is one now: this states the desired state
     * (the words, plus the attachments copied into the task-shots dir) and
     * `handoff` resolves once the server matches it, whatever was in flight when
     * Continue was pressed and whatever the reader typed during the copies.
     *
     * THE NAVIGATION IS ITS ANSWER, and that has not changed. The card on the
     * other side SEEDS FROM `GET /api/drafts`, so leaving before the record is
     * written is arriving at a card holding the words as they were 600 ms ago —
     * or holding nothing at all on a first hop.
     *
     * A REFUSED WRITE KEEPS THE READER IN THE CHAT. Navigating with nothing
     * saved is the same empty card by another road, and this side still has the
     * words: staying put with a toast is the only answer that loses nothing.
     */
    /**
     * …AND THE HOP HAS TO BE THE LAST WRITER OF THE ATTACHMENTS, which stating
     * them once does not make it.
     *
     * The copies below are a round trip per file, and the box behind this
     * popover is still live: a keystroke landing while they run makes the
     * composer's own autosave the newer statement, and its attachments are the
     * CHAT's tray paths — a tempdir on a 12 h TTL that `POST /api/schedule`
     * refuses outright. The card would then open on paths it cannot schedule.
     *
     * So when the record the handoff settled on is not holding the carried
     * files, the hop says it again — with the words as they now stand, because
     * the newest keystrokes are the reader's and only the files are this
     * gesture's to insist on. Once, and then it leaves either way: a reader
     * still typing into the box they are hopping out of is a race nothing can
     * win, and the words are safe on the record whichever round ends it.
     */
    const hand = (
      carried: DraftAttachment[],
      words: string = text,
      again = false,
    ): void => {
      if (short(carried)) {
        stop(PARTIAL);
        return;
      }
      const sync = draftSyncer(key);
      if (spent(sync)) {
        stop(SPENT);
        return;
      }
      sync.setText(words, carried);
      void sync.handoff().then((out) => {
        if (out.ok) {
          const now = sync.wants();
          const kept = now?.attachments ?? [];
          const same = kept.length === carried.length
            && carried.every((one, i) => kept[i]?.path === one.path);
          if (!again && !same) {
            hand(carried, now?.text ?? words, true);
            return;
          }
          // THE BOX IS EMPTIED IN THIS TICK, before the hop — and only on the
          // road that actually wrote something. The empty-composer branch above
          // leaves without a write, so there is nothing there to hand off and
          // nothing of the reader's to throw away.
          onHandedOff?.();
          leave();
          return;
        }
        stop("Could not save that draft — you are still in the chat");
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
  }, [disabled, draft, attachments, file, sessionId, back, episode, hold, onHandedOff,
      onNavigate]);

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
            // AND IT IS OFF WHILE ITS OWN HANDOFF IS OUT (`hopping`). `go`
            // refuses a second press either way; this is the half the reader can
            // see, and it is the same `.c-schedbtn:disabled` the block draws.
            disabled={disabled || hopping}
          >
            <CalendarIcon />
          </button>
        }
      />
      <SchedConfirm onGo={go} onCancel={cancel} />
    </Popover>
  );
}
