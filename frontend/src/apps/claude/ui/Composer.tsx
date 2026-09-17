// The composer: one textarea, three pills that describe the run, the two ways a
// draft leaves the box (schedule / send), and the row's own measured fit.
//
// Sources: T:4152-4200 (chat markup), T:17870-17954 (submitChat and the key
// bindings), T:12157-12497 (the fitting ladder and the footnote's budget),
// T:12105-12118 (the scheduler's draft round trip). Inventory 03 §C/§G.
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useAutoGrow } from "@platform/lib/autoGrow";
import {
  chatDraftKey,
  composerTaskDraft,
  draftSyncer,
  draftVersion,
  forgetDraftVersion,
  fetchChatDraft,
  newTaskDraftId,
  peekDraftSyncer,
  taskDraftKey,
  useAutosave,
  type ChatDraft,
  type DraftAttachment,
} from "@platform/lib/drafts";
import { notify } from "@platform/lib/notifications";
import { registerLeaveGuard } from "@platform/lib/router";
import { announceTasksChanged } from "@platform/lib/tasksChanged";
import { announceDraftsGone, dropListingKeys, onDraftChange } from "@shell/tasksPulse";
import "../styles/composer.css";
import type { PermissionMode } from "../protocol/types";
import type { RunStatus, SendOptions } from "../protocol/controller-api";
import type { Attachment } from "../shots/types";
import {
  applyLead2,
  fitFlags,
  fitSelect,
  measureRowNeed,
  pickRowFit,
  type RowFit,
} from "./fit";
import { PERMISSION_SHORT } from "./composer-defaults";
import { EffortSelect } from "./EffortSelect";
import { ModelSelect } from "./ModelSelect";
import { PermissionSelect } from "./PermissionSelect";
import { copyToTaskShots, SchedButton } from "./SchedButton";

/** T:4227 / T:4156 — the box's own placeholder, verbatim. The chat one names
 *  who is being replied to; the landing one names the errand. */
export const CHAT_PLACEHOLDER = "Reply to Claude…";
export const HOME_PLACEHOLDER = "Ask Claude…";

/** The disabled Send's tooltip when the caller hands no reason (P4R1-2). The
 *  real sentence is `schedBlockReason`'s, threaded through `blockedReason`; this
 *  is the floor, so a dead control is never a dead control with nothing to say. */
export const BLOCKED_SEND_TITLE = "Waiting on a scheduled message";

/** HOW LONG THE READER HAS TO TAKE IT BACK. The platform's own 2.5 s is the
 *  window for a confirmation nobody has to act on; this card asks a question,
 *  and a reader who has just landed on another page needs long enough to read
 *  it, find the button and press it (Akshil, 2026-09-17: "for ~5 s"). */
export const UNDO_TOAST_MS = 5000;

/**
 * THE TOAST'S UNDO — the draft this page just wrote, taken back.
 *
 * MODULE SCOPE, NOT A CALLBACK, because by the time it is pressed the composer
 * that saved is usually gone: the press that made the save was a navigation.
 * Everything it touches outlives the component — the syncer for the key, the
 * held listing, the drafts channel.
 *
 * THE DELETE GOES THROUGH THE KEY'S OWN WRITER, exactly as the List's trash
 * does (`ScheduleTaskViews.dropDraft`): a bare DELETE fired beside a pending
 * PUT is the pair that ordering by hand never got right, and the syncer states
 * the version it is deleting. `removed` rather than `ok` for that function's
 * reason too — `ok` asks whether the server holds what this page last WANTED,
 * and the answer must be about the delete.
 *
 * THE ROW GOES FIRST (`dropListingKeys`), because the reader pressed a button
 * and a row that lingers until a poll catches up reads as a press that did
 * nothing. If the DELETE then refuses, the row is NOT put back by hand: the
 * draft is still on the server, `listingGen` was never bumped, and the next
 * listing read paints it again — while the toast says the undo did not take.
 */
async function undoSavedDraft(key: string): Promise<void> {
  dropListingKeys([key]);
  const sync = draftSyncer(key);
  sync.markDeleted();
  const gone = (await sync.handoff()).removed;
  if (!gone) {
    notify({ title: "Could not undo — the draft is still in Upcoming", tone: "error" });
    return;
  }
  sync.forget();
  // …and the editors that record may ALSO be open in hear it now rather than on
  // the next long-poll — the Tasks card the reader may have opened on it.
  announceDraftsGone([key]);
  announceTasksChanged();
}

/** Where the composer's own controls open: shadcn/Base UI popovers and menus
 *  (`platform/shadcn/ui/popover`, `dropdown-menu`) and any dialog. Focus
 *  landing in one of these is still "in the composer" for the idle fold. */
const POPUP_SURFACE =
  '[data-slot="popover-content"], [data-slot^="dropdown-menu"], [role="menu"], [role="listbox"], [role="dialog"]';


/** Everything the three pills need, from `useComposerDefaults`. */
export interface ComposerControls {
  model: string;
  effort: string;
  permission: PermissionMode;
  setModel(value: string): void;
  setEffort(value: string): void;
  setPermission(value: PermissionMode): void;
}

// ---- the fitting ladder ----------------------------------------------------

/** Stamp one candidate onto the row and refit its pills, so the next
 *  measurement prices THAT candidate (T:12327-12344). Stage 2 is a DOM write
 *  and not a class: what shortens is the selected option's text, which is the
 *  only thing an appearance:none select paints. */
function applyFit(row: HTMLElement, fit: RowFit): void {
  const flags = fitFlags(fit);
  row.classList.toggle("is-compact", flags.compact);
  row.classList.toggle("is-tight", flags.tight);
  row.classList.toggle("is-stack", flags.stack);
  for (const select of Array.from(
    row.querySelectorAll<HTMLSelectElement>("select.c-perm-sel"),
  )) {
    for (const option of Array.from(select.options)) {
      const full = option.dataset.full || option.textContent || "";
      option.textContent = flags.compact
        ? PERMISSION_SHORT[option.value as PermissionMode] || full
        : full;
    }
  }
  // A shortened label inside the old box leaves exactly the dead space
  // fitSelect exists to remove, and `.is-tight` bakes its paddings into the
  // fitted widths — so every pill is refitted on every pass.
  for (const select of Array.from(
    row.querySelectorAll<HTMLSelectElement>("select.c-pill"),
  )) {
    fitSelect(select);
  }
}

/**
 * The verdict, recomputed from scratch on every pass — so widening undoes
 * itself with no state to get stale.
 *
 * ONE ResizeObserver, and it watches the COLUMN rather than the row: what these
 * verdicts write changes the composer's HEIGHT, so an observer on the row would
 * be re-triggered by its own answer (T:12448-12454). `revision` stands in for
 * T's MutationObserver on the class/`hidden` flips a long way from this row —
 * in React those arrive as a re-render, so the caller bumps it instead.
 */
