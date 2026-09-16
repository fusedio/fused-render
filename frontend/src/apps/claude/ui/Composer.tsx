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
  deleteChatDraft,
  draftVersion,
  forgetDraftVersion,
  fetchChatDraft,
  saveChatDraft,
  useAutosave,
  type ChatDraft,
  type DraftAttachment,
} from "@platform/lib/drafts";
import { notify } from "@platform/lib/notifications";
import { onDraftChange } from "@shell/tasksPulse";
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
import { SchedButton } from "./SchedButton";

/** T:4227 / T:4156 — the box's own placeholder, verbatim. The chat one names
 *  who is being replied to; the landing one names the errand. */
export const CHAT_PLACEHOLDER = "Reply to Claude…";
export const HOME_PLACEHOLDER = "Ask Claude…";

/** The disabled Send's tooltip when the caller hands no reason (P4R1-2). The
 *  real sentence is `schedBlockReason`'s, threaded through `blockedReason`; this
 *  is the floor, so a dead control is never a dead control with nothing to say. */
export const BLOCKED_SEND_TITLE = "Waiting on a scheduled message";

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
  /**
   * A GESTURE ASKED FOR THE BOX — bump the number to ask again. Unlike
   * `autoFocus` this is not policy about arriving, it is a request, so it wins
   * over a host that keeps the keyboard elsewhere (see the effect that reads
   * it). `ClaudeChat` raises it when a never-sent chat's row is pressed.
   */
  focusRequest?: number;
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
   * …and the way back: the paths the task form was opened on, handed to the tray
   * on the mount that follows "Back to chat". These are task-shots-resident real
   * paths, so this is `addPaths`' errand — no upload, thumbnails through
   * /api/fs/raw.
   */
  onRestoreAttachments?(paths: string[]): void;
  /** Empty the tray without sending it — what adopting a record from elsewhere
   *  does to the files, the way `take()` does on Send. */
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

