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
import "../styles/composer.css";
import type { PermissionMode } from "../protocol/types";
import type { RunStatus, SendOptions } from "../protocol/controller-api";
import {
  applyLead2,
  fitFlags,
  fitSelect,
  footnoteTight,
  measureRowNeed,
  pickRowFit,
  type RowFit,
} from "./fit";
import { PERMISSION_SHORT } from "./composer-defaults";
import { EffortSelect } from "./EffortSelect";
import { ModelSelect } from "./ModelSelect";
import { PermissionSelect } from "./PermissionSelect";
import { SchedButton } from "./SchedButton";
import { takeDraft } from "./sched-draft";

/** T:4227 / T:4156 — the box's own placeholder, verbatim. The chat one names
 *  who is being replied to; the landing one names the errand. */
export const CHAT_PLACEHOLDER = "Reply to Claude…";
export const HOME_PLACEHOLDER = "Ask Claude…";

/** The footnote's two sentences. The text lives HERE and nowhere else, and the
 *  second one is the half a narrow column drops (T:4213, 12382-12392). */
export const FOOTNOTE_LEAD = "Claude can read and edit files here.";
export const FOOTNOTE_TAIL = " Approvals control what runs without asking.";

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

/** Two lines is the footnote's budget; measured in LINES rather than at a
 *  width, because the sentence's own length is the other variable (T:12369). */
function useFootnoteFit(
  ref: React.RefObject<HTMLElement | null>,
  revision: unknown,
): void {
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => {
      el.classList.remove("is-tight");
      if (!el.offsetWidth) return; // hidden — the landing page has no footnote
      const cs = getComputedStyle(el);
      if (
        footnoteTight(
          el.clientHeight,
          parseFloat(cs.paddingTop) || 0,
          parseFloat(cs.paddingBottom) || 0,
          parseFloat(cs.lineHeight) || 0,
        )
      ) {
        el.classList.add("is-tight");
      }
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(el.parentElement ?? el);
    return () => observer.disconnect();
  }, [ref, revision]);
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
  /** The landing card's kind-dependent placeholder (`homePlaceholderFor`,
   *  T:5392). Unset keeps the markup's own kind-free wording. */
  placeholder?: string;
  /**
   * Follow-ups the CLI never delivered, handed BACK to the box they were typed
   * in (`still_queued`, T:15911). `seq` is what re-delivers the same text (two
   * stops can strand the same words), and the text is APPENDED rather than
   * assigned, because the user may already be typing the next thing.
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
  onPaste,
  camera,
  fitRevision,
  back,
  onNavigate,
}: ComposerCardProps) {
  // The other half of the scheduler round trip, put back into an EMPTY box
  // only, and the stash is spent either way (T:12105-12118).
  const [text, setText] = useState(() => takeDraft(file));
  const { ref: boxRef, grow } = useAutoGrow(text);
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

  // Stranded follow-ups come back. Keyed on `seq` and not on the text, so the
  // same words stranded twice are delivered twice — and the box takes the
  // keyboard, because there is now something in it the user has to decide about.
  // A DELIVERY LEDGER rather than a dependency list: the same words stranded
  // twice arrive as two deliveries with two `seq`s, and any re-render in between
  // must not re-append the one already taken.
  const delivered = useRef(0);
  useEffect(() => {
    if (!restore || !restore.text || restore.seq === delivered.current) return;
    delivered.current = restore.seq;
    const back = restore.text;
    setText((prev) => (prev.trim() ? prev.replace(/\s*$/, "\n") + back : back));
    boxRef.current?.focus({ preventScroll: true });
    grow();
  }, [restore, boxRef, grow]);

  // A tray still uploading holds the send back rather than sending half of it.
  const attaching = !!attachPending;
  const canSend = !attaching && (text.trim().length > 0 || !!hasAttachments);

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

  return (
    <form
      className="c-composer"
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
      {chips}
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
          setText(ev.currentTarget.value);
          grow();
        }}
        onKeyDown={onKeyDown}
        {...(onPaste ? { onPaste } : {})}
      />
      {count > 0 ? (
        <div className="c-queued">
          {count === 1
            ? "1 follow-up is queued for this turn."
            : `${count} follow-ups are queued for this turn.`}
        </div>
      ) : null}
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
          disabled={(variant === "chat" && !!blocked) || !!navLocked}
          {...(navLocked && navLockedReason ? { disabledReason: navLockedReason } : {})}
          onCancel={focusBox}
          onNavigate={onNavigate}
        />
        <button
          className="c-send"
          type="submit"
          aria-label={running ? "Stop" : "Send"}
          title={running ? "Stop" : attaching ? "Attaching…" : "Send"}
          // T NEVER DISABLES SEND (T:17193-17195, T:4187), and `blocked` here
          // was the one thing that did. The send door already refuses a blocked
          // composer at `submit`'s first guard, so the dim bought nothing and
          // cost the load-bearing half: `disabled` also kills the STOP this
          // button becomes mid-run, and a reader who cannot stop a turn has no
          // way out of it. `canSend`/`sendBusy` stay — those are "there is
          // nothing to send yet", which is a different sentence.
          disabled={!running && (!canSend || !!sendBusy)}
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
    </form>
  );
}

// ---- the chat composer ----------------------------------------------------

export type ComposerProps = Omit<ComposerCardProps, "variant"> & {
  /** The target's kind, as `setTargetNoun` writes it — "files here" by
   *  default, which is the kind-FREE wording the markup ships (T:4205-4213). */
  footnote?: string;
};

/** The chat view's composer: the card, plus the footnote whose second sentence
 *  a narrow column drops. */
export function Composer({ footnote, ...card }: ComposerProps) {
  const footRef = useRef<HTMLDivElement | null>(null);
  const lead = footnote ?? FOOTNOTE_LEAD;
  useFootnoteFit(footRef, lead);
  return (
    <>
      <div className="c-composer-chat">
        <ComposerCard {...card} variant="chat" />
      </div>
      <div className="c-footnote" ref={footRef}>
        {lead}
        <span className="c-fn-more">{FOOTNOTE_TAIL}</span>
      </div>
    </>
  );
}