export function useRowFit(
  rowRef: React.RefObject<HTMLElement | null>,
  columnRef: React.RefObject<HTMLElement | null> | undefined,
  revision: unknown,
): RowFit {
  const [fit, setFit] = useState<RowFit>("full");
  const current = useRef<RowFit>("full");

  useLayoutEffect(() => {
    const row = rowRef.current;
    if (!row) return;
    const run = () => {
      const box = row.clientWidth;
      if (!box) return; // the other view's composer: no box, nothing to decide
      const verdict = pickRowFit(box, (candidate) => {
        applyFit(row, candidate);
        return measureRowNeed(row);
      });
      applyFit(row, verdict);
      applyLead2(row, verdict === "stack");
      if (current.current !== verdict) {
        current.current = verdict;
        setFit(verdict);
      }
    };
    run();
    if (typeof ResizeObserver === "undefined") return;
    // MEASURED OUT OF THE OBSERVER'S OWN CALLBACK. The observed box is the chat
    // COLUMN and the writes land on the row inside it, so a content-sized
    // column would make this re-enter itself — which the browser reports as
    // "ResizeObserver loop completed with undelivered notifications", and
    // design.md §9's clean-console gate fails on it. One frame's delay costs
    // nothing here (`run` is idempotent and only writes on a changed verdict)
    // and takes the write out of the callback entirely.
    let frame: number | null = null;
    const observer = new ResizeObserver(() => {
      if (frame !== null) return;
      frame =
        typeof requestAnimationFrame === "function"
          ? requestAnimationFrame(() => {
              frame = null;
              run();
            })
          : (setTimeout(() => {
              frame = null;
              run();
            }, 0) as unknown as number);
    });
    observer.observe(columnRef?.current ?? row.parentElement ?? row);
    return () => {
      if (frame !== null && typeof cancelAnimationFrame === "function") cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, [rowRef, columnRef, revision]);

  return fit;
}

// ---- the card both composers are ------------------------------------------

export interface ComposerCardProps {
  variant: "chat" | "home";
  file: string | null;
  sessionId: string;
  controls: ComposerControls;
  status: RunStatus;
  /** Follow-ups typed while a run is live that have not been acknowledged. */
  queued?: string[];
  /**
   * THE PROJECT QUEUE IS ON (prefs `queue.enabled`), which TAKES THE NOTE BELOW
   * AWAY.
   *
   * A follow-up typed into this chat's own running turn is held by the live host
   * for the seconds the turn has left. Under the queue that is the ONE waiting
   * state with nothing to say about it: there is no scheduler entry, so nothing
   * to be behind, nothing to run next and nothing to delete — and the bubbles are
   * already in the transcript above, in order, exactly where the reader put them.
   * A footnote counting them is a third piece of chrome for a state that resolves
   * itself, in a pane that now says "waiting" about messages that genuinely are
   * (Akshil, 2026-09-12).
   *
   * FLAG OFF THE NOTE STAYS, untouched: there is no other ink in that build
   * saying a follow-up went somewhere, and a line that has gone invisible is
   * worse than 24px of composer card (the note's own original argument).
   */
  queueOn?: boolean;
  /** A fresh turn. */
  onSend(text: string, opts: SendOptions): void;
  /** Into the live run's inbox (T:16024). Falls back to `onSend` when absent. */
  onFollowUp?(text: string): void;
  onStop(): void;
  /** Notes or pictures alone are sendable, with no words at all (T:17903). */
  hasAttachments?: boolean;
  /**
   * A CHIP IS STILL ATTACHING, so nothing leaves this box yet — the camera's
   * own `shotBusy` gate (T:11203), one level up.
   *
   * `hasAttachments` counts in-flight placeholders (they are chips the user can
   * see), but `take()` deliberately leaves a `pending` item in the tray for the
   * NEXT message. Sendable-because-of-chips plus taken-without-them is a
   * wordless Enter dispatching an EMPTY send, and a worded one going out
   * without the files it was written about (Bugbot, PR #1064).
   */
  attachPending?: boolean;
  /** A pending scheduled message closes the composer (`schedBlocked`, PR4). */
  blocked?: boolean;
  blockedPlaceholder?: string;
  /** WHY the box is shut, for the calendar button's tooltip and spoken name —
   *  ONE sentence with one author, so a reader refused by the button reads the
   *  same words as the card six pixels above it (T:17232-17250). */
  blockedReason?: string;
  /**
   * `annNavLocked` — a comment round or a walkthrough owns this page. T guards
   * every `.schedbtn` on `schedBlocked() || annNavLocked()` (T:12075, T:12099,
   * via `querySelectorAll`, so BOTH composers) and T:1470 dims both, because
   * leaving for `/tasks` mid-round strands the notes.
   *
   * Its own prop rather than folded into `blocked`, because the two are scoped
   * differently: `blocked` is chat-only (T:16851 — the landing card's copy is
   * never blocked, since there is no session holding queued work), while a
   * nav lock is about THIS PAGE and so applies to the landing composer too.
   */
  navLocked?: boolean;
  /** Why, for the seat's `title` and accessible name — `NAV_LOCKED_REASON`
   *  (T:6896). A refusal the reader cannot see the cause of is the failure
   *  Bugbot #1046 closed on the Back button's twin. */
  navLockedReason?: string;
  autoFocus?: boolean;
  // `focusRequest` IS GONE (Bugbot review of caef75eb1, LOW). It was the seat
  // for "a never-sent chat's row was pressed, put the caret in the box", and
  // that road went with the draft MOVE out of the Recent list — a press now
  // opens the record where it already lives. No host has passed it since, so
  // what was left was a prop, a `setGestured`, an effect and a ref that nothing
  // could raise.
  /** The landing card's kind-dependent placeholder (`homePlaceholderFor`,
   *  T:5392). Unset keeps the markup's own kind-free wording. */
  placeholder?: string;
  /**
   * Follow-ups the CLI never delivered, handed BACK to the box they were typed
   * in (`still_queued`, T:15911). `seq` is what re-delivers the same text (two
   * stops can strand the same words), and the text is APPENDED rather than
   * assigned by default, because the user may already be typing the next
   * thing.
   *
   * `replace: true` is the OTHER caller of this seat — a draft row pressed in
   * the Recent list (`ClaudeChat.onFillDraft`, bug report 2026-09-15: "make
   * sure text before it in composer is cleaned and only draft text is
   * there"). That row's whole content is about to become the box's whole
   * content, not a second sentence appended to whatever was there, so it asks
   * for the replacement this seat did not used to offer — including down to
   * an empty string, for a draft that is pictures with no words at all.
   */
  restore?: { text: string; seq: number };
  /** The textarea itself, for a host modal's `initialFocus` (TaskPeek, whose
   *  target used to be the iframe element). */
  boxRef?: React.MutableRefObject<HTMLTextAreaElement | null>;
  /**
   * T:8505 `annAutoSubmit` — the ONE programmatic send. A spoken walkthrough
   * ends by seeding the box with what was said before the first click and then
   * pressing send itself; ✓ Done does the same with no words at all.
   *
   * T reaches for `form.requestSubmit()`, which cannot work here: the composer's
   * text is React state and only this component can read it. So the seat is
   * handed out instead — filled while this composer is mounted, nulled when it
   * goes, which is also what makes it honest about WHICH composer is on screen
   * (home or chat, never both).
   *
   * SEEDED, and that is finding 3's whole fix: the caller hands the words IN
   * (`submitRef.current(intro)`) and they are folded into the box's own text
   * for this one send. They used to be written through the `restore` seat — a
   * STATE write — and the send pressed from a `setTimeout(0)`, which could run
   * before React had applied it: the notes went out with an empty box and the
   * sentence that introduced them was lost (Bugbot, PR #1074). The answer says
   * whether the send happened, so a caller whose send was refused can put the
   * words back in the box instead of dropping them.
   */
  submitRef?: React.MutableRefObject<((seed?: string) => boolean) | null>;
  /**
   * THE SEND WINDOW'S LATCH, in its two forms.
   *
   * `busyRef` is read SYNCHRONOUSLY in `submit`: the parent takes the latch
   * inside `onSend`, in the same tick as the call below, so a second Enter that
   * arrives before React has re-rendered still sees it — which is exactly the
   * race that let two submits into one send window (Bugbot, PR #1074).
   * `sendBusy` is the same fact as a prop, for the button's `disabled`.
   */
  busyRef?: React.MutableRefObject<boolean>;
  sendBusy?: boolean;
  /** The column whose width the ladder measures against. */
  columnRef?: React.RefObject<HTMLElement | null>;
  /** Chips above the box: attachments (PR2), annotations (PR3). */
  chips?: ReactNode;
  /**
   * THE TRAY ITSELF, for the scheduler handoff's other half. `chips` is what it
   * LOOKS like and `hasAttachments` is whether there is one; Schedule needs the
   * list, because what travels to the task form is a copy of every file in it
   * (owner E2E R1, F4 (2026-09-10)). A function, read at Continue time, for the
   * reason `draft` is one.
   */
  attachments?(): readonly Attachment[];
  /**
   * …and the way back, which ONLY A SESSION'S COMPOSER has: the paths a stored
   * record carries, handed to the tray when that record is seeded into the box
   * on mount (or adopted from another tab). These are task-shots-resident real
   * paths, so this is `addPaths`' errand — no upload, thumbnails through
   * /api/fs/raw.
   *
   * A session-less composer (`new:<file>`) never calls it: its record is an
   * Upcoming row, and "Back to chat" from that card lands on a CLEAN box
   * (Akshil, 2026-09-16).
   *
   * IT MAY ANSWER WITH A PROMISE, and the host's does: `addPaths` commits the
   * chips past an await, and the box holds its autosave until that has landed
   * (`restoreTray`, Bugbot 4027549715). A host that restores synchronously —
   * and every test double — may go on returning nothing.
   *
   * THE PROMISE MAY RESOLVE WITH A REVERT — the host's does, straight off
   * `addPaths`' own answer (`useAttachments.ts`): a function that undoes
   * exactly what THIS call put in the tray, and nothing else. `restoreTray`
   * below calls it, instead of `onDiscardAttachments`, when this particular
   * restore turns out to be stale — a newer one already got there first, and
   * the whole-tray wipe would have taken its files too (Bugbot 4028927464).
   */
  onRestoreAttachments?(paths: string[]): void | Promise<void | (() => void)>;
  /** Empty the tray. What leaving a never-sent chat does to the files once
   *  they are on the draft, what adopting a record from elsewhere does to them,
   *  and what the Schedule hop does once they are on the card — the way `take()`
   *  does it on Send. */
  onDiscardAttachments?(): void;
  /**
   * ⌘V of a picture or a file (T:11719 `shotPasteHandler`). The handler decides
   * whether the paste was an attachment — a paste of WORDS must reach the box,
   * and stealing an ordinary paste in a composer the user types in all day would
   * be a far worse bug than never having had the feature.
   */
  onPaste?: React.ClipboardEventHandler<HTMLTextAreaElement>;
  /**
   * THE CAMERA'S OLD SEAT, immediately left of Schedule (T:4166-4171). The
   * screenshot button lived here as a pill and moved into the `#anncta` strip on
   * 2026-08-27 because it acts on the PREVIEW rather than on this draft — so
   * `ClaudeChat` passes nothing and the seat stands empty. It stays a seat
   * because the row's fit is MEASURED: a control appearing here changes what
   * fits, and the revision below is what re-prices the row when it does.
   */
  camera?: ReactNode;
  /**
   * Anything OUTSIDE this row whose arrival changes the row's geometry — the
   * chip row growing, the camera seat filling. T watches for those with a
   * MutationObserver on the rows' subtree `hidden` and on the body/chat classes
   * (T:12455-12474); in React they arrive as a re-render, so the caller bumps
   * this instead (inventory 03 §G, and `useRowFit`'s `revision`).
   */
  fitRevision?: unknown;
  back: string;
  onNavigate?(url: string): void;
}

/** EVERYTHING THERE IS TO LOSE IN A COMPOSER: the words, and how many files are
 *  in the tray. It is the pair `dirty` is made of, and the latch a deliberate
 *  clear leaves behind reads it — see `spent`.
 *
 *  TWO FIELDS RATHER THAN ONE STRING (Bugbot 4036328238). The halves come from
 *  two different pieces of state — `text` is this component's, the tray is the
 *  host's — and they land in separate renders, so a render is routinely stale in
 *  one and fresh in the other. A single comparable value can only answer "is all
 *  of this the spent box", and answered "no" to a render that had emptied one
 *  half and was still showing the other: the latch came off, and the still-stale
 *  half went straight into the mirrors the unmount save reads. */
interface BoxShape {
  text: string;
  files: number;
}
const boxShape = (words: string, files: number): BoxShape => ({
  text: words,
  files,
});

export function ComposerCard({
  variant,
  file,
  sessionId,
  controls,
  status,
  queued,
  queueOn,
  onSend,
  onFollowUp,
  onStop,
  hasAttachments,
  attachPending,
  blocked,
  blockedPlaceholder,
  blockedReason,
  navLocked,
  navLockedReason,
  autoFocus,
  placeholder,
  restore,
  boxRef: hostBoxRef,
  submitRef,
  busyRef,
  sendBusy,
  columnRef,
  chips,
  attachments,
  onRestoreAttachments,
  onDiscardAttachments,
  onPaste,
  camera,
  fitRevision,
  back,
  onNavigate,
}: ComposerCardProps) {
  // WHAT IS IN THE BOX DEPENDS ON WHETHER THERE IS A CONVERSATION BEHIND IT —
  // the one fork this file has, and every rule below hangs off it (see "THE
  // DRAFT, AND THE TWO BOXES THAT KEEP ONE DIFFERENTLY").
  const [text, setText] = useState("");
  const restoreAttachments = useRef(onRestoreAttachments);
  restoreAttachments.current = onRestoreAttachments;
  const { ref: boxRef, grow } = useAutoGrow(text);

  /**
   * IDLE UNLESS THE READER IS IN IT (Akshil, 2026-09-16: "on load when not
   * active, single line with only the placeholder and send; when we click it
   * becomes active and smooths into the current layout; when I click outside
   * it is not active, even if there is text inside").
   *
   * The chat's composer is ONE LINE — the box and Send — whenever the reader's
   * attention is elsewhere, and the full card only while they are in it. Two
   * facts make "in it": focus is inside the form (`within`, from the form's own
   * focus/blur), AND the reader put it there (`gestured` — a pointer on the
   * form or a key in its box). The second guard is for `autoFocus` and the
   * caret-taking effects, which focus the box on arrival: a card that opened
   * on its own the moment the page loaded would not be an idle line. A blur
   * that leaves the form clears both, so a click on the transcript folds the
   * card back — with the draft still in the box, one line of it showing.
   *
   * `active` is DERIVED and the card's furniture is never hidden while it is
   * needed: a run in progress keeps Stop reachable (Send and Stop are one
   * button, which the folded line keeps), and a block banner over the box
   * keeps the card open so the disabled controls it explains are in view. The
   * landing card (`variant === "home"`) never folds.
   */
  const [within, setWithin] = useState(false);
  const [gestured, setGestured] = useState(false);
  const engage = useCallback(() => setGestured(true), []);
  // A PRESS ON SEND IS NOT A GESTURE AT THE CARD (Akshil, 2026-09-16: "send
  // button should work without making the whole chat active"): it sends — or
  // stops — from the idle line and leaves it idle. Everything else in the form
  // is the reader reaching for the card.
  const onFormPointerDown = useCallback((ev: React.PointerEvent<HTMLFormElement>) => {
    if ((ev.target as Element | null)?.closest?.(".c-send")) return;
    setGestured(true);
  }, []);
  const onFormFocus = useCallback(() => setWithin(true), []);
  const formRef = useRef<HTMLFormElement | null>(null);
  /**
   * …AND A PRESS OUTSIDE FOLDS IT EVEN WHEN FOCUS DOES NOT MOVE (Akshil,
   * 2026-09-16: in the Explorer's side panel "it becomes active, doesn't
   * become inactive when I click outside"). The Explorer's listing keeps the
   * keyboard where it is on a press — its rows are not focusable and it does
   * not take focus itself — so the textarea never blurs and `onFormBlur` never
   * runs. Focus is one read of "the reader left"; the pointer is the other.
   * Listened on the document only WHILE the card is open, and a press inside
   * the form or inside a surface the form opened does not count.
   */
  const active = variant === "chat" && within && gestured;
  useEffect(() => {
    if (!active) return;
    const form = formRef.current;
    if (!form) return;
    const doc = form.ownerDocument;
    const onDocPointerDown = (ev: PointerEvent) => {
      const t = ev.target as Element | null;
      if (!t) return;
      if (form.contains(t) || t.closest?.(POPUP_SURFACE)) return;
      setWithin(false);
      setGestured(false);
    };
    doc.addEventListener("pointerdown", onDocPointerDown, true);
    return () => doc.removeEventListener("pointerdown", onDocPointerDown, true);
  }, [active]);
  const onFormBlur = useCallback((ev: React.FocusEvent<HTMLFormElement>) => {
    // Focus moving BETWEEN the form's own controls is a blur too; only one
    // that leaves the form is a leave.
    const next = ev.relatedTarget as Element | null;
    if (next && ev.currentTarget.contains(next)) return;
    // …AND A SURFACE THE FORM OPENED IS STILL THE FORM (Akshil, 2026-09-16:
    // "when I click inside the active composer, like a dropdown or Schedule
    // task, it should stay active"). The pill selects and the Schedule confirm
    // are Base UI popovers, PORTALED to the body, so focus moving into one of
    // them is a blur that leaves the form's subtree while the reader is still
    // in the composer. Two reads say so: the focus went into a popup or
    // dialog, or it went nowhere (a press on the popup's own padding) while a
    // popup is up. The popup hands focus back to its trigger when it closes,
    // which is inside the form again, so nothing here has to un-fold later.
    if (next && next.closest(POPUP_SURFACE)) return;
    if (!next && ev.currentTarget.ownerDocument.querySelector(POPUP_SURFACE)) return;
    setWithin(false);
    setGestured(false);
  }, []);

  // ---- THE DRAFT, AND THE TWO BOXES THAT KEEP ONE DIFFERENTLY -------------
  //
  // The key is the session, or `new:<file>` while the chat has not got one yet
  // (chatDraftKey) — the same key the Tasks card edits the record under, so a
  // draft made here and a draft opened there are one record with one name.
  //
  // AND THE KEY IS ALSO WHICH RULE THIS BOX LIVES BY (Akshil, 2026-09-16):
  //
  //   * ON A SESSION — a finished task, a blocked one, a run in progress, an
  //     archived conversation — this composer is WHERE that chat's unsent
  //     message is. The ✎ Draft chip on the row points at it and nothing else
  //     shows it, so the box has to hold what was left in it. Everything the
  //     draft ever did stands: it seeds from the record on mount, autosaves 600
  //     ms after the last keystroke, flushes on blur and on `pagehide`, adopts
  //     what another tab wrote, empties when the record goes, and its Send
  //     spends the draft. Nothing asks on the way out, because nothing here is
  //     ever unsaved.
  //   * WITH NO SESSION YET there is no chat for a record to be the unsent
  //     message OF — what the words would become is an UPCOMING ROW on the Tasks
  //     card, and a box that autosaved into that list minted a task out of every
  //     half-typed thought. So this one writes nothing on its own and opens
  //     empty, and the single moment its words can be lost — leaving — SAVES
  //     them, silently, and says so on a toast with an Undo (Akshil,
  //     2026-09-17). It used to ask instead ("Unsent message": save as draft,
  //     discard, cancel), and the dialog was a door held shut in front of a
  //     reader who had already decided to go somewhere: three buttons for a
  //     question with one sensible answer, on every hop out of a chat. The
  //     answer it kept getting is now what happens, and the one press that
  //     undoes it rides on the confirmation. `pagehide` is the same save with
  //     no room for a file copy, and there is no native prompt over it any
  //     more — a reload writes the words and goes.
  //
  //     AND WHAT IT SAVES IS A TASK DRAFT, `draft:<id>`, minted fresh every time
  //     (`composerTaskDraft`; Akshil, 2026-09-16). It used to be this box's
  //     `new:<file>` key — one record per FOLDER — so the second thing the
  //     reader saved out of a folder landed on top of the first. `draftKey`
  //     below is therefore only the SESSION road's key now; the other road never
  //     writes under it, and nothing else in this app does either.
  const draftKey = chatDraftKey(sessionId, file);
  /** Which of the two rules above is in force. */
  const hasSession = !!sessionId;
  // …and the same fact for the handlers that were built before this render.
  const hasSessionRef = useRef(hasSession);
  hasSessionRef.current = hasSession;
  // What the box holds RIGHT NOW, for everything that runs outside a render:
  // the async seed below, the conflict rule, the leave guard, `pagehide` and the
  // unmount all fire from closures built several keystrokes ago.
  // …mirrored below, beside `dirtyRef`, rather than here: the two say one
  // thing between them and a clear has to be able to survive both (`spent`).
  const textRef = useRef(text);
  /**
   * WHAT THE BOX HELD WHEN IT WAS LAST EMPTIED ON PURPOSE, or null — and the
   * mirrors are written PAST it (bug report, 2026-09-17: ONE "Save as draft"
   * press, TWO Upcoming rows holding the same sentence under two ids).
   *
   * `clearComposer` and `submit` empty `textRef`/`dirtyRef` SYNCHRONOUSLY and
   * queue `setText("")` at ordinary priority. Anything that renders this
   * subtree at a HIGHER priority before React flushes that queue re-runs this
   * body with `text` still holding the spent words — a lower-priority update is
   * left in the queue rather than applied early — and `ClaudeChat` keeps the
   * whole conversation in a `useSyncExternalStore`, whose every emit (a poll
   * landing, a run tick, a controller notice) is exactly such a render. The
   * unconditional mirrors then put the words AND the dirty flag back on a box
   * the reader had already answered for, and the unmount behind the navigation
   * filed them a second time — under a SECOND id, because the same clear had
   * blanked `unsentId` (`mintUnsentId`).
   *
   * So a clear latches what it spent, and a render still showing exactly that
   * is read as the stale render it is. The latch is the whole SHAPE `dirty` is
   * made of — the words and how many files are in the tray — because the tray
   * is the host's own state and lags a clear the same way. Everything that puts
   * something into the box un-latches first, so a sentence typed twice is never
   * mistaken for the one already filed.
   *
   * HALF BY HALF, AND THE LATCH COMES OFF LAST (Bugbot 4036328238). The two
   * halves arrive in different renders, so each is believed on its own: a half
   * that still reads exactly what was spent is refused, a half that has moved on
   * is mirrored, and only a render whose BOTH halves have moved on puts the box
   * back in charge. Judging the pair as one value let the first half to empty
   * release the latch and wave the other, still-stale, half through.
   */
  const spent = useRef<BoxShape | null>(null);
  const draftKeyRef = useRef(draftKey);
  draftKeyRef.current = draftKey;
  const discardAttachments = useRef(onDiscardAttachments);
  discardAttachments.current = onDiscardAttachments;
  const trayRead = useRef(attachments);
  trayRead.current = attachments;
  const fileRef = useRef(file);
  fileRef.current = file;
  /**
   * THE ID A SESSION-LESS BOX'S WORDS WOULD BE FILED UNDER — minted by the
   * first road that needs one, reused by the rest, and forgotten the moment the
   * box is cleared (Akshil, 2026-09-16).
   *
   * There are two roads out of a chat that has never been sent and they can
   * both run for ONE set of words: the leave save, and the `pagehide`/unmount
   * write under it. Minting an id inside each would file the same half-sentence
   * as two Upcoming rows. Minting one per EPISODE — one set of words, from the
   * first keystroke to the clear — is the rule that makes "every save is a new
   * draft" true without making "one save" mean two.
   */
  const unsentId = useRef("");
  const mintUnsentId = useCallback((): string => {
    if (!unsentId.current) unsentId.current = newTaskDraftId();
    return unsentId.current;
  }, []);

  /**
   * WHICH SET OF WORDS THIS BOX IS ON — a counter, bumped every time the box is
   * emptied ON PURPOSE (Bugbot 4027549698).
   *
   * The seed's GET is the one thing in this file that paints the box from an
   * answer older than the box itself. It used to be judged by a single
   * question — "is the box empty?" — and a Send, a Discard or an adopted
   * deletion is EXACTLY a box that has just become empty, so an answer landing
   * a moment later repainted the sentence that had just been spent. `gone` then
   * read the restored words as a reader typing a follow-up and kept them.
   *
   * A counter rather than a flag because the same box can go through this
   * several times, and the seed has to compare against the episode it was
   * dispatched IN, not against "has anything ever happened".
   */
  const episode = useRef(0);
  /**
   * A SCHEDULE HOP IS IN FLIGHT, AND THIS BOX IS FROZEN WHILE IT IS
   * (Bugbot 4034977395).
   *
   * Continue closes its confirm, copies the tray into the task-shots dir a round
   * trip at a time, and only then writes the record — and until this flag
   * existed the composer stayed fully live for every one of those milliseconds.
   * Send, the leave dialog's Save and Discard, another Continue: each of them
   * spends or re-files the very words the hop has latched, and the hop wrote its
   * own copy afterwards regardless. One set of words, one gesture at a time.
   *
   * The ref is what the out-of-render handlers read (`submit`, the leave guard);
   * the state is what dims the controls.
   */
  const [hopping, setHopping] = useState(false);
  const hoppingRef = useRef(false);
  hoppingRef.current = hopping;
  /** What the hop compares against, twice: once when Continue is pressed and
   *  once at the last moment before it writes. */
  const readEpisode = useCallback(() => episode.current, []);
  /**
   * THE RECORD A RESTORED TRAY IS STILL FILLING FROM, or null (Bugbot
   * 4027549715).
   *
   * Seeding and adopting hand the tray real paths through `onRestoreAttachments`
   * → `addPaths`, which only commits PAST AN AWAIT — so the render that paints
   * the restored words still has an EMPTY tray, and the autosave behind it
   * pushed those words with no files and persisted the wipe before the chips
   * landed. While this is set nothing is written, and the baseline is put back
   * to the record itself: the change is still owed, so the first render after
   * the tray fills carries the whole draft rather than nothing at all.
   */
  const heldBase = useRef<{ text: string; attachments: DraftAttachment[] } | null>(null);
  const restoring = useRef(0);
  /** Hand the tray a record's paths and hold autosave until they are IN it. */
  const restoreTray = useCallback(
    (files: readonly DraftAttachment[], base: { text: string; attachments: DraftAttachment[] }) => {
      if (!files.length) return;
      restoring.current += 1;
      heldBase.current = base;
      // WHAT THIS HOLD IS ABOUT, latched at dispatch (Bugbot 4028710588): the
      // key it names and the episode the box was on when it opened — the same
      // two questions the seed's text path asks of its own await.
      const key = draftKeyRef.current;
      const era = episode.current;
      const done = () => {
        restoring.current = Math.max(0, restoring.current - 1);
        if (!restoring.current) heldBase.current = null;
      };
      // The prop may answer with nothing at all (a host that restores
      // synchronously, every test double), and a hold nobody ever releases is
      // an autosave that never speaks again — so the sync answer releases here.
      const back = restoreAttachments.current?.(files.map((a) => a.path));
      if (back && typeof (back as Promise<void | (() => void)>).then === "function") {
        void (back as Promise<void | (() => void)>).then((revert) => {
          // ABORT: a Send, an adopted delete, or a key change already moved
          // this box past the episode this hold was about (Bugbot 4028710588).
          // `addPaths` commits past its own await, so landing here at all is
          // exactly a spent draft's files coming back into the tray — put
          // right back out, and nothing is said to the syncer about them.
          //
          // ONLY WHAT THIS CALL ADDED, and NEVER `discardAttachments` (Bugbot
          // 4028927464): a NEWER restore — another seed, an adopted record —
          // can be sitting in the same tray right now, its own files already
          // landed or still on the way, and the whole-tray wipe took those
          // too, on top of bumping the epoch out from under its own pending
          // `addPaths`. `revert` is this call's own undo and touches nothing
          // else.
          if (
            draftKeyRef.current !== key ||
            episode.current !== era ||
            peekDraftSyncer(key)?.isGone()
          ) {
            revert?.();
          }
          done();
        }, done);
      } else {
        done();
      }
    },
    [],
  );

  // ONCE PER KEY, and never killed by a cleanup. The first shape latched a
  // single "seeded" ref AND flipped an `alive` flag in the effect's cleanup;
  // the two together lost every draft (owner E2E flow D, 2026-09-11): the
  // key changes once on most mounts (the session id lands a render after the
  // box does, `new:<file>` → `<session>`), so the cleanup killed the fetch in
  // flight and the latch refused the re-run. Now each key fetches once, a
  // late answer is judged only by whether the box is still empty, and a
  // StrictMode double mount costs one duplicate GET whose second answer is a
  // no-op `setText` of the same words.
  const seededKeys = useRef<Set<string>>(new Set());
  /**
   * A DRAFT THAT LANDED HAS TO TAKE THE KEYBOARD (Akshil QA, 2026-09-14).
   *
   * Pressing a never-sent chat's row in the Recent list is a promise that the
   * next Enter sends those words — and it was not kept: the box filled and
   * `document.activeElement` stayed on `<body>`. The mount effect below fires
   * `autoFocus` when the composer APPEARS, which on that road is before the
   * draft's GET has answered, and the commit that paints the restored text
   * (plus the auto-grow relayout behind it) can leave the caret nowhere.
   *
   * So the seed says when it landed and the focus is taken THEN, at the end of
   * the text — a caret in the middle of a restored sentence is its own small
   * bug. A counter rather than a flag, because a key change (`new:<file>` →
   * `<session>`) can seed twice in one composer's life.
   */
  const [seededAt, setSeededAt] = useState(0);
  useEffect(() => {
    // ONLY A SESSION'S COMPOSER READS. A `new:<file>` box seeding from its
    // record would paint an Upcoming row back into a chat the reader had just
    // opened fresh, which is the thing the session-less rule exists to stop.
    if (!hasSession) return;
    if (seededKeys.current.has(draftKey)) return;
    seededKeys.current.add(draftKey);
    // Words typed before the GET answered are the reader's and outrank it.
    if (textRef.current) return;
    // WHAT THIS READ IS ABOUT, latched at dispatch: the key it names and the
    // set of words the box was on when it went out (Bugbot 4027549698).
    const key = draftKey;
    const era = episode.current;
    void fetchChatDraft(key).then((saved) => {
      // THREE ANSWERS, TWO OF WHICH ARE "LEAVE IT EMPTY": `undefined` is a read
      // that FAILED (offline, the server restarting) and `null` is a key with no
      // record. Neither may touch the box, and neither is told to the syncer —
      // a failed read that seeded an empty state would be this client claiming
      // to know what the server holds.
      if (!saved) return;
      // AND IT IS STILL THIS BOX, ON THESE WORDS, ON A RECORD THAT EXISTS
      // (Bugbot 4027549698). "Is the box empty?" was the whole test, and an
      // empty box is precisely what a Send, a Discard and a remote delete leave
      // behind — so an answer that had been in the air across one of them
      // repainted words the reader had already spent, and `gone` then kept them
      // as a follow-up. Three questions, one per way that can happen:
      //
      //   * the key moved on (`new:<file>` → the session it was just given),
      //   * the box was emptied on purpose since this read went out (`episode`),
      //   * this page has since said the record should not exist (`isGone`) —
      //     which is the Send's own DELETE, and the trash's, and covers the
      //     version this answer names being one already spent.
      if (draftKeyRef.current !== key) return;
      if (episode.current !== era) return;
      if (peekDraftSyncer(key)?.isGone()) return;
      // WORDS TYPED WHILE THE FETCH WAS IN FLIGHT outrank anything it can
      // answer (design.md: "a composer that is focused ignores incoming draft
      // updates"). The test is the words, NOT the caret: `autoFocus` below puts
      // the caret in the box on mount, before any fetch can answer.
      if (textRef.current) return;
      const files = saved.attachments ?? [];
      // THE TRAY FIRST, because the hold it takes has to be up before the
      // autosave behind the `setText` below can speak (Bugbot 4027549715).
      // These are real paths, so they are registered rather than uploaded.
      restoreTray(files, { text: saved.text ?? "", attachments: files });
      if (saved.text) {
        // A seed is words going IN, so the stale-render latch stands down for
        // them the way it does for a keystroke (`spent`).
        spent.current = null;
        setText(saved.text);
        // Restored words are already the server's words: tell the hook (so the
        // box coming back is not a change) and the syncer (so it is not a write).
        autosaveRef.current.reset({ text: saved.text, attachments: files });
        draftSyncer(key).seedText(saved.text, files);
        grow();
        // …and the caret goes in after them (see `seededAt`).
        setSeededAt((n) => n + 1);
      }
    });
  }, [hasSession, draftKey, grow, restoreTray]);

  // What the tray holds, in the draft's own three fields. Read during render
  // because `attachments()` is a plain read of the host's state (ClaudeChat
  // passes `() => attach.items`), and `pending`/`view`-less chips are left out
  // for SchedButton's reason: a chip still uploading names no file yet.
  const trayDraft: DraftAttachment[] = attachments
    ? attachments()
        .filter((a) => !a.pending && !!a.view)
        .map((a) => ({
          path: a.view as string,
          name: a.name || (a.view as string),
          // Anything that is not a picture wears the glyph, the same floor a
          // stored record's own rows carry.
          kind: a.kind === "image" ? "image" : "file",
        }))
    : [];
  const trayDraftRef = useRef(trayDraft);
  /**
   * THE MIRRORS, WRITTEN HALF BY HALF PAST THE LATCH (`spent`, Bugbot
   * 4036328238) — and ALL of them, which is the other half of that bug: the
   * tray mirror used to be written here unconditionally, so a stale render
   * handed the spent files to the unmount save and to the session-flip effect
   * however carefully the words were guarded.
   *
   * `dirtyRef` is then DERIVED from the two mirrors rather than from this
   * render, so there is exactly one answer to "is there anything here to lose"
   * and it is made of the same words and files every save will write.
   */
  const dirtyRef = useRef(false);
  {
    const latched = spent.current;
    const freshText = !latched || latched.text !== text;
    const freshTray = !latched || latched.files !== trayDraft.length;
    if (freshText) textRef.current = text;
    if (freshTray) trayDraftRef.current = trayDraft;
    // Only a box that has moved on in BOTH halves is the reader's again.
    if (freshText && freshTray) spent.current = null;
    dirtyRef.current =
      !hasSession
      && (!!textRef.current.trim() || trayDraftRef.current.length > 0);
  }

  // ---- THE SESSION COMPOSER'S AUTOSAVE ------------------------------------
  //
  // 600 ms after the last keystroke, plus blur / pagehide / unmount. Empty text
  // with an empty tray is a DELETE server-side, so clearing the box by hand
  // clears the draft too without this having to know the difference.
  //
  // NO `form` IS SENT, ever, and that is the whole reason the contract makes it
  // a patch (drafts §2): a composer has no opinion about a time or a repeat, so
  // its keystroke saves must not wipe the ones a Schedule hop put on the same
  // record while the reader was typing in this box.
  //
  // THE CONFLICT RULE (design §2). A 409 means somebody else — the other tab,
  // the task form, the Board — wrote this record first. If the caret is not in
  // this box, or nothing has been typed since the last save, their words are
  // simply the newer ones and the box takes them. If the reader is mid-sentence
  // theirs win, once, and the toast says so: keeping a half-typed line silently
  // over somebody else's save is how two tabs lose one message between them.
  //
  // THE HOOK IS CALLED ON BOTH ROADS AND ONLY SPEAKS ON ONE. A session-less box
  // hands it the same value and a `push` that says nothing, which is how the
  // fork stays one `if` rather than two components: `useAutosave` only calls
  // `push` when the value CHANGES, so a silent push is a composer that writes
  // nothing at all.
  const focusedRef = useRef(false);
  const autosave = useAutosave(
    { text, attachments: trayDraft },
    (value) => {
      if (!hasSessionRef.current) return;
      // THE TRAY IS STILL FILLING FROM THE RECORD (Bugbot 4027549715). This
      // value's `attachments` is the tray as it is RIGHT NOW, which during a
      // restore is empty — writing it would persist a wipe of the very files
      // being put back. So nothing is said, and the baseline goes back to the
      // record: the change stays owed, and the render that lands the chips
      // pushes the whole draft.
      const held = heldBase.current;
      if (held) {
        autosaveRef.current.reset(held);
        return;
      }
      draftSyncer(draftKeyRef.current).setText(value.text, value.attachments);
    },
    { key: draftKey },
  );
  // THE CONFLICT RULE IS REGISTERED WITH THE KEY, not held by this component:
  // the syncer is the record's writer and outlives every editor that opens on
  // it, so what it needs is to know which editor — if any — is on screen to
  // adopt into right now. The answer detaches it, so a composer that has gone
  // cannot be asked to take somebody else's words.
  useEffect(() => {
    if (!hasSession) return;
    return draftSyncer(draftKey).watch({
      focused: () => focusedRef.current,
      localText: () => textRef.current,
      adopt: (record) => adoptRef.current(record as ChatDraft | null),
      onKept: () =>
        notify({ title: "Updated elsewhere, kept your text", tone: "info" }),
    });
  }, [hasSession, draftKey]);
  // `submit` is a useCallback built below; it needs the autosave handle, and the
  // handle's identity is stable, so it is read through the ref every other seat
  // in this file uses for the same reason.
  const autosaveRef = useRef(autosave);
  autosaveRef.current = autosave;

  /**
   * THE MOMENT THE CHAT GETS A SESSION, AND WHAT IS IN THE BOX WHEN IT DOES
   * (Bugbot 4027549731).
   *
   * The first send mints the session, and it lands a render later — so a
   * follow-up typed in that gap sits in a box that has just changed rules. The
   * session-less half stands down (`dirty` goes false, the leave guard
   * unregisters, and nothing asks on the way out any more) and the session half
   * has not been told anything: `useAutosave` only speaks when the VALUE
   * changes, and the value did not change, the key did. The words were on
   * nobody's books, and leaving or reloading neither asked nor saved them.
   *
   * So the flip states them itself, once, on the session's own syncer — and
   * that is also why the guard does not have to be held until it lands: from
   * this line on the words are the SYNCER's, and a syncer with something
   * pending flushes on `pagehide`, on a tab switch and on a window blur for
   * every key at once (`drafts.listen`). A layout effect, so the statement is
   * made before the paint that drops the guard rather than after it.
   */
  const hadSession = useRef(hasSession);
  useLayoutEffect(() => {
    const was = hadSession.current;
    hadSession.current = hasSession;
    if (was || !hasSession) return;
    const words = textRef.current;
    const files = trayDraftRef.current;
    // A SEND CLEARS THE BOX BEFORE THE SESSION ARRIVES, which is the ordinary
    // road here: nothing left behind, nothing to state.
    if (!words.trim() && !files.length) return;
    autosaveRef.current.reset({ text: words, attachments: files });
    draftSyncer(draftKey).setText(words, files);
    // …and the id those words would have been filed under as a TASK draft is
    // spent: they are a chat's unsent message now, and the session's record is
    // where they live.
    unsentId.current = "";
  }, [hasSession, draftKey]);

  /**
   * PUT THE SERVER'S RECORD ON SCREEN — the one place this composer adopts words
   * it did not type. Reached from the 409 rule above and from the change feed
   * below, so "somebody else edited this draft" has exactly one outcome however
   * the news arrives.
   *
   * `null` is the record deleted: the box empties the way its own Send empties
   * it, tray included, because the files were part of the draft that is gone.
   */
  const adoptRecord = useCallback((record: ChatDraft | null) => {
    const next = record?.text ?? "";
    const files = record?.attachments ?? [];
    /**
     * AND THE LATCH IS TOLD WHICH OF THE TWO THINGS THIS IS (`spent`, Bugbot
     * 4036328238).
     *
     * A record with content is a WRITE: these words did not come from the box,
     * so nothing here is the spent box and the mirrors take them at once rather
     * than waiting for a render that may be overtaken.
     *
     * `null` is the record DELETED, which empties the box exactly the way
     * `clearComposer` does — and it used to un-latch, which is the opposite of
     * what an emptying needs. The tray empties a render later (the host owns
     * it), so the very next render still showed the deleted draft's files and,
     * un-latched, mirrored them into the unmount save: the record the reader had
     * just seen deleted came back as a fresh Upcoming row.
     */
    if (record) {
      spent.current = null;
      textRef.current = next;
      trayDraftRef.current = files;
      dirtyRef.current =
        !hasSessionRef.current && (!!next.trim() || files.length > 0);
    } else {
      spent.current = boxShape(textRef.current, trayDraftRef.current.length);
      textRef.current = "";
      // BOTH MIRRORS, not just the words (Bugbot 4036328238). The tray mirror used
      // to be left holding the spent chips until a render replaced it — and the
      // latch, doing its job, is exactly what stops a stale render replacing it.
      // `dirtyRef` is made of the two, so a bare picture left in the mirror was an
      // "unsaved message" the unmount filed all over again.
      trayDraftRef.current = [];
      dirtyRef.current = false;
    }
    // A BOX REPAINTED FROM ELSEWHERE IS A NEW SET OF WORDS (Bugbot 4027549698):
    // a seed's answer still in the air was asked about the ones this replaces,
    // and `null` here — the record deleted — is the case it must never undo.
    episode.current += 1;
    setText(next);
    discardAttachments.current?.();
    // …and the tray takes its hold before the reset below, for the same reason
    // the seed does (Bugbot 4027549715).
    restoreTray(files, { text: next, attachments: files });
    autosaveRef.current.reset({ text: next, attachments: files });
    // …AND THE SYNCER TAKES IT AS ALREADY-STORED. Without this the record just
    // adopted would be written straight back over: the box changed, and a
    // change is what makes a request. `seedText` is the one way to say "this is
    // what is wanted AND what is there".
    draftSyncer(draftKeyRef.current).seedText(next, files);
    grow();
  }, [grow, restoreTray]);
  const adoptRef = useRef(adoptRecord);
  adoptRef.current = adoptRecord;
  /**
   * THE RECORD CHANGED SOMEWHERE ELSE (design §3) — a session's composer only.
   *
   * `/api/tasks/changes` pushes every announced draft key with its version, so a
   * second tab's save, a discard from the List, a Board drag that sent these
   * words, and `POST /api/schedule` deleting the draft it came from all reach
   * this box the same way and within a second.
   *
   * THE FEED'S GONE IS ONLY ACTED ON FOR A KEY THIS CLIENT HAS A VERSION FOR
   * (contract §3): the announced key set is noisy by construction, and clearing
   * a box on a key nobody has ever written would throw away words that were
   * never saved.
   *
   * A DISCARD MADE ON THIS PAGE IS NOT THAT (`certain`, tasksPulse). Trashing
   * this draft's own row in Recent chats is first-person: the DELETE landed,
   * and the record is gone whatever this box believes about versions. It has to
   * be said, because the delete itself FORGETS the version on its way out
   * (`drafts.write`, contract §2) — so the guard above, applied to a local
   * discard, threw away the one announcement that was never noise and left the
   * composer holding words whose record no longer existed. The next keystroke
   * then wrote them straight back as a fresh v1 (Akshil, 2026-09-16).
   *
   * CHANGED IS ONLY ACTED ON WHEN IT IS NEWER, and never over a reader who is
   * typing: the next save's own 409 settles that case, with the toast.
   */
  useEffect(
    () =>
      onDraftChange((changed, gone, certain) => {
        // A SESSION-LESS BOX HEARS NOTHING. Its key is an Upcoming row's, and
        // that row's life — saved on the card, scheduled, trashed — is no news
        // for a chat the reader is typing a fresh message into.
        if (!hasSessionRef.current) return;
        const key = draftKeyRef.current;
        const seen = draftVersion(key);
        if (gone.includes(key) && (certain || seen !== undefined)) {
          forgetDraftVersion(key);
          // …UNLESS THE READER IS MID-SENTENCE IN THIS BOX (Bugbot, PR #1180).
          // `gone` is news about a RECORD, and a reader typing a follow-up holds
          // words that are newer than whatever was deleted — the send's own
          // DELETE is the everyday way this arrives. Emptying the box on it
          // takes a sentence nobody asked to spend, which is the one thing no
          // rule here may do; the record is gone, so the version is forgotten
          // above and the next save simply creates it again.
          //
          // A DISCARD MADE ON THIS PAGE IS STILL OBEYED (`certain`): trashing
          // this draft's own row is the reader saying so in the first person,
          // and answering that with "no, you were typing" would be the button
          // not working.
          if (!certain && focusedRef.current && textRef.current.trim()) return;
          adoptRef.current(null);
          return;
        }
        if (seen === undefined) return;
        const row = changed.find((c) => c.key === key);
        if (!row || row.version <= seen) return;
        if (focusedRef.current && textRef.current.trim()) return;
        void fetchChatDraft(key).then((saved) => {
          if (draftKeyRef.current !== key) return;
          // COULD NOT FIND OUT IS NOT "IT IS GONE" (Bugbot, PR #1180). A failed
          // GET — offline, the server restarting — used to read as `null` here,
          // and `null` is the instruction to empty the box: a blip took the
          // reader's words. `undefined` says the read failed, and the answer to
          // that is to do nothing at all; the next announcement asks again.
          if (saved === undefined) return;
          // AND THE GUARD IS ASKED AGAIN, because the round trip is where the
          // typing happens. The check above was made before the GET went out,
          // so a reader who started a sentence while it was in the air had it
          // overwritten by an answer that predated their first keystroke.
          if (focusedRef.current && textRef.current.trim()) return;
          adoptRef.current(saved);
        });
      }),
    [],
  );

  /**
   * IS THERE ANYTHING HERE TO LOSE — words, or files in the tray. Pictures with
   * no sentence at all are a message (T:17903), so they are worth the question
   * on their own.
   *
   * FALSE FOR A SESSION'S COMPOSER, ALWAYS, and that single `!hasSession` is
   * what stands the whole leave-guard half of this file down on that road: the
   * registration below, the `pagehide` save and the unmount save all read this.
   * A box that autosaves has nothing to lose, and a second write of words
   * already on the server would be one draft too many.
   *
   * The ref is what every out-of-render handler reads, and it is written
   * SYNCHRONOUSLY by `clearComposer` below: a navigation fired in the same tick
   * as the clear must already see a clean box, or the guard it just satisfied
   * saves the same words again.
   */
  // …AND IT IS THE MIRRORS' ANSWER, not this render's (`spent`, above). A render
  // older than the last clear still shows the spent words; reading `text` here
  // would re-register the leave guard and the native `beforeunload` prompt for a
  // box the reader has already answered for, and then contradict the `dirtyRef`
  // the handlers behind them read.
  const dirty = dirtyRef.current;

  /** What a leave save leaves behind: an empty box and an empty tray (Akshil,
   *  2026-09-16: "after that, the composer is cleared"). Also the Schedule hop's
   *  last act, for the same reason — those words are on the card now, and two
   *  copies of one half-written thing is the bug this design ends. */
  const clearComposer = useCallback(() => {
    // WHAT IS BEING SPENT, latched before it is let go: React has not rendered
    // the empty box yet, and any render that beats it to the commit still shows
    // this and must not be believed (`spent`).
    spent.current = boxShape(textRef.current, trayDraftRef.current.length);
    textRef.current = "";
    // BOTH MIRRORS, not just the words (Bugbot 4036328238). The tray mirror used
    // to be left holding the spent chips until a render replaced it — and the
    // latch, doing its job, is exactly what stops a stale render replacing it.
    // `dirtyRef` is made of the two, so a bare picture left in the mirror was an
    // "unsaved message" the unmount filed all over again.
    trayDraftRef.current = [];
    dirtyRef.current = false;
    // …AND A SEED STILL IN THE AIR IS NOT AN ANSWER ABOUT THESE WORDS ANY MORE
    // (`episode`, Bugbot 4027549698). An emptied box is exactly what that read
    // was told to fill.
    episode.current += 1;
    // …AND THE NEXT WORDS ARE THE NEXT DRAFT. An emptied box starts a new
    // episode, so whatever is typed into it next is filed under an id of its
    // own rather than over the one just saved (`unsentId`).
    unsentId.current = "";
    setText("");
    discardAttachments.current?.();
    grow();
  }, [grow]);

  /**
   * LEAVING SAVES THE WORDS AND GOES — AND IT GOES THROUGH THE KEY'S ONE WRITER
   * (Bugbot review of caef75eb1, HIGH-1).
   *
   * This was the dialog's "Save as draft" button, and it is now simply what
   * leaving does (Akshil, 2026-09-17). Every other answer the dialog offered was
   * a worse version of this one: "Discard" threw away words the reader could
   * have got back from Upcoming in a second, and "Cancel" was a door held shut
   * in front of someone who had already decided to go. So the hop happens, the
   * record is written behind it, and the toast carries the single press that
   * takes it back (`UNDO_TOAST_MS`, `undoSavedDraft`).
   *
   * THE ORDER IS THE POINT. The key is minted, THE WORDS ARE STATED ON THE
   * KEY'S SYNCER, and only then is the box emptied — all of it SYNCHRONOUSLY,
   * before the first await, because the navigation that asked is already on its
   * way and must not be held up by a round trip, and because `clearComposer`
   * forgets `unsentId`: reading it after the clear would file these words under
   * the NEXT episode's id. Everything after that runs from a closure, on module
   * state (`draftSyncer` outlives the editors), so it lands whether or not this
   * composer is still mounted.
   *
   * AND THE STATEMENT COMES BEFORE THE COPY FOR ONE REASON: A DOOR CAN SLAM
   * DURING THE COPY (Bugbot 4035442481).
   *
   * Uploading a tray of pictures is seconds of round trips, and for all of them
   * the box is already empty — so `dirtyRef` is false and BOTH door-slam saves
   * (the `pagehide` handler and the unmount cleanup) correctly decline to file a
   * clean box. If the only copy of the words were the closure below, a reload in
   * that window would abort the one write that had them and they would be gone.
   *
   * A syncer holding a pending state is not in that position: `drafts.listen`
   * flushes EVERY syncer in the registry with `keepalive` on `pagehide` and on a
   * tab switch, and a `keepalive` request outlives the document. So the words
   * are wanted from the first tick, by the module that survives this page, and
   * the copy only ever ADDS to them — a second statement with the files on it,
   * which is a second write, and the cheaper half of the trade.
   *
   * `forget()` AFTERWARDS, because the record has left this page: the Tasks
   * card is where those words are edited now, and a syncer that went on wanting
   * anything for this key would write the cleared box back over them.
   *
   * TWO RECORDS, ONE GESTURE. On a SESSION this states the chat record's words
   * and files and NO `form`, which the contract reads as a patch (drafts §2):
   * a chat whose record already carries a bound form (the ✎ chip's time, repeat
   * and model) keeps every one of those settings. On a chat with no session it
   * mints a TASK draft instead — see `composerTaskDraft`, and the paragraph
   * about `new:<file>` in the draft-key section above. (Only the second road
   * ever gets here: `dirty` is false on a session, so nothing registers a guard
   * there. The branch is kept because `unloadSave` below has the same fork for
   * the same reason, and one of the two spelling it differently is how they
   * start disagreeing.)
   *
   * THE FILES ARE COPIED INTO THE TASK-SHOTS DIR FIRST, the same copy the
   * Schedule hop makes and for the same reason: a chat attachment's path is a
   * tempdir on a 12 h TTL and `POST /api/schedule` refuses any path outside
   * `schedule.shots_dir()`. A draft the card cannot schedule is half a draft.
   *
   * AND A FILE THAT DID NOT MAKE IT NO LONGER STOPS THE SAVE (Akshil,
   * 2026-09-17, reversing Bugbot 4027244608's fail-closed). Refusing to write
   * was the right answer while a dialog was up to refuse INTO — the reader was
   * still standing in front of it. Nothing is standing in front of this: the hop
   * has happened, and "nothing was saved" would mean the words are gone. So the
   * draft is written with whatever copied, the toast names what did not, and the
   * reader still has the record to put the picture back on.
   */
  const saveAndLeave = useCallback(() => {
    const words = textRef.current.trim();
    const carry = (trayRead.current?.() ?? []).filter((a) => !a.pending && !!a.view);
    const onSession = hasSessionRef.current;
    const key = onSession ? draftKeyRef.current : taskDraftKey(mintUnsentId());
    const target = fileRef.current ?? "";
    const sync = draftSyncer(key);
    /** What this gesture wants the record to hold — said once now with no files
     *  on it, and again after the copy with whatever landed. */
    const state = (files: DraftAttachment[]) => {
      if (onSession) sync.setText(words, files);
      else sync.setTask(composerTaskDraft(words, target, files));
    };
    state([]);
    clearComposer();
    void (async () => {
      const carried = carry.length ? await copyToTaskShots(carry).catch(() => []) : [];
      const lost = carry.length - carried.length;
      // …and nothing is said twice when nothing was carried: `setText`/`setTask`
      // with a state it already wants is a no-op, but the `if` says so out loud.
      if (carried.length) state(carried);
      const out = await sync.handoff();
      if (!out.ok) {
        // THE ONE OUTCOME WITH NOTHING TO OFFER. The box is already empty and
        // the page has already moved, so there is no "you are still in the chat"
        // to say any more — only that the write did not land. An error is
        // retained by `resolveTier`, so it is still findable in the panel after
        // the card goes.
        notify({
          title: "Could not save that draft",
          detail: words.slice(0, 80),
          tone: "error",
        });
        return;
      }
      sync.forget();
      // …AND THE LISTING HEARS IT NOW, not on the next long-poll. This gesture
      // already hopped the reader to Upcoming (or is about to), so a paint that
      // reads the listing before this row exists on the server is the common
      // case, not the rare one — and `undoSavedDraft` below announces on its
      // own write for exactly the same reason: the two halves of one gesture
      // must not disagree about when the card shows up.
      announceTasksChanged();
      notify({
        title: "Saved as draft",
        ...(lost
          ? {
              detail: `${lost} file${lost === 1 ? "" : "s"} could not be attached`,
            }
          : {}),
        action: { label: "Undo", onClick: () => void undoSavedDraft(key) },
        // POP ONLY. An Undo that outlived its card would delete a draft the
        // reader has since gone on writing — see `NotificationInput.retain`.
        retain: false,
        popupMs: UNDO_TOAST_MS,
        // …AND THE SECONDS ARE REAL ONES (Bugbot 4035442489). This card pops on
        // the page the hop just landed on, and an ordinary popup closes on the
        // reader's first press anywhere — which on a page they have just arrived
        // at is immediately, and long before they have found the Undo. Sticky
        // keeps it up for its own `popupMs`; Escape and the ✕ still close it.
        sticky: true,
      });
    })();
  }, [clearComposer, mintUnsentId]);

  /**
   * THE LAST WRITE A DOOR-SLAM GETS — and it is WORDS ONLY (Bugbot review,
   * HIGH-1 and HIGH-2).
   *
   * Two reasons it is not the same call as "Save as draft":
   *
   *   * IT GOES THROUGH THE SYNCER, like every other write on this key, so it
   *     carries the page's `client`/`seq` and the version it is editing. A bare
   *     `saveChatDraft` from a tab that was reloading could otherwise land
   *     AFTER the Tasks card had edited the same record and silently undo it.
   *   * IT DROPS THE TRAY. A chat attachment's path is a tempdir on a 12 h TTL,
   *     and `POST /api/schedule` refuses any path outside `schedule.shots_dir()`
   *     — so writing those raw paths onto the record makes a draft the card
   *     cannot schedule and that 404s tomorrow. The fix is a copy per file
   *     (`copyToTaskShots`), and a copy is a round trip, which is exactly what a
   *     `pagehide` handler does not have. So the words are saved, the files are
   *     not, and the console says so; the two roads that CAN wait — the modal's
   *     Save and the Schedule hop — carry the files properly.
   */
  const unloadSave = useCallback((opts: { keepalive?: boolean }) => {
    // THE CHECK LIVES HERE TOO, not only in the two callers that already make
    // it (the `pagehide` handler, the unmount cleanup). Both read `dirtyRef`
    // before calling this — but a door-slam save that trusted its callers
    // alone is one stale caller away from filing an already-saved-and-cleared
    // box as a brand-new draft (`mintUnsentId` mints fresh the moment
    // `unsentId` is empty, which is exactly what `clearComposer` just made
    // it). Asking again, here, is what makes "no write after the box is
    // clean" true regardless of who calls this next.
    if (!dirtyRef.current) return;
    const words = textRef.current.trim();
    const dropped = trayDraftRef.current.length;
    if (dropped && typeof console !== "undefined") {
      console.warn(
        "[composer] the page is going: saving the words only and dropping "
          + `${dropped} attachment${dropped === 1 ? "" : "s"} — a task-shots copy `
          + "is a round trip, and this handler has none.",
      );
    }
    // …AND IT FILES THE SAME KIND OF RECORD THE DIALOG'S SAVE WOULD
    // (`composerTaskDraft`): a chat with no session has no unsent message, it
    // has a task nobody finished writing, and a door slammed on one must leave
    // the same Upcoming row a pressed button would. Under the SAME id as that
    // button's — `unsentId` is minted once per set of words — so a reader who
    // saved and then reloaded has one draft, not two.
    const sync = draftSyncer(
      hasSessionRef.current ? draftKeyRef.current : taskDraftKey(mintUnsentId()),
    );
    if (hasSessionRef.current) sync.setText(words, []);
    else sync.setTask(composerTaskDraft(words, fileRef.current ?? "", []));
    sync.flushNow(opts);
  }, [mintUnsentId]);

  /**
   * THE GUARD, AND IT ALWAYS SAYS YES.
   *
   * `registerLeaveGuard` is consulted by `navigate`/`navigateUrl` before they
   * push (platform/lib/router.ts), so a folder row, a breadcrumb, the Tasks page
   * and the notification list all reach this save without knowing a composer
   * exists. The two hops that do NOT go through those — closing the explorer's
   * Claude panel and switching the session inside one pane — reach it through
   * `confirmLeave()`; see ClaudeChat, Listing and Preview.
   *
   * IT IS STILL A GUARD rather than nothing at all, because the registry is the
   * only thing that knows a composer is on screen: without it the panel's ✕ and
   * the session switch would take the box away with no save under them at all
   * (the unmount write is the floor, not the plan — it drops the tray). What
   * changed is the answer: it used to be a promise nobody resolved until a
   * reader had pressed one of three buttons, and it is now `true`, said in the
   * same tick, with the words already on their way to the server.
   *
   * SYNCHRONOUS ON PURPOSE. `guarded` awaits this before pushing, so anything
   * returned here is time the reader spends looking at the page they are
   * leaving. The save is fired and not awaited for exactly that reason.
   */
  const saveBeforeLeaving = useCallback((): boolean => {
    if (!dirtyRef.current) return true;
    // …EXCEPT WHILE A HOP IS MID-AIR, and then it says NO (Bugbot 4034977395,
    // kept through the silent-save rewrite). The Schedule hop is already
    // carrying exactly these words to a card, with the files copied and the
    // record version stated; a leave-save fired under it would file the same
    // half-written thing a second time, which is the one bug this whole design
    // exists to end. Refusing is not a door held shut for long — the hop lands
    // in a moment and clears the freeze, and the navigation can be made again.
    if (hoppingRef.current) return false;
    saveAndLeave();
    return true;
  }, [saveAndLeave]);
  useEffect(() => {
    if (!dirty) return;
    return registerLeaveGuard(saveBeforeLeaving);
  }, [dirty, saveBeforeLeaving]);

  /**
   * A RELOAD OR A TAB CLOSE, AND IT IS SILENT TOO.
   *
   * There used to be a `beforeunload` here raising the browser's native "leave
   * site?" prompt — the only warning the platform allows a page to give — with
   * the `pagehide` save under it. It is gone (Akshil, 2026-09-17): an in-app hop
   * no longer asks, and a reload that stopped to ask would be the same design
   * saying two different things about the same words. What is left is the half
   * that was always doing the real work: `pagehide` fires when the page is
   * actually going, and the words are SAVED rather than dropped — `keepalive` is
   * what lets that request outlive the document.
   *
   * The floor is unchanged, and it is the only one that ever mattered: never
   * lose text silently. Saving it silently is not losing it.
   */
  useEffect(() => {
    if (!dirty || typeof window === "undefined") return;
    const onPageHide = () => {
      if (!dirtyRef.current) return;
      unloadSave({ keepalive: true });
    };
    window.addEventListener("pagehide", onPageHide);
    return () => {
      window.removeEventListener("pagehide", onPageHide);
    };
  }, [dirty, unloadSave]);

  /**
   * …AND THE SAME FLOOR FOR A HOST THAT TAKES THIS COMPOSER AWAY WITHOUT ASKING.
   *
   * Every door this file knows about asks first. A host that closes the pane
   * from its own chrome (the explorer listing's pane ✕, which lives in
   * `apps/explorer` and writes `_side=off` in place rather than navigating) does
   * not, and an unmount is too late for a dialog. So the words are written
   * instead of dropped — the one outcome that is never wrong.
   *
   * It cannot double-write behind a leave save: that clears the box
   * synchronously (`clearComposer`), so by the time the unmount that followed it
   * runs there is nothing dirty left to save.
   */
  useEffect(
    () => () => {
      if (!dirtyRef.current) return;
      unloadSave({});
    },
    [unloadSave],
  );

  const rowRef = useRef<HTMLDivElement | null>(null);
  // The host's ref MIRRORS ours rather than replacing it: `useAutoGrow` owns the
  // element it measures, and a modal's `initialFocus` only needs to be able to
  // reach it.
  useLayoutEffect(() => {
    if (hostBoxRef) hostBoxRef.current = boxRef.current;
  });
  const running =
    status === "running" || status === "starting" || status === "stopping";
  const fit = useRowFit(
    rowRef,
    columnRef,
    // The camera seat is IN the key and not merely a dependency of it: a seat
    // appearing or leaving changes `composerRowNeed` by a whole control plus a
    // gap, which is exactly the kind of change T's MutationObserver existed to
    // catch (T:12455-12474).
    `${controls.model}|${controls.effort}|${controls.permission}|${blocked ? 1 : 0}|${
      camera ? 1 : 0
    }|${String(fitRevision ?? "")}`,
  );

  // THIS IS THE NATIVE `initialFocus`, and it has to be, because a modal's
  // cannot be: `boxRef` is filled by the layout effect above, and with the chat
  // behind a `lazy` chunk the textarea does not exist yet at the moment
  // `Modal` computes `initialFocus` — so a host reading the ref there gets
  // `null` and falls back to the dialog's first focusable (the ✕). This effect
  // runs when the composer itself mounts, whenever that is, which is the only
  // moment at which "focus the composer" is a thing that can be done.
  // `preventScroll`: focus inside a scrolled log must not jump it (D348).
  useEffect(() => {
    if (autoFocus) boxRef.current?.focus({ preventScroll: true });
  }, [autoFocus, boxRef]);

  /**
   * THE CARET, TAKEN AND PUT AT THE END OF WHATEVER IS IN THE BOX.
   *
   * TWICE, and the second time deferred by a task rather than a frame: the box
   * this focuses can be REPLACED by the commit that follows (the auto-grow
   * relayout, the fit ladder's re-key), and a focus on a node that is no longer
   * in the document is a focus on nothing. `boxRef.current` is re-read inside
   * `put` so the retry lands on whatever node is there now, and the focus is
   * skipped when the caret is already home — so the common case costs one call.
   *
   * A TASK AND NOT `requestAnimationFrame`: a pane that is not on screen never
   * gets a frame, and a caret that only arrives when somebody is looking is a
   * caret that never arrives at all.
   *
   * Returns its own canceller, so every caller is an effect body's one-liner.
   */
  const takeCaret = useCallback(() => {
    let live = true;
    const put = () => {
      if (!live) return;
      const box = boxRef.current;
      if (!box) return;
      const doc = (box as { ownerDocument?: Document }).ownerDocument
        ?? (typeof document === "undefined" ? undefined : document);
      if (doc?.activeElement !== box) box.focus({ preventScroll: true });
      // Guarded because a textarea that is not in a document (and every test
      // double) may refuse the call, and a throw here would cost the focus as
      // well as the caret.
      try {
        const end = box.value.length;
        box.setSelectionRange(end, end);
      } catch {
        // No selection API — the focus above is the half that matters.
      }
    };
    put();
    const again = setTimeout(put, 0);
    return () => {
      live = false;
      clearTimeout(again);
    };
  }, [boxRef]);

  /**
   * …AND AGAIN ONCE A RESTORED DRAFT IS IN THE BOX (`seededAt`, Akshil QA
   * 2026-09-14), with the caret at the END of it.
   *
   * The seed answers well after the mount, so this is the call that actually
   * lands the caret for a session whose composer opened on stored words — and
   * the caret goes after them, because a caret in the middle of a restored
   * sentence is its own small bug.
   *
   * Gated on `autoFocus` like the mount effect above: a landing page, a preview
   * and a `noFocus` host must not be made to take the keyboard by a draft that
   * happened to load.
   */
  useEffect(() => {
    if (!seededAt || !autoFocus) return;
    return takeCaret();
  }, [seededAt, autoFocus, takeCaret]);

  // Stranded follow-ups come back. Keyed on `seq` and not on the text, so the
  // same words stranded twice are delivered twice — and the box takes the
  // keyboard, because there is now something in it the user has to decide about.
  // A DELIVERY LEDGER rather than a dependency list: the same words stranded
  // twice arrive as two deliveries with two `seq`s, and any re-render in between
  // must not re-append the one already taken.
  const delivered = useRef(0);
  useEffect(() => {
    if (!restore || restore.seq === delivered.current) return;
    // A stranded follow-up's text is never empty. The seat only ever APPENDS
    // now: the one caller that replaced the whole box was the draft MOVE out of
    // the Recent list, and a press on a draft row no longer moves anything — it
    // opens the record where it already lives (design §1).
    if (!restore.text) return;
    delivered.current = restore.seq;
    // Words going back INTO the box, so the stale-render latch stands down for
    // them exactly as it does for a keystroke (`spent`).
    spent.current = null;
    const back = restore.text;
    // A single newline, and only when there is something to join to: a press
    // must never eat words the reader is still typing.
    setText((prev) => (prev.trim() ? prev.replace(/\s*$/, "\n") + back : back));
    boxRef.current?.focus({ preventScroll: true });
    grow();
  }, [restore, boxRef, grow]);

  // A tray still uploading holds the send back rather than sending half of it.
  const attaching = !!attachPending;
  /**
   * THE ONE THING THAT DISABLES SEND, and it took an owner decision to put it
   * there (P4R1-2). `blocked` is chat-only for the same reason the box's own
   * `disabled` is — the landing card has no session for a message to be pending
   * IN (T:16851) — and `!running` is the half that must never be dropped: while
   * a turn streams this button IS the Stop, and a chat that cannot end its own
   * running turn is a worse state than the pollution the block prevents.
   */
  const sendBlocked = variant === "chat" && !!blocked && !running;
  /**
   * …AND THE HOP'S FREEZE IS UNDER THE SAME ROOF (Bugbot 4035295068).
   *
   * The freeze shut this button so a press could not spend the very words the
   * hop is carrying — a good rule about SEND, and a dead door on STOP. Mid-turn
   * this control is the Stop, the hop's own round trips can run for seconds on a
   * tray of pictures, and a reader who hits Schedule during a live reply would
   * have had no way to end it. `!running` for exactly the reason `sendBlocked`
   * carries it: nothing may take the exit away from a streaming turn.
   */
  const hopFrozen = hopping && !running;

  const submit = useCallback((seed?: string): boolean => {
    // Nothing leaves this composer while a scheduled message is pending — not a
    // typed line, not a follow-up (T:17871).
    if (blocked) return false;
    // ... nor while a chip is still attaching, on EITHER road: both of them
    // empty the tray, and both would leave the pending files behind. The box
    // KEEPS its words (the `setText("")` below is past this door), so the same
    // Enter a moment later sends the message the user actually wrote.
    if (attaching) return false;
    // ... nor while a Schedule hop is mid-air. Those words are already on their
    // way to a card; sending them here spends them twice, and the hop behind it
    // is left writing a message that has been said (Bugbot 4034977395).
    if (hoppingRef.current) return false;
    // ONE SEND AT A TIME. Checked BEFORE the box is cleared, so a keystroke
    // this refuses costs the user nothing.
    if (busyRef?.current || sendBusy) return false;
    // The programmatic send's seed, appended on the `restore` seat's own join
    // rule (a newline, and only when there is something to join to) — the box
    // may hold words the walkthrough's intro is being added to.
    const extra = typeof seed === "string" ? seed.trim() : "";
    const typed = text.trim();
    // A BLANK LINE between them, which is T:7391's own join (`v ? v + "\n\n" +
    // seed : seed`). Not cosmetic: a blank line is the paragraph boundary both
    // in the outgoing markdown and in the annotation stanza grammar, so a
    // single newline ran the reader's draft into the walkthrough's intro and
    // changed what the model reads.
    const message = extra ? (typed ? typed.replace(/\s*$/, "") + "\n\n" + extra : extra) : typed;
    if (!message && !hasAttachments) return false;
    // A SEND SPENDS THE BOX THE SAME WAY AN ANSWERED DIALOG DOES, and a render
    // older than this line would otherwise hand a sent sentence to the unmount
    // save as an unfinished task (`spent`).
    spent.current = boxShape(textRef.current, trayDraftRef.current.length);
    textRef.current = "";
    // BOTH MIRRORS, not just the words (Bugbot 4036328238). The tray mirror used
    // to be left holding the spent chips until a render replaced it — and the
    // latch, doing its job, is exactly what stops a stale render replacing it.
    // `dirtyRef` is made of the two, so a bare picture left in the mirror was an
    // "unsaved message" the unmount filed all over again.
    trayDraftRef.current = [];
    dirtyRef.current = false;
    // THE SEED'S ANSWER IS ABOUT A SENTENCE THAT HAS NOW BEEN SENT (`episode`,
    // Bugbot 4027549698). A GET dispatched on mount and landing after this line
    // used to find an empty box and fill it back up with the words this send
    // just spent.
    episode.current += 1;
    setText("");
    if (hasSession) {
      // THE DRAFT IS SPENT, AND SAYING SO IS THE WHOLE OF IT.
      //
      // `reset` first and with the value the box is ABOUT to have, so the render
      // that empties the box is not read as the reader clearing their draft.
      // `markDeleted` is then one statement to the one writer of this record:
      // the record should not exist. It waits for nothing and orders nothing — a
      // PUT already on the wire is what the syncer is waiting on anyway, and the
      // DELETE goes out behind it with a higher sequence, so the send can no
      // longer be overtaken by the autosave of the sentence it just sent.
      //
      // A FOLLOW-UP TYPED IN THE NEXT BREATH IS SAFE FOR THE SAME REASON. It is
      // a newer statement about the same key, so it supersedes the delete
      // instead of racing it: the record ends up holding the follow-up, and the
      // sent sentence is never resurrected on the way there.
      autosaveRef.current.reset({ text: "", attachments: [] });
      draftSyncer(draftKeyRef.current).markDeleted();
    }
    // A SESSION-LESS SEND WRITES NOTHING, because nothing wrote a record in the
    // first place: words typed in that box were never a draft until somebody
    // said so. There is no DELETE to fire and nothing to spend.
    // A live run gets this message DIRECTLY instead of parking it in a
    // page-side array (T:17889-17899).
    if (running && onFollowUp) onFollowUp(message);
    else {
      onSend(message, {
        model: controls.model,
        effort: controls.effort,
        permission: controls.permission,
      });
    }
    // AND THE CARET GOES BACK IN THE BOX (T:16687 — `scrollBottom();
    // focusBox(box)` in `sendMessage`'s `finally`). An Enter-send never noticed,
    // because focus was already there; clicking Send left it on `.c-send`, so
    // the next keystroke typed nothing and the reader had to click back into a
    // box they had just used. `preventScroll`, like every other focus call
    // here: the transcript's own follow effect owns the scroll, and a focus
    // that also scrolls fights it.
    boxRef.current?.focus({ preventScroll: true });
    return true;
  }, [blocked, attaching, busyRef, sendBusy, text, hasAttachments, hasSession, running, onFollowUp, onSend, controls, boxRef]);

  // The seat for the programmatic send. In an EFFECT so a render React throws
  // away (StrictMode's double invoke, a concurrent attempt that loses) cannot
  // leave its own `submit` installed for the recorder to press.
  useEffect(() => {
    if (!submitRef) return;
    submitRef.current = submit;
    return () => {
      if (submitRef.current === submit) submitRef.current = null;
    };
  }, [submitRef, submit]);

  const onKeyDown = useCallback(
    (ev: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (ev.key !== "Enter") return;
      // Shift+Enter is a newline. Enter never STOPS a run — a user drafting the
      // next message mid-run must not kill the turn with a keystroke meant to
      // queue text (T:17915). Cmd/Ctrl+Enter is the same send, for the hands
      // that learned it in every other composer in this app.
      if (ev.shiftKey) return;
      ev.preventDefault();
      submit();
    },
    [submit],
  );

  const draft = useCallback(() => text, [text]);
  const focusBox = useCallback(
    () => boxRef.current?.focus({ preventScroll: true }),
    [boxRef],
  );

  const count = queued?.length ?? 0;
  const collapsed = variant === "chat" && !active && !blocked;

  return (
    <>
      {/* THE TRAY IS A SIBLING ABOVE THE CARD, NOT A CHILD OF IT (T:4154's
          `<div class="annchips" id="annchips-chat">` and T:4220's home twin,
          both preceding the composer box). Rendered inside the form it was
          enclosed by the card's border and inset by the textarea's 15px gutter
          — a chip in a box instead of a chip floating above one, 30px narrower
          (visual pass 2, FIX-16). Nothing below moves: the textarea lands at
          the same y on both sides either way; what changes is which side of the
          border the chip is on. */}
      {chips}
      <form
        ref={formRef}
        className={collapsed ? "c-composer is-idle" : "c-composer"}
        // THE READER'S HAND opens the card — the pointer anywhere in it, or a
        // key in its box (`onKeyDown` below) — and focus leaving it folds the
        // card back (`onFormBlur`). Focus arriving is NOT enough on its own:
        // `autoFocus` and the caret-taking effects focus this box on arrival.
        onPointerDown={variant === "chat" ? onFormPointerDown : undefined}
        onFocus={variant === "chat" ? onFormFocus : undefined}
        onBlur={variant === "chat" ? onFormBlur : undefined}
        onSubmit={(ev) => {
          ev.preventDefault();
          // The submit event is the send BUTTON's path (Enter in the box never
          // reaches here): while a run is live the button is a stop button
          // (T:17909-17914).
          if (running) {
            onStop();
            return;
          }
          submit();
        }}
      >
        <textarea
          ref={boxRef}
          rows={variant === "home" ? 2 : 1}
          placeholder={
            blocked && blockedPlaceholder
              ? blockedPlaceholder
              : variant === "home"
                ? // `homePlaceholderFor` names the KIND once the pane has decided
                  // it ("Ask Claude about this project…"); the markup's own
                  // kind-free wording stands until then (T:5392).
                  placeholder || HOME_PLACEHOLDER
                : CHAT_PLACEHOLDER
          }
          spellCheck={false}
          // AND GRAMMARLY OFF, all three spellings, exactly as T:4156-4157 and
          // T:4227-4228 ship them beside `spellcheck`. Not cosmetic: Grammarly
          // injects a sibling contenteditable and a floating button INTO this
          // element's box, and `ui/fit.ts`'s `readRow` prices `row.children` — an
          // injected node in that chain is precisely the surprise a measured
          // ladder cannot absorb.
          data-gramm="false"
          data-gramm_editor="false"
          data-enable-grammarly="false"
          disabled={blocked}
          // READ-ONLY, NOT DISABLED, while a hop is out: the words are still the
          // reader's to see and to copy, and `disabled` would take the caret out
          // of the box mid-gesture. `[readonly]` wears the same dim as
          // `:disabled` (styles/composer.css).
          readOnly={hopping}
          value={text}
          onChange={(ev) => {
            const value = ev.currentTarget.value;
            // A KEYSTROKE IS NEVER THE STALE RENDER (`spent`). The reader
            // retyping the very sentence they just saved is a NEW set of words,
            // and a latch left standing would read the render that paints them
            // as the one that predates the clear.
            spent.current = null;
            setText(value);
            grow();
          }}
          onKeyDown={(ev) => {
            if (variant === "chat") engage();
            onKeyDown(ev);
          }}
          // WHO HAS THE CARET, for the conflict rule above and nothing else: a
          // record that changed elsewhere is adopted into a box nobody is
          // typing in, and never over one somebody is.
          onFocus={() => {
            focusedRef.current = true;
          }}
          onBlur={() => {
            focusedRef.current = false;
            // LEAVING THE BOX SENDS WHAT IS PENDING — on the road that has
            // something pending. The reader looking away is the likeliest moment
            // for a tab to be closed, a laptop to be shut or a link to be
            // followed, and 600 ms is a long time for the last sentence to exist
            // only here. A session-less box has nothing on the wire to flush:
            // looking away from it costs nothing and means nothing.
            if (hasSession) draftSyncer(draftKeyRef.current).flushNow();
          }}
          {...(onPaste ? { onPaste } : {})}
        />
        {/* A DIVERGENCE FROM T, RECORDED (visual pass 3, FIX-27). T has no
            queued line at all: its own queue handling (T:15905-15917) puts the
            stranded text back INTO the box, so the reader learns about it by
            finding their words there. This line is kept — a follow-up that has
            gone somewhere invisible is worse than 24px of composer card — but
            it is the one thing in this file that adds a box T does not draw
            (91 → 115 while a follow-up is pending), so it is written down here
            rather than left for a fourth visual pass to find again. */}
        {count > 0 && !queueOn ? (
          <div className="c-queued">
            {count === 1
              ? "1 follow-up is queued for this turn."
              : `${count} follow-ups are queued for this turn.`}
          </div>
        ) : null}
        {/* THE TOOLS' SHELF: a one-track grid whose row goes 1fr → 0fr while the
            composer is idle (styles/composer.css `.c-composer-tools`). A grid
            track is the one height that animates from "whatever the row needs"
            to nothing without a guessed `max-height`; the row itself is
            untouched, so the fit ladder still measures it. */}
        <div className="c-composer-tools">
        <div className="c-composer-row" ref={rowRef}>
          <ModelSelect value={controls.model} onChange={controls.setModel} />
          <EffortSelect value={controls.effort} onChange={controls.setEffort} />
          <PermissionSelect
            value={controls.permission}
            onChange={controls.setPermission}
            compact={fit !== "full"}
          />
          <span className="c-spacer" />
          {camera}
          {/* IMMEDIATELY LEFT OF SEND, and that seat is the whole idea: these two
              are the ways this draft leaves the box — now, or as a task
              (T:4174-4183). The landing card's copy is never blocked. */}
          <SchedButton
            file={file}
            sessionId={sessionId}
            draft={draft}
            {...(attachments ? { attachments } : {})}
            back={back}
            // TWO GUARDS WITH DIFFERENT SCOPES, which is what T:12075/12099 read
            // off `schedBlocked() || annNavLocked()` for every `.schedbtn`:
            //
            //   * `blocked` stays CHAT-ONLY (T:16851) — the landing card has no
            //     session holding queued work, so nothing there is blocked;
            //   * `navLocked` applies to BOTH, because a comment round owns the
            //     PAGE. `styles/ann.css`'s `pointer-events: none` stopped the
            //     mouse on the landing composer, but the button stayed in tab
            //     order — so a keyboard Enter still opened the confirm and
            //     Continue still left for `/tasks`, stranding the notes. That is
            //     the exact failure Bugbot PR #1046 closed, reachable again by
            //     another road. Disabled for the eye, guarded for the hand.
            //
            // The block reads the SAME `blocked` the box does — never a second
            // read of the schedule, because a parallel notion of "is this session
            // blocked" is two answers to one question (T:17233-17236).
            disabled={(variant === "chat" && !!blocked) || !!navLocked}
            {...(navLocked && navLockedReason
              ? { disabledReason: navLockedReason }
              : variant === "chat" && blocked && blockedReason
                ? { disabledReason: blockedReason }
                : {})}
            onCancel={focusBox}
            onNavigate={onNavigate}
            // THE HOP EMPTIES A SESSION-LESS BOX, and only that one. Continue
            // writes the record and leaves for the card, which is where those
            // words are edited from now on — keeping a second copy in a box that
            // does not autosave is the disagreement this design ends, and a
            // cleared composer also stands the leave guard down, so the hop's
            // own navigation does not save a SECOND draft of what Continue has
            // just filed.
            //
            // A SESSION'S BOX KEEPS ITS WORDS, because they are the same record:
            // its autosave is the writer the hop just handed off to, and
            // emptying the box here would be an immediate DELETE of what
            // Continue had written a tick earlier.
            {...(hasSession ? {} : { onHandedOff: clearComposer })}
            // WHICH WORDS, AND WHEN IT HAS THEM. The hop reads the episode back
            // right before it writes and abandons a press whose sentence has
            // since been sent or discarded; `onHopChange` is how this box knows
            // to stop offering those gestures for that window in the first
            // place.
            episode={readEpisode}
            onHopChange={setHopping}
          />
          <button
            className="c-send"
            type="submit"
            aria-label={running ? "Stop" : "Send"}
            // AND THE SHUTTER WINDOW SAYS SO TOO (Bugbot, PR #1074). With the
      // `disabled` attribute gone (T:4187), the `title` is the only thing left
      // that can tell the reader why a press does nothing — and `sendBusy` is
      // the window that can run to SECONDS on a large pane, where `attaching`
      // is usually a blink. A control that looks ready and silently refuses is
      // the one outcome dropping the dim must not buy.
      title={
        running
          ? // T:4187's own string, not the shorter one this shipped with
            // (visual pass 3, FIX-27). `aria-label` is `Stop` on both sides —
            // that is the control's NAME — and the tooltip is where legacy says
            // which stop it is: a turn's, not the recorder's or the app's.
            "Stop this turn"
          : sendBlocked
            ? // THE REASON, verbatim — the same sentence the banner shows and
              // the calendar button carries, so a reader refused here reads the
              // words they have already read six pixels above (T:17232-17250).
              blockedReason || BLOCKED_SEND_TITLE
            : attaching
              ? "Attaching…"
              : sendBusy
                ? "Taking the picture…"
                : hopping
                  ? // The one refusal here that ends by itself, so it names what
                    // is happening rather than something to go and fix.
                    "Finishing the handoff to the task card…"
                  : "Send"
      }
            // T NEVER DISABLES SEND — not for an empty box, not for a pending
            // scheduled message, not for anything. There is no `.send:disabled`
            // rule in the whole of T (T:2956-2981), the markup carries no
            // attribute (T:4187, T:4246) and no line of T's script ever sets one:
            // `applyComposerBlockState` disables the BOX (T:17218) and the
            // Schedule pill (T:17238) and leaves this button alone. T has no
            // `canSend` at all — the name in this app is T:7720's `activeRun ||
            // !sending`, which is the ANNOTATION send gate, not this button.
            //
            // So the refusals all live where T puts them: in the submit handler,
            // which swallows an empty send, a blocked composer, a chip still
            // attaching and a capture in flight. The dim bought nothing the
            // handler was not already doing, it was never reviewed (it appears in
            // none of PR1-R1..R4 or PR2-R1) and it cost the load-bearing half —
            // `disabled` also kills the STOP this button becomes mid-run, leaving
            // a reader no way out of a turn.
            //
            // The two transient windows T never had (`attaching`, `sendBusy`) say
            // so in the `title` instead, which is feedback without a dead door.
            //
            // ...WITH ONE OWNER-DECIDED EXCEPTION, AND IT IS THE SCHEDULE BLOCK
            // (P4R1-2, Akshil, 2026-09-10: Send should be disabled, and the
            // strip's three seats with it). The block is unlike every refusal
            // above it: not transient, not about this draft, and already
            // explained by a banner directly over the box — so an orange button
            // that swallows the press is the one case where the dim tells the
            // reader something the handler cannot. The rest of the rule stands
            // untouched: nothing here is disabled for an empty box, a chip still
            // attaching or a capture in flight.
            //
            // AND NEVER THE STOP. `!running` is load-bearing: a pending message
            // landing while an interactive turn streams must not strand the
            // reader with a reply they cannot end (T:17193-17195).
            // NO ATTRIBUTE TO BE FALSE. Spread rather than `disabled={x}`, so
            // every state but the block leaves this button exactly as PR3 has
            // it — `props.disabled === undefined`, which is what T's markup
            // carries and what this app's own suites read (P4 batch, "Send
            // carries no attribute to be false").
            // …AND WHILE A SCHEDULE HOP IS OUT, on the same argument: the press
            // is not transient-and-harmless, it would spend the very words the
            // hop is carrying (Bugbot 4034977395).
            {...(sendBlocked || hopFrozen ? { disabled: true } : {})}
          >
            {running ? (
              <svg
                width="14"
                height="14"
                viewBox="0 0 16 16"
                fill="none"
                aria-hidden="true"
              >
                <rect
                  x="4"
                  y="4"
                  width="8"
                  height="8"
                  rx="1.5"
                  fill="currentColor"
                />
              </svg>
            ) : (
              <svg
                width="14"
                height="14"
                viewBox="0 0 16 16"
                fill="none"
                aria-hidden="true"
              >
                <path
                  d="M8 13V3M8 3L3.5 7.5M8 3l4.5 4.5"
                  stroke="currentColor"
                  strokeWidth="2.2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            )}
          </button>
        </div>
        </div>
      </form>
    </>
  );
}

// ---- the chat composer ----------------------------------------------------

export type ComposerProps = Omit<ComposerCardProps, "variant"> & {
  /**
   * The live artifact strip's seat, and it is HERE because T puts it here: below
   * the composer (T:4203), so a page appearing never moves the box the user is
   * typing into.
   */
  artStrip?: ReactNode;
};

/** The chat view's composer: the card and the strip under it. The footnote
 *  that used to close the column ("Claude can read and edit files here…") is
 *  gone (Akshil, 2026-09-16) — with the composer opening as a pill, a line of
 *  small print under it was the tallest thing in the block. */
export function Composer({ artStrip, ...card }: ComposerProps) {
  return (
    <>
      <div className="c-composer-chat">
        <ComposerCard {...card} variant="chat" />
      </div>
      {artStrip}
    </>
  );
}