export function ComposerCard({
  variant,
  file,
  sessionId,
  controls,
  status,
  queued,
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
  focusRequest,
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
  // THE BOX OPENS EMPTY AND FILLS FROM THE RECORD (design "one record", §1).
  // There is no sessionStorage hop to read back any more: the Schedule button
  // carries a KEY, and the words it left behind are the server's copy — which
  // is the same copy this composer seeds from on an ordinary reload. One road
  // in, so a walk back from the task form cannot disagree with a refresh.
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
  useEffect(() => {
    if (focusRequest) setGestured(true);
  }, [focusRequest]);
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

  // ---- THE SERVER-SIDE DRAFT (design.md, "Client behavior / Chat composer") --
  //
  // What the reader typed and did not send, kept on the server so it survives a
  // reload, another window, and opening a different chat and coming back. ONE
  // record, and this is the only door into it from here: the Schedule hop edits
  // the same one (design §1), so there is no "the hop wins" rule left to state.
  //
  // The key is the session, or `new:<file>` while the chat has not got one yet
  // (chatDraftKey). A brand-new chat's first send both creates the session and
  // deletes the draft, so nothing is ever re-keyed under the reader.
  const draftKey = chatDraftKey(sessionId, file);
  // What the box holds RIGHT NOW, for the async seed below to check against —
  // `text` inside that closure is the value from the render that started the
  // fetch, which is precisely the one that may be stale by the time it answers.
  const textRef = useRef(text);
  textRef.current = text;
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
    if (seededKeys.current.has(draftKey)) return;
    seededKeys.current.add(draftKey);
    // Words typed before the GET answered are the reader's and outrank it.
    if (textRef.current) return;
    void fetchChatDraft(draftKey).then((saved) => {
      if (!saved) return;
      // WORDS TYPED WHILE THE FETCH WAS IN FLIGHT outrank anything it can
      // answer (design.md: "a composer that is focused ignores incoming draft
      // updates"). The test is the words, NOT the caret: `autoFocus` below puts
      // the caret in the box on mount, before any fetch can answer.
      if (textRef.current) return;
      if (saved.text) {
        setText(saved.text);
        // Restored words are already the server's words: tell the autosave so
        // the box coming back does not cost a PUT of the same text.
        autosaveRef.current.reset({ text: saved.text, attachments: saved.attachments ?? [] });
        grow();
        // …and the caret goes in after them (see `seededAt`).
        setSeededAt((n) => n + 1);
      }
      // The tray's half, through the same door "Back to chat" uses: these are
      // real paths, so they are registered rather than uploaded.
      if (saved.attachments?.length) {
        restoreAttachments.current?.(saved.attachments.map((a) => a.path));
      }
    });
  }, [draftKey, grow]);

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
  const focusedRef = useRef(false);
  const autosave = useAutosave(
    { text, attachments: trayDraft },
    (value, opts) => saveChatDraft(draftKey, value.text, value.attachments, opts),
    {
      conflict: {
        focused: () => focusedRef.current,
        localText: () => textRef.current,
        adopt: (record) => adoptRef.current(record as ChatDraft | null),
        onKept: () =>
          notify({ title: "Updated elsewhere, kept your text", tone: "info" }),
      },
    },
  );
  // `submit` is a useCallback built below; it needs the autosave handle, and the
  // handle's identity is stable, so it is read through the ref every other seat
  // in this file uses for the same reason.
  const autosaveRef = useRef(autosave);
  autosaveRef.current = autosave;
  const draftKeyRef = useRef(draftKey);
  draftKeyRef.current = draftKey;
  const discardAttachments = useRef(onDiscardAttachments);
  discardAttachments.current = onDiscardAttachments;
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
    setText(next);
    discardAttachments.current?.();
    if (files.length) restoreAttachments.current?.(files.map((a) => a.path));
    autosaveRef.current.reset({ text: next, attachments: files });
    grow();
  }, [grow]);
  const adoptRef = useRef(adoptRecord);
  adoptRef.current = adoptRecord;
  /**
   * THE RECORD CHANGED SOMEWHERE ELSE (design §3).
   *
   * `/api/tasks/changes` now pushes every announced draft key with its version,
   * so a second tab's save, a discard from the List, a Board drag that sent
   * these words, and `POST /api/schedule` deleting the draft it came from all
   * reach this box the same way and within a second. This replaced a `spent`
   * set, an `inflight` map and a listener that had to be awaited by its
   * announcer — all of which existed to order one document's writes against its
   * own reads, which a version does for every document at once.
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
        const key = draftKeyRef.current;
        const seen = draftVersion(key);
        if (gone.includes(key) && (certain || seen !== undefined)) {
          forgetDraftVersion(key);
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
   * …AND AGAIN ONCE A RESTORED DRAFT IS IN THE BOX (`seededAt`, Akshil QA
   * 2026-09-14), with the caret at the END of it.
   *
   * TWICE, and the second time deferred by a task rather than a frame: the box
   * this focuses can be REPLACED by the commit that follows (the auto-grow
   * relayout, the fit ladder's re-key), and a focus on a node that is no longer
   * in the document is a focus on nothing. `boxRef.current` is re-read inside
   * `put` so the retry lands on whatever node is there now, and it is skipped
   * when the caret is already home — so the common case costs one `focus`.
   *
   * A TASK AND NOT `requestAnimationFrame`: a pane that is not on screen never
   * gets a frame, and a caret that only arrives when somebody is looking is a
   * caret that never arrives for the test rig.
   *
   * Gated on `autoFocus` like the mount effect above: a landing page, a preview
   * and a `noFocus` host must not be made to take the keyboard by a draft that
   * happened to load.
   */
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
   * A GESTURE ASKED FOR THIS BOX (`focusRequest`) — which is NOT the same fact
   * as `autoFocus`, and the difference is the whole of this seat (Akshil QA,
   * 2026-09-14).
   *
   * `autoFocus` is ambient policy: "should this composer take the keyboard
   * merely by appearing". The explorer's folder pane answers NO and is right to
   * — `apps/explorer/ListingPreviewPane` mounts the chat with `noFocus` so the
   * listing keeps the keyboard. But pressing a never-sent chat's row in the
   * Recent list is a REQUEST for the composer: the whole content of that row is
   * an unsent sentence, and the press promises the next Enter sends it. Gating
   * that on the ambient answer is what left the caret on `<body>` in exactly
   * the pane the gesture lives in.
   *
   * A COUNTER, so the same request twice is two requests. The flag it raises
   * outlives this effect because the draft's own GET has not answered yet —
   * `seededAt` below takes the caret again once the words are actually in the
   * box, and it has to know the gesture happened.
   */
  const requested = useRef(false);
  useEffect(() => {
    if (!focusRequest) return;
    requested.current = true;
    return takeCaret();
  }, [focusRequest, takeCaret]);

  /**
   * …AND AGAIN ONCE A RESTORED DRAFT IS IN THE BOX, with the caret after it.
   *
   * The seed answers well after the mount, so this is the call that actually
   * lands the caret for a draft press — and it is also why `requested` is a ref
   * rather than a dependency: the request happened one commit and several
   * hundred milliseconds ago.
   */
  useEffect(() => {
    if (!seededAt || !(autoFocus || requested.current)) return;
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

  const submit = useCallback((seed?: string): boolean => {
    // Nothing leaves this composer while a scheduled message is pending — not a
    // typed line, not a follow-up (T:17871).
    if (blocked) return false;
    // ... nor while a chip is still attaching, on EITHER road: both of them
    // empty the tray, and both would leave the pending files behind. The box
    // KEEPS its words (the `setText("")` below is past this door), so the same
    // Enter a moment later sends the message the user actually wrote.
    if (attaching) return false;
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
    setText("");
    // THE DRAFT IS SPENT. `reset` first and with the value the box is ABOUT to
    // have, so a write debounced a keystroke ago has nothing left to say, and so
    // an answer to a write already out speaks for nobody.
    //
    // …AND THE DELETE WAITS FOR WHAT IS ALREADY ON THE WIRE (Bugbot, PR #1180).
    // A version refuses a LATER write stating a STALE number; it has no opinion
    // about a PUT and a DELETE dispatched against the SAME one, which is exactly
    // this pair — the autosave read version 7, the DELETE would too, and the
    // server takes whichever arrives second. When that was the PUT, the sent
    // message came back as a live draft. `settle()` puts them in order here
    // instead: the PUT lands, this client takes the version it made, and the
    // DELETE states that one. Un-awaited by `submit` itself, because a send must
    // still be one tick for the caller.
    const key = draftKeyRef.current;
    autosaveRef.current.reset({ text: "", attachments: [] });
    void autosaveRef.current.settle().then(() => deleteChatDraft(key));
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
  }, [blocked, attaching, busyRef, sendBusy, text, hasAttachments, running, onFollowUp, onSend, controls, boxRef]);

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
          value={text}
          onChange={(ev) => {
            const value = ev.currentTarget.value;
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
        {count > 0 ? (
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
            {...(sendBlocked ? { disabled: true } : {})}
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
