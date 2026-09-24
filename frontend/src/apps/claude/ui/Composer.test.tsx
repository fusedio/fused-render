import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
/**
 * WHAT THE PAGE LISTENS FOR, RECORDED BEFORE ANYTHING CAN ARM IT.
 *
 * `drafts.listen` binds ONE `blur`/`pagehide` pair for every syncer at once,
 * the first time any syncer is made — and the shim's `window.addEventListener`
 * is a no-op, so a listener armed before this line is a listener no test can
 * fire. This runs above the imports for that reason: nothing here calls
 * `draftSyncer` at module scope, so this suite's own first mount is what arms it.
 */
const winListeners: Record<string, ((ev: unknown) => void)[]> = {};
(globalThis.window as unknown as {
  addEventListener(t: string, fn: (ev: unknown) => void): void;
  removeEventListener(t: string, fn: (ev: unknown) => void): void;
}).addEventListener = (t, fn) => {
  (winListeners[t] ||= []).push(fn);
};
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const {
  ComposerCard, BLOCKED_SEND_TITLE, CHAT_PLACEHOLDER, HOME_PLACEHOLDER, freshBox,
} = await import("./Composer");
const { DEFAULT_EFFORT, DEFAULT_MODEL, DEFAULT_PERMISSION } =
  await import("./composer-defaults");
const { draftVersion, forgetDraftVersion, peekDraftSyncer, resetDraftSyncers } =
  await import("@platform/lib/drafts");
const { SchedButton } = await import("./SchedButton");
import type { TaskDraftForm } from "@platform/lib/drafts";

const realFetch = globalThis.fetch;
beforeEach(() => {
  (globalThis as { fetch: unknown }).fetch = () =>
    Promise.resolve(new Response("{}", { status: 200 }));
});

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  (globalThis as { fetch: unknown }).fetch = realFetch;
  // MODULE STATE, and `bun test` runs every suite in one process: a syncer left
  // wanting something writes into the next test's fetch ledger, and a remembered
  // version makes the next mount's first PUT conditional on a record that test
  // never made.
  resetDraftSyncers();
});

const controls = {
  model: DEFAULT_MODEL,
  effort: DEFAULT_EFFORT,
  permission: DEFAULT_PERMISSION,
  setModel() {},
  setEffort() {},
  setPermission() {},
};

interface Sent {
  text: string;
  model?: string;
}

function mount(over: Partial<Parameters<typeof ComposerCard>[0]> = {}) {
  const sent: Sent[] = [];
  const followups: string[] = [];
  let stops = 0;
  let renderer: ReactTestRenderer | undefined;
  const card = (extra: Partial<Parameters<typeof ComposerCard>[0]> = {}) => (
    <ComposerCard
      variant="chat"
      file="/p/app.py"
      sessionId=""
      controls={controls}
      status="idle"
      back="/explorer/view/p"
      onSend={(text, opts) => sent.push({ text, model: opts.model })}
      onFollowUp={(text) => followups.push(text)}
      onStop={() => {
        stops += 1;
      }}
      {...over}
      {...extra}
    />
  );
  act(() => {
    renderer = create(card());
  });
  mounted.push(renderer!);
  const root = renderer!.root;
  const box = () => root.findByType("textarea");
  const type = (value: string) =>
    act(() => {
      box().props.onChange({ currentTarget: { value } });
    });
  const press = (
    key: string,
    mods: { shiftKey?: boolean; metaKey?: boolean; ctrlKey?: boolean } = {},
  ) => {
    let prevented = false;
    act(() => {
      box().props.onKeyDown({
        key,
        shiftKey: false,
        ...mods,
        preventDefault() {
          prevented = true;
        },
      });
    });
    return prevented;
  };
  const submitForm = () =>
    act(() => {
      root.findByType("form").props.onSubmit({ preventDefault() {} });
    });
  return {
    root,
    box,
    type,
    press,
    submitForm,
    sent,
    followups,
    stops: () => stops,
    /** For the one test that is ABOUT an unmount (a host that takes the pane
     *  away while the question is up). Unmounting twice is a no-op, so the
     *  `afterEach` sweep is unbothered. */
    unmount: () => act(() => renderer!.unmount()),
    /** The host handing this composer a prop it did not have — the session id
     *  the first send mints, which lands a render after the box does. */
    rerender: (extra: Partial<Parameters<typeof ComposerCard>[0]>) =>
      act(() => {
        renderer!.update(card(extra));
      }),
  };
}

test("the placeholder names who is being replied to, per variant (T:4156/4227)", () => {
  expect(mount().box().props.placeholder).toBe(CHAT_PLACEHOLDER);
  expect(mount({ variant: "home" }).box().props.placeholder).toBe(
    HOME_PLACEHOLDER,
  );
  expect(CHAT_PLACEHOLDER).toBe("Reply to Claude…");
  expect(HOME_PLACEHOLDER).toBe("Ask Claude…");
});

test("Enter sends and clears; Shift+Enter is a newline (T:17916)", () => {
  const c = mount();
  c.type("ship it");
  expect(c.press("Enter", { shiftKey: true })).toBe(false);
  expect(c.sent).toEqual([]);
  expect(c.box().props.value).toBe("ship it");

  expect(c.press("Enter")).toBe(true);
  expect(c.sent).toEqual([{ text: "ship it", model: DEFAULT_MODEL }]);
  expect(c.box().props.value).toBe("");
});

test("Cmd/Ctrl+Enter is the same send", () => {
  const c = mount();
  c.type("go");
  expect(c.press("Enter", { metaKey: true })).toBe(true);
  expect(c.sent.map((s) => s.text)).toEqual(["go"]);
});

test("an empty box sends nothing, and neither does a whitespace one", () => {
  const c = mount();
  c.press("Enter");
  c.type("   ");
  c.press("Enter");
  expect(c.sent).toEqual([]);
});

test("a bare attachment is sendable with no words at all (T:17903)", () => {
  const c = mount({ hasAttachments: true });
  c.press("Enter");
  expect(c.sent).toEqual([{ text: "", model: DEFAULT_MODEL }]);
});

test("a chip still ATTACHING holds the send back and keeps the words", () => {
  // `hasAttachments` counts in-flight placeholders but `take()` leaves them in
  // the tray, so a send fired now goes out without them — wordless it is an
  // EMPTY send, worded it is the message minus its files (Bugbot, PR #1064).
  const c = mount({ hasAttachments: true, attachPending: true });
  const send = () => c.root.findByProps({ className: "c-send" });
  // The refusal is the HANDLER's, not the attribute's (T:4187 — nothing in T
  // ever disables this button): the `title` is what says why.
  expect(send().props.disabled).toBeUndefined();
  expect(send().props.title).toBe("Attaching…");

  // Wordless: nothing at all leaves.
  c.press("Enter");
  expect(c.sent).toEqual([]);

  // Worded: refused, and THE BOX IS KEPT — the same Enter a moment later is the
  // message the user actually wrote.
  c.type("look at these");
  expect(c.press("Enter")).toBe(true);
  expect(c.sent).toEqual([]);
  expect(c.box().props.value).toBe("look at these");
  // The button is the same door.
  c.submitForm();
  expect(c.sent).toEqual([]);

  // And a follow-up road is no way around it.
  const live = mount({ status: "running", hasAttachments: true, attachPending: true });
  live.type("and this");
  live.press("Enter");
  expect(live.followups).toEqual([]);
  expect(live.stops()).toBe(0);
});

test("the bytes land ⇒ the send opens again", () => {
  const c = mount({ hasAttachments: true, attachPending: false });
  expect(c.root.findByProps({ className: "c-send" }).props.title).toBe("Send");
  c.press("Enter");
  expect(c.sent).toEqual([{ text: "", model: DEFAULT_MODEL }]);
});

test("Enter NEVER stops a run — it hands the text to the live turn (T:17915)", () => {
  const c = mount({ status: "running" });
  c.type("also fix the tests");
  c.press("Enter");
  expect(c.stops()).toBe(0);
  expect(c.followups).toEqual(["also fix the tests"]);
  expect(c.sent).toEqual([]);
});

test("the submit button is the ONLY way to stop: it is a stop square while live", () => {
  const idle = mount();
  idle.type("x");
  const send = idle.root
    .findAllByType("button")
    .find((b) => b.props.className === "c-send");
  expect(send!.props["aria-label"]).toBe("Send");

  const live = mount({ status: "running" });
  const stop = live.root
    .findAllByType("button")
    .find((b) => b.props.className === "c-send");
  expect(stop!.props["aria-label"]).toBe("Stop");
  live.submitForm();
  expect(live.stops()).toBe(1);
  expect(live.sent).toEqual([]);
});

test("send is disabled ONLY by the schedule block — never for empty, attaching or busy (T:4187)", () => {
  // The DELETED test's subject, with T's expectation, and the ONE exception the
  // owner added on top of it (P4R1-2, 2026-09-10). `.c-send` carries no
  // `disabled` for an empty box and none through either transient window: T has
  // no `.send:disabled` rule (T:2956-2981), no attribute in the markup (T:4187,
  // T:4246) and no script line that sets one — `applyComposerBlockState`
  // reaches the box (T:17218) and the Schedule pill (T:17238) and stops there.
  // T has no `canSend` either: that name is T:7720's annotation send gate.
  //
  // The block is different in kind: not transient, not about this draft, and
  // already explained by a banner over the box.
  const send = (c: ReturnType<typeof mount>) =>
    c.root.findAllByType("button").find((b) => b.props.className === "c-send")!;
  for (const props of [
    {},
    { hasAttachments: true, attachPending: true },
    { sendBusy: true, hasAttachments: true },
    // Mid-run the button IS the Stop, and a block may never take it.
    { status: "running" as const, blocked: true },
    // The landing card has no session for a message to be pending in.
    { variant: "home" as const, blocked: true },
  ]) {
    expect(send(mount(props)).props.disabled).toBeUndefined();
  }
  // ...and the block, which does — with the reason on the tooltip, since a
  // disabled control is out of tab order and the attribute cannot speak.
  const shut = send(mount({ blocked: true, blockedReason: "TASK-3 runs at 09:00" }));
  expect(shut.props.disabled).toBe(true);
  expect(shut.props.title).toBe("TASK-3 runs at 09:00");
  // No reason handed in is still not a dead control with nothing to say.
  expect(send(mount({ blocked: true })).props.title).toBe(BLOCKED_SEND_TITLE);
  // …and the refusals that remain are still MADE, in `submit`: an empty box
  // with nothing typed, and a worded send through a door that is shut. The
  // send window (`sendBusy`) is NOT one of those any more — the parent parks
  // the line (ClaudeChat's outbox), so the composer hands it over.
  const empty = mount();
  empty.submitForm();
  expect(empty.sent).toEqual([]);
  for (const props of [{ blocked: true }, { hasAttachments: true, attachPending: true }]) {
    const c = mount(props);
    c.type("try it");
    c.submitForm();
    expect(c.sent).toEqual([]);
    expect(c.followups).toEqual([]);
  }
  const busy = mount({ sendBusy: true });
  busy.type("try it");
  busy.submitForm();
  expect(busy.sent.map((s) => s.text)).toEqual(["try it"]);
});

test("the hop freeze shuts Send — but NEVER the Stop (Bugbot 4035295068)", () => {
  // The freeze exists so a press cannot spend the words the hop is carrying.
  // Mid-turn that same control is the Stop, and the hop's round trips run for a
  // second per picture: shutting it there left a reader watching a reply they
  // could not end. Same `!running` the schedule block has always carried.
  const send = (c: ReturnType<typeof mount>) =>
    c.root.findAllByType("button").find((b) => b.props.className === "c-send")!;
  const hop = (c: ReturnType<typeof mount>, on: boolean) =>
    act(() => {
      (c.root.findByType(SchedButton).props as {
        onHopChange(on: boolean): void;
      }).onHopChange(on);
    });

  // Idle: the freeze is about SEND, and it shuts it, with the transient reason
  // on the tooltip.
  const idle = mount();
  hop(idle, true);
  expect(send(idle).props.disabled).toBe(true);
  expect(send(idle).props.title).toBe("Finishing the handoff to the task card…");
  // …and the hop ending gives the door back.
  hop(idle, false);
  expect(send(idle).props.disabled).toBeUndefined();

  // Mid-run: the control is the Stop, and the freeze may not touch it.
  const live = mount({ status: "running" });
  hop(live, true);
  expect(send(live).props["aria-label"]).toBe("Stop");
  expect(send(live).props.disabled).toBeUndefined();
  expect(send(live).props.title).toBe("Stop this turn");
  // And it still stops: the form's submit reaches `onStop` before any send
  // guard, so the press ends the turn while the hop is out.
  live.submitForm();
  expect(live.stops()).toBe(1);
  expect(live.sent).toEqual([]);
});

test("send is NEVER disabled for having nothing to send (T:2956-2981)", () => {
  // T has no `.send:disabled` rule at all and never sets the attribute: an
  // empty submit is swallowed in the handler, which is what `submit`'s own
  // first guard does here. The dim was an unreviewed divergence, and the
  // nearest owner signal points the other way (PR2-R1's P2-2, "never disabled
  // unless a mode is active", resolved by hiding rather than disabling).
  const c = mount();
  const send = () =>
    c.root.findAllByType("button").find((b) => b.props.className === "c-send")!;
  expect(send().props.disabled).toBeUndefined();
  c.type("hi");
  expect(send().props.disabled).toBeUndefined();

  // …and it still REFUSES: pressing it with an empty box sends nothing.
  const empty = mount();
  act(() => {
    empty.root.findByType("form").props.onSubmit({ preventDefault() {} });
  });
  expect(empty.sent).toEqual([]);
});

test("a capture in flight names itself on the button — and the line still goes", () => {
  // `sendBusy` is the shutter window, which can run to seconds on a large pane.
  // The `title` says so (Bugbot, PR #1074). It used to REFUSE the send too, in
  // the handler, and the words sat in the box with no other sign — the exact
  // window in which a fast second line was lost (multi-send QA 2026-09-19).
  // The parent parks the line now, so the composer hands it over and clears.
  const c = mount({ sendBusy: true, hasAttachments: true });
  const send = c.root.findAllByType("button").find((b) => b.props.className === "c-send")!;
  expect(send.props.disabled).toBeUndefined();
  expect(send.props.title).toBe("Taking the picture…");
  c.type("with this shot");
  c.press("Enter");
  expect(c.sent.map((s) => s.text)).toEqual(["with this shot"]);
  expect(c.box().props.value).toBe("");
});

test("a blocked composer takes no input by any path (T:17871)", () => {
  const c = mount({
    blocked: true,
    blockedPlaceholder: "TASK-3 runs at 09:00",
  });
  expect(c.box().props.disabled).toBe(true);
  expect(c.box().props.placeholder).toBe("TASK-3 runs at 09:00");
  c.type("sneak this in");
  c.press("Enter");
  expect(c.sent).toEqual([]);
});

test("A BLOCK NEVER TAKES STOP: the button still ends a live turn (T:17193-17195)", () => {
  // The block is the PENDENCY of a scheduled message, not the run — and a
  // pending message landing while an interactive turn streams must not strand
  // the user with a reply they cannot end. `disabled` may only ever suppress
  // the SEND half of this one button.
  const c = mount({
    status: "running",
    blocked: true,
    blockedPlaceholder: "TASK-3 runs at 09:00",
  });
  const stop = c.root
    .findAllByType("button")
    .find((b) => b.props.className === "c-send")!;
  expect(stop.props["aria-label"]).toBe("Stop");
  // No attribute at all, not `false`: PR3 removed `disabled` from this button
  // outright (T:4187), so there is nothing here to be false — and the owner's
  // P4R1-2 clause is `!running`, so a streaming turn keeps its way out.
  expect(stop.props.disabled).toBeUndefined();
  expect(stop.props.title).toBe("Stop this turn");
  c.submitForm();
  expect(c.stops()).toBe(1);
  // ...and the send half is still shut: the box is dead and nothing leaves it.
  expect(c.sent).toEqual([]);
  expect(c.followups).toEqual([]);
  // IDLE AND BLOCKED, the button is the Send and the Send is off (P4R1-2). The
  // handler's guard stays where it was — the attribute is the reader's signal,
  // not the enforcement.
  const idle = mount({ blocked: true });
  const send = idle.root
    .findAllByType("button")
    .find((b) => b.props.className === "c-send")!;
  expect(send.props["aria-label"]).toBe("Send");
  expect(send.props.disabled).toBe(true);
  idle.type("sneak this in");
  idle.submitForm();
  expect(idle.sent).toEqual([]);
});

test("queued follow-ups are named under the box, singular and plural", () => {
  const hint = (c: ReturnType<typeof mount>) =>
    c.root
      .findAllByProps({ className: "c-queued" })
      .map((n) => n.props.children);
  expect(hint(mount({ queued: ["a"] }))).toEqual([
    "1 follow-up is queued for this turn.",
  ]);
  expect(hint(mount({ queued: ["a", "b"] }))).toEqual([
    "2 follow-ups are queued for this turn.",
  ]);
  expect(hint(mount())).toEqual([]);
});

// ---- the programmatic send, and the latch around the send window ----------
//
// Both are PR #1074's answers, and both live here because this is the only
// component that can read the box: the walkthrough's intro is handed IN as an
// argument (it used to be staged through `restore` — a state write — and the
// send pressed from a `setTimeout(0)` that could beat it), and the door is held
// shut by a ref the parent takes in the same tick this component calls `onSend`.

type Seat = { current: ((seed?: string) => boolean) | null };

test("the programmatic send takes its words as an ARGUMENT, never through the box", () => {
  const seat: Seat = { current: null };
  const c = mount({ submitRef: seat });
  let answered = false;
  act(() => {
    answered = seat.current!("walk me through the header");
  });
  // It sent, and it sent the words it was handed — with no render in between
  // for a timer to lose them in.
  expect(answered).toBe(true);
  expect(c.sent.map((s) => s.text)).toEqual(["walk me through the header"]);
});

test("a seeded send JOINS what the reader had already typed, with a BLANK LINE", () => {
  // T:7391's own join — `el.value = v ? v + "\n\n" + seed : seed`. A blank line
  // is the paragraph boundary both in the outgoing markdown and in the
  // annotation stanza grammar, so a single newline ran the reader's draft into
  // the walkthrough's intro and changed what the model reads.
  const seat: Seat = { current: null };
  const c = mount({ submitRef: seat });
  c.type("here is the task");
  act(() => {
    seat.current!("and here is the walkthrough");
  });
  expect(c.sent.map((s) => s.text)).toEqual([
    "here is the task\n\nand here is the walkthrough",
  ]);
  expect(c.box().props.value).toBe("");
});

test("trailing whitespace in the draft does not become a THIRD newline", () => {
  const seat: Seat = { current: null };
  const c = mount({ submitRef: seat });
  c.type("here is the task   \n\n  ");
  act(() => {
    seat.current!("and here is the walkthrough");
  });
  expect(c.sent.map((s) => s.text)).toEqual([
    "here is the task\n\nand here is the walkthrough",
  ]);
});

test("the seat with NO seed is ✓ Done: notes alone, and a refusal says so", () => {
  const seat: Seat = { current: null };
  const done = mount({ submitRef: seat, hasAttachments: true });
  act(() => {
    seat.current!();
  });
  expect(done.sent.map((s) => s.text)).toEqual([""]);

  // A composer that cannot send answers `false`, which is what lets the caller
  // put the words back in the box instead of dropping them.
  const shut: Seat = { current: null };
  mount({ submitRef: shut, blocked: true });
  let answered = true;
  act(() => {
    answered = shut.current!("nowhere to put this");
  });
  expect(answered).toBe(false);
});

test("✓ Done's round is asked for LIVE, not read off the last paint", () => {
  // THE ⌘↩ BUG (Akshil, 2026-09-17). ✓ Done commits the open note card and
  // presses this seat inside one microtask: the store has the note, React has
  // not painted, so `hasAttachments` — a render-time snapshot taken before that
  // write — still says the message carries nothing. The send refused, and the
  // mode machine disarmed anyway: "my comment saved but it didn't push it in
  // the chat".
  //
  // `hasAttachmentsNow` is called HERE, in the tick the send happens, which is
  // the whole of the difference.
  const seat: Seat = { current: null };
  let round = false;
  const c = mount({ submitRef: seat, hasAttachments: false, hasAttachmentsNow: () => round });

  // Nothing to carry, an empty box: refused, exactly as before.
  let answered = true;
  act(() => {
    answered = seat.current!();
  });
  expect(answered).toBe(false);
  expect(c.sent).toHaveLength(0);

  // A note lands in the store. NO re-render, no new props — this is the state
  // the real ✓ Done presses in.
  round = true;
  act(() => {
    answered = seat.current!();
  });
  expect(answered).toBe(true);
  expect(c.sent.map((x) => x.text)).toEqual([""]);
});

test("the live round does not override the doors that refuse for a REASON", () => {
  // It answers "is there something to send", never "send it anyway": a blocked
  // box still refuses, and the caller still hears `false`.
  const shut: Seat = { current: null };
  const c = mount({ submitRef: shut, blocked: true, hasAttachmentsNow: () => true });
  let answered = true;
  act(() => {
    answered = shut.current!();
  });
  expect(answered).toBe(false);
  expect(c.sent).toHaveLength(0);
});

test("a second Enter inside the send window is NEVER refused: the words leave the box", () => {
  // The parent takes the latch inside `onSend`, in the very tick this call is
  // made. `submit` used to read it and return false with the words still in
  // the box — silently — so the reader typed on and the next Enter sent two
  // messages as one (multi-send QA 2026-09-19). The parent PARKS the second
  // line now (ClaudeChat's outbox), so the composer hands every line over.
  const sent: string[] = [];
  const c = mount({ onSend: (text: string) => sent.push(text) });
  c.type("ship it");
  c.press("Enter");
  // The parent's window is open now (`sendBusy`); the composer does not care.
  c.rerender({ sendBusy: true, onSend: (text: string) => sent.push(text) });
  c.type("and again");
  c.press("Enter");
  expect(sent).toEqual(["ship it", "and again"]);
  expect(c.box().props.value).toBe("");
});

test("a latched composer still SENDS — and never disarms Stop", () => {
  const send = (c: ReturnType<typeof mount>) =>
    c.root.findAllByType("button").find((b) => b.props.className === "c-send")!;
  const latched = mount({ sendBusy: true });
  latched.type("hi");
  // No attribute (T:4187): the latch is the parent's to park behind, not a
  // reason to grey the button.
  expect(send(latched).props.disabled).toBeUndefined();
  latched.submitForm();
  expect(latched.sent.map((s) => s.text)).toEqual(["hi"]);
  expect(latched.box().props.value).toBe("");
  // A live run's button is the only way to stop it (T:17909-17914): a latch on
  // the way in must not take that away.
  const live = mount({ sendBusy: true, status: "running" });
  expect(send(live).props["aria-label"]).toBe("Stop");
  expect(send(live).props.disabled).toBeUndefined();
});

// ---- the page outbox's seat: ↑ pulls back, Ctrl+Enter sends now -----------

test("↑ in an EMPTY box pulls the newest parked line back to edit", () => {
  let parked = ["first", "second"];
  const c = mount({
    queuedCount: parked.length,
    onPullQueued: () => parked.pop() ?? null,
  });
  expect(c.press("ArrowUp")).toBe(true);
  expect(c.box().props.value).toBe("second");
  // With words in the box, ↑ is the caret's: nothing is pulled.
  expect(c.press("ArrowUp")).toBe(false);
  expect(c.box().props.value).toBe("second");
  expect(parked).toEqual(["first"]);
});

test("↑ does nothing when nothing is parked, or when the seat is not wired", () => {
  const bare = mount();
  expect(bare.press("ArrowUp")).toBe(false);
  const empty = mount({ queuedCount: 0, onPullQueued: () => null });
  expect(empty.press("ArrowUp")).toBe(false);
});

test("the outbox hint names the parked lines under the box, and not-sent rows apart", () => {
  const hint = (c: ReturnType<typeof mount>) =>
    c.root.findAllByProps({ className: "c-queued c-outbox" }).map((n) => n.props.children);
  expect(hint(mount({ queuedCount: 1 }))).toEqual(["1 message waiting to send · ↑ to edit it"]);
  expect(hint(mount({ queuedCount: 2 }))[0]).toContain("2 messages");
  // A not-sent row is never "waiting to send" (Bugbot, PR #1323).
  expect(hint(mount({ queuedCount: 0, notSentCount: 1 }))[0]).toContain("1 message not sent");
  expect(hint(mount())).toEqual([]);
  // …but ↑ reaches it.
  let pulled = 0;
  const c = mount({ notSentCount: 1, onPullQueued: () => (pulled++, "back") });
  expect(c.press("ArrowUp")).toBe(true);
  expect(pulled).toBe(1);
});

test("CTRL+Enter while LIVE is send-now; Cmd+Enter is never (it is ✓ Done's chord)", () => {
  const now: string[] = [];
  const live = mount({ status: "running", onSendNow: (t: string) => now.push(t) });
  live.type("stop and do this");
  expect(live.press("Enter", { ctrlKey: true })).toBe(true);
  expect(now).toEqual(["stop and do this"]);
  expect(live.followups).toEqual([]);
  expect(live.box().props.value).toBe("");
  // Plain Enter while live is still the follow-up road…
  live.type("and then this");
  live.press("Enter");
  expect(live.followups).toEqual(["and then this"]);
  // …and so is ⌘↩: the annotation round owns that chord (`pressDoneChord`),
  // and a stop must never shadow it.
  live.type("cmd line");
  live.press("Enter", { metaKey: true });
  expect(live.followups).toEqual(["and then this", "cmd line"]);
  expect(now).toEqual(["stop and do this"]);
  // Idle: Ctrl+Enter is the ordinary send, `onSendNow` untouched.
  const idle = mount({ onSendNow: (t: string) => now.push(t) });
  idle.type("go");
  idle.press("Enter", { ctrlKey: true });
  expect(idle.sent.map((s) => s.text)).toEqual(["go"]);
  expect(now).toEqual(["stop and do this"]);
});

test("Ctrl+Enter while live with NO send-now seat falls back to the follow-up", () => {
  const c = mount({ status: "running" });
  c.type("x");
  c.press("Enter", { ctrlKey: true });
  expect(c.followups).toEqual(["x"]);
});

test("the ✓ Done seat reads the send window LIVE, not as it was when installed", () => {
  // `submit` is handed out once through `submitRef`; the window opens later,
  // on the parent's re-render. A seat that closed over `sendBusy` at install
  // time let a wordless round through into the parked road (Bugbot round 2).
  const seat: Seat = { current: null };
  const c = mount({ submitRef: seat, hasAttachments: true });
  const installed = seat.current!;
  c.rerender({ submitRef: seat, hasAttachments: true, sendBusy: true });
  let went = true;
  act(() => {
    went = installed();
  });
  expect(went).toBe(false);
  expect(c.sent).toEqual([]);
  // …and the moment the window closes, the same seat sends.
  c.rerender({ submitRef: seat, hasAttachments: true, sendBusy: false });
  act(() => {
    went = installed();
  });
  expect(went).toBe(true);
  expect(c.sent).toHaveLength(1);
});

test("a WORDLESS send inside the window still refuses — notes cannot be parked", () => {
  // The notes' photograph is taken at send time and the tray belongs to the
  // send in flight, so ✓ Done keeps its round armed and says so (ClaudeChat's
  // "Your notes were not sent: the last message is still going out").
  const c = mount({ sendBusy: true, hasAttachments: true });
  c.submitForm();
  expect(c.sent).toEqual([]);
  // Words, though, always go — parked by the parent.
  c.type("with words");
  c.submitForm();
  expect(c.sent.map((s) => s.text)).toEqual(["with words"]);
});

// ---- the caret goes back in the box (T:16687) ------------------------------

test("clicking Send puts focus back in the textarea", () => {
  // T:16687 — `scrollBottom(); focusBox(box)` in `sendMessage`'s `finally`. An
  // Enter-send never noticed, because focus was already there; clicking Send
  // left it on `.c-send`, so the next keystroke typed nothing and the reader
  // had to click back into a box they had just used.
  //
  // `createNodeMock` is how a ref reaches a real object under
  // react-test-renderer, which builds no host nodes of its own.
  const focused: Array<Record<string, unknown> | undefined> = [];
  const node = {
    focus: (opts?: Record<string, unknown>) => focused.push(opts),
    // `grow()` reads these on every keystroke.
    style: {} as Record<string, string>,
    scrollHeight: 20,
  };
  const boxRef = { current: null as unknown };
  // A non-null ref anywhere in this tree wakes the measured ladders, and they
  // read `getComputedStyle` — which this suite has no CSSOM for. A stub is
  // enough: nothing here asserts a measurement.
  const G = globalThis as Record<string, unknown>;
  const realCS = G.getComputedStyle;
  G.getComputedStyle = () => ({
    paddingTop: "0px",
    paddingBottom: "0px",
    lineHeight: "16px",
    paddingLeft: "0px",
    paddingRight: "0px",
    columnGap: "6px",
    marginLeft: "0px",
    marginRight: "0px",
    display: "flex",
  });
  let renderer!: ReactTestRenderer;
  try {
  act(() => {
    renderer = create(
      <ComposerCard
        variant="chat"
        file="/p/app.py"
        sessionId=""
        controls={controls}
        status="idle"
        back="/explorer/view/p"
        boxRef={boxRef as never}
        onSend={() => {}}
        onFollowUp={() => {}}
        onStop={() => {}}
      />,
      // ONLY the textarea: mocking every host node would give `rowRef` a
      // non-null element and wake the fit ladder, which reads
      // `getComputedStyle` — and this suite has no CSSOM.
      { createNodeMock: (el) => (el.type === "textarea" ? node : null) },
    );
  });
  mounted.push(renderer);
  const root = renderer.root;
  act(() => {
    root.findByType("textarea").props.onChange({ currentTarget: { value: "hello" } });
  });
  expect(focused).toHaveLength(0);

  act(() => {
    root.findByType("form").props.onSubmit({ preventDefault() {} });
  });

  // Once, and with `preventScroll` — the transcript's own follow effect owns
  // the scroll, and a focus that also scrolls fights it.
  expect(focused).toHaveLength(1);
  expect(focused[0]).toEqual({ preventScroll: true });
  } finally {
    if (realCS === undefined) delete G.getComputedStyle;
    else G.getComputedStyle = realCS;
  }
});

// ---- the two guards on the Schedule seat, and the one NOT on Send ----------

test("a nav lock disables the Schedule seat in BOTH composers, with the reason", () => {
  // T:12075/12099 guard every `.schedbtn` on `schedBlocked() || annNavLocked()`
  // via `querySelectorAll`. `pointer-events: none` stopped the mouse but left
  // the button in tab order, so a keyboard Enter still opened the confirm and
  // Continue still left for `/tasks`, stranding the notes — the exact failure
  // Bugbot PR #1046 closed, reachable again by another road.
  const REASON = "Finish or discard the notes first";
  for (const variant of ["chat", "home"] as const) {
    const c = mount({ variant, navLocked: true, navLockedReason: REASON });
    const seat = c.root
      .findAllByType("button")
      .find((b) => String(b.props.className ?? "").includes("c-schedbtn"))!;
    expect(seat.props.disabled).toBe(true);
    expect(seat.props.title).toBe(REASON);
    expect(String(seat.props["aria-label"])).toContain(REASON);
  }
});

test("the SCHEDULE block is chat-only, as T:16851 has it", () => {
  const seatOf = (c: ReturnType<typeof mount>) =>
    c.root
      .findAllByType("button")
      .find((b) => String(b.props.className ?? "").includes("c-schedbtn"))!;
  // A landing card has no session holding queued work, so nothing there is
  // blocked...
  expect(seatOf(mount({ variant: "home", blocked: true })).props.disabled).toBe(false);
  // ...while the chat's own seat is.
  expect(seatOf(mount({ variant: "chat", blocked: true })).props.disabled).toBe(true);
});

test("the seat's reason: the nav lock outranks the block, and the block is the fallback", () => {
  // ONE sentence with one author (T:17232-17250) — a reader refused by the
  // button reads the same words as the banner six pixels above it. Two guards
  // can be up at once, and then the reason has to pick: the nav lock is the
  // one the reader can act on (finish the notes), while the block lifts on its
  // own clock, so the lock speaks first.
  const NAV = "Finish or discard the notes first";
  const BLOCK = "TASK-3 runs at 09:00";
  const seatOf = (c: ReturnType<typeof mount>) =>
    c.root
      .findAllByType("button")
      .find((b) => String(b.props.className ?? "").includes("c-schedbtn"))!;

  const both = seatOf(
    mount({ blocked: true, blockedReason: BLOCK, navLocked: true, navLockedReason: NAV }),
  );
  expect(both.props.title).toBe(NAV);

  // The block alone, and the banner's own sentence is what the seat says.
  const blocked = seatOf(mount({ blocked: true, blockedReason: BLOCK }));
  expect(blocked.props.disabled).toBe(true);
  expect(blocked.props.title).toBe(BLOCK);
  expect(String(blocked.props["aria-label"])).toContain(BLOCK);

  // ...and never on the landing card, whose seat is not blocked at all, so
  // there is no refusal for a reason to explain.
  const home = seatOf(mount({ variant: "home", blocked: true, blockedReason: BLOCK }));
  expect(home.props.disabled).toBe(false);
  expect(home.props.title).not.toBe(BLOCK);
});

test("THE BLOCK TAKES SEND AND NEVER STOP (P4R1-2, T:17193-17195)", () => {
  // The owner reversed the dim for this ONE refusal (2026-09-10: "Send DISABLED
  // … Stop stays live if a run is streaming"). An orange button that swallows
  // the press is the wrong answer for a wait measured in minutes and explained
  // by a banner six pixels above the box — and the load-bearing half is kept
  // exactly: `disabled` also kills the STOP this button becomes mid-run, and a
  // reader who cannot stop a turn has no way out of it.
  const send = (c: ReturnType<typeof mount>) =>
    c.root.findAllByType("button").find((b) => b.props.className === "c-send")!;
  expect(send(mount({ blocked: true, hasAttachments: true })).props.disabled).toBe(true);
  // And mid-run it is the Stop, live.
  const running = mount({ blocked: true, status: "running" });
  expect(send(running).props.disabled).toBeUndefined();
  expect(send(running).props["aria-label"]).toBe("Stop");
  // The handler's own guard is untouched: the attribute is a signal, never the
  // enforcement (a keyboard road, a stale render, `submitRef`'s programmatic
  // send all still land on it).
  const c = mount({ blocked: true });
  c.type("sneak this in");
  c.press("Enter");
  c.submitForm();
  expect(c.sent).toEqual([]);
  expect(c.followups).toEqual([]);
});

// ---- the textarea's own attributes ----------------------------------------

test("the box opts out of Grammarly, all three spellings (T:4156-4157)", () => {
  // Not cosmetic: Grammarly injects a sibling contenteditable and a floating
  // button INTO this element's box, and `ui/fit.ts`'s `readRow` prices
  // `row.children` — an injected node in that chain is exactly the surprise a
  // measured ladder cannot absorb.
  const box = mount().box();
  expect(box.props.spellCheck).toBe(false);
  expect(box.props["data-gramm"]).toBe("false");
  expect(box.props["data-gramm_editor"]).toBe("false");
  expect(box.props["data-enable-grammarly"]).toBe("false");
});

// ---- NOTHING IS WRITTEN WHILE THE READER IS IN THE BOX ---------------------
//
// Akshil, 2026-09-16: "when I am in the composer, don't autosave as a draft —
// I'm already there."
//
// So this section is about REQUESTS THAT DO NOT HAPPEN: a box with no record
// behind it at all (no session, no held draft) reads nothing, writes nothing,
// and spends nothing on its Send. What the session road does instead is below
// it; what a box HOLDING an Upcoming draft does is the last section of this
// file. Every test counts the wire.

interface Req {
  url: string;
  method: string;
  /** A chat record's shape (`text`) and a task draft's (`title`/`description`),
   *  because a session-less box now writes the second — see "a never-sent chat
   *  saves a NEW draft every time" below. */
  body?: {
    text?: string;
    title?: string;
    description?: string;
    target?: string;
    attachments?: unknown[];
  };
  keepalive: boolean;
}

/** Every request this composer makes, and an answer bland enough for all of
 *  them. Returns the ledger, which starts empty on purpose: a composer that
 *  asks the server ANYTHING on mount is the thing this design removed. */
function watchFetch(): Req[] {
  const seen: Req[] = [];
  (globalThis as { fetch: unknown }).fetch = (
    url: string,
    init?: RequestInit & { keepalive?: boolean },
  ) => {
    seen.push({
      url: String(url),
      method: init?.method ?? "GET",
      // A TASK-SHOTS UPLOAD SENDS `FormData`, which is not JSON: parsing it
      // threw inside the stub, the copy came back rejected, and every Save with
      // a chip in the tray silently took the "could not attach every file" road.
      body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
      keepalive: !!init?.keepalive,
    });
    // THE BYTES BEHIND A TRAY CHIP, answered without a body stream: a real
    // `Response.blob()` resolves off a MACROTASK, and the lane tests below have
    // to wait in microtasks alone — a timer turn would flush the very render
    // they are staging.
    if (String(url).startsWith("/api/fs/raw")) {
      return Promise.resolve({
        ok: true,
        status: 200,
        blob: () => Promise.resolve(new Blob(["bytes"])),
      } as unknown as Response);
    }
    if (String(url) === "/api/schedule/shot") {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ path: "/task-shots/copy.png", kind: "image" }),
      } as unknown as Response);
    }
    return Promise.resolve(
      new Response(
        JSON.stringify({
          ok: true,
          chat: {},
          task: {},
          draft: { text: "", attachments: [], updated_at: 1, version: 1, form: {} },
        }),
        { status: 200 },
      ),
    );
  };
  return seen;
}

const tick = async (n = 8) => {
  for (let i = 0; i < n; i += 1) await Promise.resolve();
};

test("the box mounts EMPTY even when the server holds a draft for this key", async () => {
  // The old composer opened with `GET /api/drafts` and painted whatever came
  // back. It does not read the store at all now: saved drafts live in Upcoming
  // and are edited on the Tasks card, and a box that filled itself from a
  // record is a box with an opinion about words the reader did not just type.
  const seen: Req[] = [];
  (globalThis as { fetch: unknown }).fetch = (url: string) => {
    seen.push({ url: String(url), method: "GET", keepalive: false });
    return Promise.resolve(
      new Response(
        JSON.stringify({
          chat: { "new:/p/held.py": { text: "words the server is holding", attachments: [] } },
          task: {},
        }),
        { status: 200 },
      ),
    );
  };
  const c = mount({ file: "/p/held.py", sessionId: "" });
  await act(async () => {
    await tick();
  });
  expect(c.box().props.value).toBe("");
  // …and it did not even ask. No GET, so nothing to race and nothing to adopt.
  expect(seen).toEqual([]);
});

test("typing writes nothing — no debounce, no blur flush, no request at all", async () => {
  const seen = watchFetch();
  const c = mount({ file: "/p/quiet.py", sessionId: "" });
  c.type("half a thought");
  // Well past the 600 ms the autosave used to wait, and past anything a blur
  // would have flushed: the box simply holds the words.
  await act(async () => {
    await new Promise((r) => setTimeout(r, 700));
    await tick();
  });
  expect(seen).toEqual([]);
  expect(c.box().props.value).toBe("half a thought");
});

test("A SEND JUST SENDS: no DELETE, and no write of any kind", async () => {
  // The send used to say "this record is spent" beside an autosave PUT for the
  // same key, and the two had to be ordered against each other. Nothing wrote
  // the record, so there is nothing to spend.
  const seen = watchFetch();
  const c = mount({ file: "/p/sent.py", sessionId: "" });
  c.type("the message");
  expect(c.press("Enter")).toBe(true);
  expect(c.sent).toEqual([{ text: "the message", model: DEFAULT_MODEL }]);
  await act(async () => {
    await tick();
  });
  expect(seen).toEqual([]);
  expect(c.box().props.value).toBe("");
});

// ---- AND THE SESSION'S BOX KEEPS THE WHOLE DRAFT --------------------------
//
// The fork, in tests. A composer ON A SESSION is the only place that chat's
// unsent message is visible — the ✎ Draft chip on the row points here — so it
// seeds from the record, autosaves, and spends the draft on Send, exactly as it
// always did. Everything above this line is the OTHER road: `new:<file>`, whose
// record is an Upcoming row.

/** The store, as `GET /api/drafts` serves it, plus a bland answer for writes. */
function storeWith(chat: Record<string, unknown>): Req[] {
  const seen: Req[] = [];
  (globalThis as { fetch: unknown }).fetch = (
    url: string,
    init?: RequestInit & { keepalive?: boolean },
  ) => {
    const method = init?.method ?? "GET";
    seen.push({
      url: String(url),
      method,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
      keepalive: !!init?.keepalive,
    });
    if (method === "GET") {
      return Promise.resolve(
        new Response(JSON.stringify({ chat, task: {} }), { status: 200 }),
      );
    }
    return Promise.resolve(
      new Response(
        JSON.stringify({
          draft: method === "DELETE"
            ? null
            : { text: "", attachments: [], updated_at: 2, version: 9, form: {} },
        }),
        { status: 200 },
      ),
    );
  };
  return seen;
}

test("a session's composer SEEDS from the record — one GET, and the words land", async () => {
  const seen = storeWith({
    "sess-seed": {
      text: "the follow-up I never sent",
      attachments: [{ path: "/shots/a.png", name: "a.png", kind: "image" }],
      updated_at: 1,
      version: 3,
      form: {},
    },
  });
  const restored: string[][] = [];
  const c = mount({
    file: "/p/seeded.py",
    sessionId: "sess-seed",
    onRestoreAttachments: (paths: string[]) => {
      restored.push(paths);
    },
  });
  await act(async () => {
    await tick();
  });
  expect(c.box().props.value).toBe("the follow-up I never sent");
  // ONE read, and it is a read: nothing is written by a box that merely opened.
  expect(seen.filter((r) => r.method === "GET")).toHaveLength(1);
  expect(seen.filter((r) => r.method !== "GET")).toEqual([]);
  // …and the tray comes back with it: half a draft is not the draft.
  expect(restored).toEqual([["/shots/a.png"]]);
  forgetDraftVersion("sess-seed");
});

test("a NEW-CHAT composer does not read at all, however full the store is", async () => {
  // The same store, the same words — and a key of `new:<file>`, which is an
  // Upcoming row and not this box's business.
  const seen = storeWith({
    "new:/p/held.py": { text: "words the server is holding", attachments: [], version: 1 },
  });
  const c = mount({ file: "/p/held.py", sessionId: "" });
  await act(async () => {
    await tick();
  });
  expect(c.box().props.value).toBe("");
  expect(seen).toEqual([]);
});

test("a read that FAILED leaves the box empty and says nothing", async () => {
  // `fetchChatDraft` answers `undefined` for a GET that did not land, and the
  // only honest thing to do with "could not find out" is nothing at all — no
  // empty box painted over words, no toast about a blip.
  const seen: Req[] = [];
  (globalThis as { fetch: unknown }).fetch = (url: string) => {
    seen.push({ url: String(url), method: "GET", keepalive: false });
    return Promise.resolve(new Response("nope", { status: 500 }));
  };
  const c = mount({ file: "/p/offline.py", sessionId: "sess-offline" });
  await act(async () => {
    await tick();
  });
  expect(c.box().props.value).toBe("");
  expect(seen).toHaveLength(1);
});

test("a session's typing WAITS FOR A FLUSH, then goes out on the syncer's own ordered PUT", async () => {
  const seen = storeWith({});
  const c = mount({ file: "/p/typed.py", sessionId: "sess-typing" });
  await act(async () => {
    await tick();
  });
  c.type("a follow-up in progress");
  // ONE RULE FOR EVERY COMPOSER: the session road states its draft and starts
  // no timer, so 700 ms — well past the 600 ms an autosave would once have
  // taken — buys nothing on its own.
  await act(async () => {
    await new Promise((r) => setTimeout(r, 700));
    await tick();
  });
  expect(seen.filter((r) => r.method !== "GET")).toEqual([]);
  // The WINDOW losing focus is the save moment, and it is worth exactly one
  // write.
  await act(async () => {
    blurWindow("sess-typing");
    await tick();
  });
  const puts = seen.filter((r) => r.method === "PUT");
  expect(puts).toHaveLength(1);
  expect(puts[0]!.url).toBe("/api/drafts/chat/sess-typing");
  expect(puts[0]!.body?.text).toBe("a follow-up in progress");
  // THROUGH THE ONE WRITER, which is what `client` + `seq` in the body say: a
  // bare `saveChatDraft` from this file would carry neither, and could not be
  // ordered against anything else this page says about the key.
  expect(typeof (puts[0]!.body as { client?: unknown }).client).toBe("string");
  expect((puts[0]!.body as { seq?: unknown }).seq).toBe(1);
  // …and NO `form`: the composer has no opinion about a time or a repeat, so a
  // keystroke save may not wipe the ones a Schedule hop put on the record.
  expect((puts[0]!.body as { form?: unknown }).form).toBeUndefined();
  expect(c.box().props.value).toBe("a follow-up in progress");
  forgetDraftVersion("sess-typing");
});

test("LEAVING THE BOX is not leaving the page: a textarea blur alone writes nothing", async () => {
  // "Out of focus" is the WINDOW, not the textarea (Akshil, 2026-09-17). A
  // reader tabbing to the model picker and back is not a save moment, and the
  // flush the session road used to fire here made every such hop a PUT.
  const seen = storeWith({});
  const c = mount({ file: "/p/tabbed.py", sessionId: "sess-tabbed" });
  await act(async () => {
    await tick();
  });
  c.type("a sentence I am stepping away from");
  await act(async () => {
    c.box().props.onBlur();
    await new Promise((r) => setTimeout(r, 700));
    await tick();
  });
  expect(seen.filter((r) => r.method !== "GET")).toEqual([]);
  // …and the words are still here to be saved when a real save moment comes.
  await act(async () => {
    blurWindow("sess-tabbed");
    await tick();
  });
  const puts = seen.filter((r) => r.method === "PUT");
  expect(puts).toHaveLength(1);
  expect(puts[0]!.body?.text).toBe("a sentence I am stepping away from");
  forgetDraftVersion("sess-tabbed");
});

test("a session composer's UNMOUNT is a save: one PUT, under the session's key", async () => {
  // Leaving this chat for another, or closing the pane. Nothing is written per
  // keystroke any more, so the teardown has to carry the last sentence.
  const seen = storeWith({});
  const c = mount({ file: "/p/left.py", sessionId: "sess-leaving" });
  await act(async () => {
    await tick();
  });
  c.type("the last thing I typed before the pane closed");
  await act(async () => {
    c.unmount();
    await tick();
  });
  const puts = seen.filter((r) => r.method === "PUT");
  expect(puts).toHaveLength(1);
  expect(puts[0]!.url).toBe("/api/drafts/chat/sess-leaving");
  expect(puts[0]!.body?.text).toBe("the last thing I typed before the pane closed");
  forgetDraftVersion("sess-leaving");
});

test("a session's SEND spends the record — the DELETE it always fired", async () => {
  const seen = storeWith({});
  const c = mount({ file: "/p/spent.py", sessionId: "sess-send" });
  await act(async () => {
    await tick();
  });
  c.type("send this one");
  expect(c.press("Enter")).toBe(true);
  await act(async () => {
    await new Promise((r) => setTimeout(r, 700));
    await tick();
  });
  expect(c.sent).toEqual([{ text: "send this one", model: DEFAULT_MODEL }]);
  expect(seen.filter((r) => r.method === "DELETE")).toHaveLength(1);
  expect(seen.find((r) => r.method === "DELETE")!.url)
    .toBe("/api/drafts/chat/sess-send");
  expect(c.box().props.value).toBe("");
  forgetDraftVersion("sess-send");
});

test("a seed answering AFTER the send does not put the sentence back", async () => {
  // Bugbot 4027549698. The seed's GET used to be judged by one question — "is
  // the box empty?" — and a Send is exactly a box that has just become empty.
  // A read held open across one landed afterwards and painted the spent words
  // back, and `gone` then read them as a follow-up and kept them.
  const seen: Req[] = [];
  let answer: ((r: Response) => void) | undefined;
  (globalThis as { fetch: unknown }).fetch = (
    url: string,
    init?: RequestInit & { keepalive?: boolean },
  ) => {
    const method = init?.method ?? "GET";
    seen.push({
      url: String(url),
      method,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
      keepalive: !!init?.keepalive,
    });
    if (method === "GET") return new Promise<Response>((r) => { answer = r; });
    return Promise.resolve(
      new Response(JSON.stringify({ ok: true, draft: null }), { status: 200 }),
    );
  };
  const c = mount({ file: "/p/race.py", sessionId: "sess-race" });
  await act(async () => {
    await tick();
  });
  c.type("the sentence this send spends");
  expect(c.press("Enter")).toBe(true);
  expect(c.box().props.value).toBe("");
  // …and NOW the read lands, holding the record that send just deleted.
  await act(async () => {
    answer!(
      new Response(
        JSON.stringify({
          chat: {
            "sess-race": {
              text: "the sentence this send spends",
              attachments: [],
              updated_at: 1,
              version: 3,
              form: {},
            },
          },
          task: {},
        }),
        { status: 200 },
      ),
    );
    await tick();
  });
  expect(c.box().props.value).toBe("");
  await act(async () => {
    await new Promise((r) => setTimeout(r, 700));
    await tick();
  });
  // The send's own DELETE, and nothing else: no PUT putting the record back.
  expect(seen.filter((r) => r.method === "PUT")).toEqual([]);
  expect(seen.filter((r) => r.method === "DELETE")).toHaveLength(1);
  forgetDraftVersion("sess-race");
});

test("a seeded tray is never written as empty on the way in", async () => {
  // Bugbot 4027549715. `onRestoreAttachments` → `addPaths` commits the chips
  // past an await, so the render that paints the restored words still has an
  // EMPTY tray — and the autosave behind it pushed those words with no files
  // and persisted the wipe before the chips landed.
  const files = [
    { path: "/shots/a.png", name: "a.png", kind: "image" as const },
    { path: "/shots/b.csv", name: "b.csv", kind: "file" as const },
  ];
  const seen = storeWith({
    "sess-tray": {
      text: "two files and a sentence",
      attachments: files,
      updated_at: 1,
      version: 3,
      form: {},
    },
  });
  let land: (() => void) | undefined;
  let tray: { pending: boolean; view: string; name: string; kind: string }[] = [];
  const c = mount({
    file: "/p/tray.py",
    sessionId: "sess-tray",
    attachments: () => tray as never,
    onRestoreAttachments: (paths: string[]) =>
      new Promise<void>((resolve) => {
        land = () => {
          tray = paths.map((path) => ({
            pending: false,
            view: path,
            name: path.slice(path.lastIndexOf("/") + 1),
            kind: path.endsWith(".png") ? "image" : "file",
          }));
          resolve();
        };
      }),
  });
  await act(async () => {
    await new Promise((r) => setTimeout(r, 700));
    await tick();
  });
  // The words are on screen, the tray is still filling, and a box that merely
  // opened has written nothing.
  expect(c.box().props.value).toBe("two files and a sentence");
  expect(seen.filter((r) => r.method !== "GET")).toEqual([]);
  // …AND A READER TYPING INTO THAT GAP MAY NOT STATE AN EMPTY TRAY. The value
  // this box holds right now is "these words, no files", and saying it would
  // persist the wipe before the chips arrived — so not even a flush in the
  // middle of the gap (the window losing focus) may write it out.
  c.type("two files and a sentence, plus one more");
  await act(async () => {
    await new Promise((r) => setTimeout(r, 700));
    blurWindow("sess-tray");
    await tick();
  });
  expect(seen.filter((r) => r.method !== "GET")).toEqual([]);
  // The chips land; the next flush goes out, and it carries both files.
  await act(async () => {
    land!();
    await tick();
  });
  c.type("two files and a sentence, plus two more");
  await act(async () => {
    await new Promise((r) => setTimeout(r, 700));
    blurWindow("sess-tray");
    await tick();
  });
  const puts = seen.filter((r) => r.method === "PUT");
  expect(puts).toHaveLength(1);
  expect(puts[0]!.body?.text).toBe("two files and a sentence, plus two more");
  expect((puts[0]!.body?.attachments as { path: string }[]).map((a) => a.path))
    .toEqual(["/shots/a.png", "/shots/b.csv"]);
  forgetDraftVersion("sess-tray");
});

test("restore can resurrect a spent draft — Send does not let it back in", async () => {
  // Bugbot 4028710588. `restoreTray` held autosave open across `addPaths` by a
  // counter with no idea which episode it was minted for. A seed still
  // restoring when Send fires used to land AFTER the DELETE, put the spent
  // files back in the tray via `addPaths`' own commit, and the very next
  // autosave write recreated the draft that Send just spent.
  const files = [{ path: "/shots/a.png", name: "a.png", kind: "image" as const }];
  const seen = storeWith({
    "sess-resurrect": {
      text: "the spent sentence",
      attachments: files,
      updated_at: 1,
      version: 3,
      form: {},
    },
  });
  let land: (() => void) | undefined;
  let tray: { pending: boolean; view: string; name: string; kind: string }[] = [];
  const c = mount({
    file: "/p/resurrect.py",
    sessionId: "sess-resurrect",
    attachments: () => tray as never,
    onRestoreAttachments: (paths: string[]) =>
      new Promise<() => void>((resolve) => {
        land = () => {
          // WHAT `addPaths` DOES ON ITS OWN, past its await: it commits the
          // paths into the tray whatever else has happened meanwhile, and
          // hands back THIS CALL'S OWN undo (Bugbot 4028927464) — here, the
          // only restore in flight, so undoing its own chips empties the tray
          // exactly the way the old blanket `discard()` did.
          const mine = paths.map((path) => ({
            pending: false,
            view: path,
            name: path.slice(path.lastIndexOf("/") + 1),
            kind: path.endsWith(".png") ? "image" : "file",
          }));
          tray = [...tray, ...mine];
          resolve(() => {
            tray = tray.filter((s) => !mine.includes(s));
          });
        };
      }),
  });
  await act(async () => {
    await tick();
  });
  // Seeded, and the restore is still open — the record's own files have not
  // landed in the tray yet.
  expect(c.box().props.value).toBe("the spent sentence");
  expect(tray).toEqual([]);
  // SEND, while the seed's restore is still in flight.
  expect(c.press("Enter")).toBe(true);
  expect(c.box().props.value).toBe("");
  // …and NOW `addPaths` lands, holding exactly the files Send just spent.
  await act(async () => {
    land!();
    await tick();
  });
  // Resurrected into the tray is the bug; the fix puts them right back out.
  expect(tray).toEqual([]);
  await act(async () => {
    await new Promise((r) => setTimeout(r, 700));
    await tick();
  });
  // The send's own DELETE, and nothing else: no PUT recreating the spent
  // draft with the resurrected files.
  expect(seen.filter((r) => r.method === "PUT")).toEqual([]);
  expect(seen.filter((r) => r.method === "DELETE")).toHaveLength(1);
  forgetDraftVersion("sess-resurrect");
});

test("a stale restore's abort takes back only its own chips, never a newer restore's", async () => {
  // Bugbot 4028927464. A stale `restoreTray` landing used to call
  // `discardAttachments` — the WHOLE tray, plus the epoch bump `addPaths`
  // reads. That is fine when it is the only restore anyone has started, but
  // a NEWER one (another session's seed, an adopted record) can already be
  // sitting in the same tray, its own files landed or still on the way, and
  // the blanket wipe took those too. The fix: a stale restore removes only
  // what IT added.
  const seen = storeWith({
    "sess-old": {
      text: "",
      attachments: [{ path: "/shots/a.png", name: "a.png", kind: "image" }],
      updated_at: 1,
      version: 3,
      form: {},
    },
    "sess-new": {
      text: "",
      attachments: [{ path: "/shots/b.csv", name: "b.csv", kind: "file" }],
      updated_at: 1,
      version: 3,
      form: {},
    },
  });
  let tray: { pending: boolean; view: string; name: string; kind: string }[] = [];
  const landers: (() => void)[] = [];
  const c = mount({
    file: "/p/overlap.py",
    sessionId: "sess-old",
    attachments: () => tray as never,
    onRestoreAttachments: (paths: string[]) =>
      new Promise<() => void>((resolve) => {
        // Each call is its OWN restore: it lands (appends its own chips) only
        // when THIS test tells it to, in whatever order the test picks.
        landers.push(() => {
          const mine = paths.map((path) => ({
            pending: false,
            view: path,
            name: path.slice(path.lastIndexOf("/") + 1),
            kind: path.endsWith(".png") ? "image" : "file",
          }));
          tray = [...tray, ...mine];
          resolve(() => {
            tray = tray.filter((s) => !mine.includes(s));
          });
        });
      }),
    // Still wired, exactly as the real host wires it (`attach.discard`) — RED
    // pre-fix: the old code reached for this on every stale landing, wiping
    // b.csv along with a.png. GREEN post-fix: this path calls `revert()`
    // instead and never touches this at all.
    onDiscardAttachments: () => {
      tray = [];
    },
  });
  await act(async () => {
    await tick();
  });
  // `sess-old`'s seed dispatched its restore and is holding — nothing in the
  // tray yet.
  expect(landers.length).toBe(1);
  expect(tray).toEqual([]);

  // ANOTHER SESSION'S SEED, while the first is still in flight — the key
  // changes under the box, which is exactly what makes the first's eventual
  // landing stale.
  c.rerender({ sessionId: "sess-new" });
  await act(async () => {
    await tick();
  });
  expect(landers.length).toBe(2);

  // THE NEWER RESTORE LANDS FIRST — its files are in the tray.
  await act(async () => {
    landers[1]!();
    await tick();
  });
  expect(tray.map((s) => s.view)).toEqual(["/shots/b.csv"]);

  // …AND ONLY THEN THE STALE ONE. Landing at all commits its own chip
  // (`addPaths`' own behavior, past its await) before this box ever sees it,
  // so the moment of truth is what the stale-abort branch does next.
  await act(async () => {
    landers[0]!();
    await tick();
  });
  // The newer restore's file is still here; only the stale one's is gone —
  // NOT an emptied tray.
  expect(tray.map((s) => s.view)).toEqual(["/shots/b.csv"]);

  // …and nothing here ever asked to persist an empty tray: a keystroke forces
  // the render that reads it, and the PUT the next flush produces still carries
  // b.csv.
  c.type("still here");
  await act(async () => {
    await new Promise((r) => setTimeout(r, 700));
    blurWindow("sess-new");
    await tick();
  });
  const puts = seen.filter((r) => r.method === "PUT");
  expect(puts.length).toBeGreaterThan(0);
  for (const put of puts) {
    const paths = (put.body?.attachments as { path: string }[] | undefined)?.map((a) => a.path);
    expect(paths).not.toEqual([]);
  }
  expect(puts[puts.length - 1]!.body?.attachments).toEqual([
    { path: "/shots/b.csv", name: "b.csv", kind: "file" },
  ]);
  forgetDraftVersion("sess-old");
  forgetDraftVersion("sess-new");
});

test("words typed before the session lands move onto the session's record", async () => {
  // Bugbot 4027549731. The first send mints the session and it arrives a render
  // later, so a follow-up typed in that gap sits in a box that has just changed
  // rules: the session-less half stands down (no `dirty`, no leave guard) and
  // the session half was never told, because `useAutosave` only speaks when the
  // VALUE changes and it was the KEY that changed.
  const seen = storeWith({});
  const c = mount({ file: "/p/flip.py", sessionId: "" });
  await act(async () => {
    await tick();
  });
  c.type("the follow-up I typed while it was starting");
  expect(seen.filter((r) => r.method !== "GET")).toEqual([]);
  c.rerender({ sessionId: "sess-flip" });
  // The flip STATES the words under the new key and starts no timer — same
  // deferral as every other road — so they go out on the next flush.
  await act(async () => {
    await new Promise((r) => setTimeout(r, 700));
    await tick();
  });
  expect(seen.filter((r) => r.method !== "GET")).toEqual([]);
  await act(async () => {
    blurWindow("sess-flip");
    await tick();
  });
  const puts = seen.filter((r) => r.method === "PUT");
  expect(puts).toHaveLength(1);
  expect(puts[0]!.url).toBe("/api/drafts/chat/sess-flip");
  expect(puts[0]!.body?.text).toBe("the follow-up I typed while it was starting");
  forgetDraftVersion("sess-flip");
});

test("a session's composer is NEVER asked about on the way out", async () => {
  // Nothing here is unsaved, so the question would be about words that are
  // already on the server — and the hop stays the synchronous call it has always
  // been (platform/lib/router.ts).
  installBody();
  storeWith({});
  const pushes = watchPushes();
  const { navigateUrl } = await import("@platform/lib/router");
  try {
    const c = mount({ file: "/p/leaving.py", sessionId: "sess-leave" });
    await act(async () => {
      await tick();
    });
    c.type("half a follow-up");
    act(() => {
      navigateUrl("/tasks");
    });
    expect(pushes.urls).toEqual(["/tasks"]);
    expect(c.root.findAll((n) => n.type === "button" && n.props.children === "Save as draft"))
      .toHaveLength(0);
  } finally {
    pushes.restore();
    delete doc.body;
    forgetDraftVersion("sess-leave");
  }
});

// ---- LEAVING ASKS NOTHING ANY MORE -----------------------------------------
//
// The "Unsent message" dialog is gone with the road that needed it: a
// session-less box now HOLDS a task draft and saves it on the way out, so there
// is nothing left to ask about (see the held-draft section at the end of this
// file). What survives here is the proof that no navigation off this composer
// is ever held up — plus the DOM props that proof needs.
//
// The body stub is `schedule-hop.render.test.tsx`'s pattern: react-dom's
// `createPortal` throws on a container that is not an element by `nodeType`,
// from inside the render, which unmounts the tree and takes the assertions
// with it. Installed per test and taken away again — `bun test` shares one
// `globalThis` across the whole run, and a standing `document.body` changes
// what other libraries decide to do.

const doc = globalThis.document as unknown as { body?: unknown };

function domNode() {
  return {
    focus() {},
    blur() {},
    setSelectionRange() {},
    scrollIntoView() {},
    addEventListener() {},
    removeEventListener() {},
    contains: () => false,
    closest: () => null,
    querySelector: () => null,
    querySelectorAll: () => [] as unknown[],
    style: {} as Record<string, string>,
    value: "",
    scrollHeight: 20,
    getBoundingClientRect: () => ({
      top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0,
    }),
  };
}

function installBody() {
  doc.body = { nodeType: 1, children: [] as unknown[], createNodeMock: domNode };
}

/** `history.pushState`, watched: this is the fact "the navigation happened",
 *  and the guard's whole job is to hold it up until the reader has answered. */
function watchPushes(): { urls: string[]; restore(): void } {
  const hist = globalThis.history as unknown as {
    pushState(state: unknown, title: string, url: string): void;
  };
  const real = hist.pushState;
  const urls: string[] = [];
  hist.pushState = (_state: unknown, _title: string, url: string) => {
    urls.push(url);
  };
  return { urls, restore: () => { hist.pushState = real; } };
}

test("a CLEAN composer is never asked about, and the hop stays synchronous", async () => {
  // The guard registers only while there is something to lose, which is what
  // keeps every other navigation in this app the same synchronous call it has
  // always been (platform/lib/router.ts).
  installBody();
  watchFetch();
  const pushes = watchPushes();
  const { navigateUrl } = await import("@platform/lib/router");
  try {
    const c = mount({ file: "/p/empty.py", sessionId: "" });
    act(() => {
      navigateUrl("/tasks");
    });
    // Pushed in the same tick, with no dialog anywhere.
    expect(pushes.urls).toEqual(["/tasks"]);
    expect(c.root.findAll((n) => n.type === "button" && n.props.children === "Save as draft"))
      .toHaveLength(0);
  } finally {
    pushes.restore();
    delete doc.body;
  }
});

// ---- the Schedule hop ------------------------------------------------------

test("Continue CREATES the draft, then leaves, and the box is empty behind it", async () => {
  const { SchedConfirm } = await import("./SchedConfirm");
  const seen = watchFetch();
  const hops: string[] = [];
  const discarded: number[] = [];
  const c = mount({
    file: "/p/hop.py",
    sessionId: "",
    onNavigate: (url: string) => hops.push(url),
    onDiscardAttachments: () => discarded.push(1),
  });
  c.type("make this a task");
  await act(async () => {
    c.root.findByType(SchedConfirm).props.onGo();
    await tick();
  });
  // THE HOP IS THE WRITER. Nothing saved these words before it — the composer
  // writes nothing — so Continue is what makes the record exist, and what it
  // makes is a TASK draft of its own rather than this folder's one chat record.
  const puts = seen.filter((r) => r.method === "PUT");
  expect(puts).toHaveLength(1);
  expect(puts[0]!.url.startsWith("/api/drafts/task/")).toBe(true);
  expect(puts[0]!.body?.title).toBe("make this a task");
  expect(puts[0]!.body?.target).toBe("/p/hop.py");
  // …then the card, on the draft it just minted, with the way back on it.
  expect(hops).toHaveLength(1);
  expect(hops[0]).not.toContain("new%3A");
  expect(hops[0]).toContain(
    "draft=" + encodeURIComponent(puts[0]!.url.slice("/api/drafts/task/".length)),
  );
  // …and the composer is clean: one copy, edited on the card from here on.
  expect(c.box().props.value).toBe("");
  expect(discarded).toHaveLength(1);
});


// ---- THE DRAFT A SESSION-LESS BOX HOLDS ------------------------------------
//
// Akshil, 2026-09-17: the landing composer IS where the folder's newest
// Upcoming draft lives. `ClaudeChat` picks that row off the listing and hands
// this box its key (`heldKey`, `draft:<id>`) and its stored form (`heldForm`),
// and the box mirrors what is typed into that one record — stated on every
// keystroke, WRITTEN only when the words could otherwise be lost: the window
// losing focus, the box being swapped to another draft, and the unmount.
//
// So every test below counts the wire at a moment, not after a delay: the
// 600 ms autosave is exactly what `defer` turns off.

/** A listing row's stored form, as `GET /api/tasks` carries it (`form:
 *  dict(record)` server-side, version included). */
function heldRow(
  title: string,
  description = "",
  version?: number,
): TaskDraftForm & { version?: number } {
  return {
    title,
    description,
    target: "/p/held.py",
    when: null,
    repeat: null,
    model: "",
    effort: "",
    permission: "",
    attachments: [],
    new_task_each_run: null,
    session_id: "",
    custom_rule: null,
    ...(version === undefined ? {} : { version }),
  };
}

/** THE WINDOW LOSING FOCUS. `drafts.listen` arms one `blur` listener for every
 *  key at once, the first time any syncer is made — so this suite records what
 *  gets armed on the shim's window and fires it. `bun test` shares one module
 *  registry across files, so another suite may have armed it first and had its
 *  listener swallowed by the shim's no-op; the fallback does by hand what that
 *  listener does for every key (`flushNow`), so the case under test is the same
 *  either way. */
function blurWindow(key: string) {
  const fns = winListeners.blur ?? [];
  if (fns.length) {
    for (const fn of fns) fn({ type: "blur" });
    return;
  }
  peekDraftSyncer(key)?.flushNow();
}

test("a held draft opens on the row's own words, and asks the server for nothing", async () => {
  const seen = watchFetch();
  const c = mount({
    file: "/p/held.py",
    sessionId: "",
    heldKey: "draft:d-seed",
    heldForm: heldRow("Ship the release", "and tell the team", 4),
  });
  await act(async () => {
    await tick();
  });
  // The two card fields, back as one block of prose (`joinDraft`) — the listing
  // row already carried the whole record, so there is no GET to make…
  expect(c.box().props.value).toBe("Ship the release\n\nand tell the team");
  // …and a box that merely opened has written nothing either.
  expect(seen).toEqual([]);
  forgetDraftVersion("draft:d-seed");
});

test("typing waits for a flush: 700 ms writes nothing, the window blurring writes once", async () => {
  const seen = watchFetch();
  const c = mount({ file: "/p/held.py", sessionId: "", heldKey: "draft:d-typing" });
  c.type("a task I am still writing");
  // Well past the 600 ms an autosave would have taken: `defer` means the
  // statement is recorded and no timer is started at all.
  await act(async () => {
    await new Promise((r) => setTimeout(r, 700));
    await tick();
  });
  expect(seen).toEqual([]);
  await act(async () => {
    blurWindow("draft:d-typing");
    await tick();
  });
  const puts = seen.filter((r) => r.method === "PUT");
  expect(puts).toHaveLength(1);
  expect(puts[0]!.url).toBe("/api/drafts/task/d-typing");
  expect(puts[0]!.body?.title).toBe("a task I am still writing");
  expect(c.box().props.value).toBe("a task I am still writing");
  forgetDraftVersion("draft:d-typing");
});

test("the unmount is a save: one PUT, under the key the box was holding", async () => {
  const seen = watchFetch();
  const c = mount({ file: "/p/held.py", sessionId: "", heldKey: "draft:d-leave" });
  c.type("words the pane is taking away");
  await act(async () => {
    c.unmount();
    await tick();
  });
  const puts = seen.filter((r) => r.method === "PUT");
  expect(puts).toHaveLength(1);
  expect(puts[0]!.url).toBe("/api/drafts/task/d-leave");
  expect(puts[0]!.body?.title).toBe("words the pane is taking away");
  forgetDraftVersion("draft:d-leave");
});

test("an emptied held draft is DELETED on the way out, never saved as a blank row", async () => {
  const seen = watchFetch();
  const c = mount({ file: "/p/held.py", sessionId: "", heldKey: "draft:d-empty" });
  c.type("something worth a row");
  await act(async () => {
    blurWindow("draft:d-empty");
    await tick();
  });
  // The write landed, so this client knows the record's version — which is the
  // whole condition on the delete: a key nobody has written has nothing to
  // remove, and asking would be this page guessing about the server.
  expect(draftVersion("draft:d-empty")).toBe(1);
  c.type("");
  await act(async () => {
    c.unmount();
    await tick();
  });
  const dels = seen.filter((r) => r.method === "DELETE");
  expect(dels).toHaveLength(1);
  expect(dels[0]!.url).toBe("/api/drafts/task/d-empty");
  forgetDraftVersion("draft:d-empty");
});

test("swapping to another draft row saves the one being put down and seeds the one picked up", async () => {
  const seen = watchFetch();
  const c = mount({ file: "/p/held.py", sessionId: "", heldKey: "draft:d-first" });
  c.type("the draft being put down");
  c.rerender({
    heldKey: "draft:d-second",
    heldForm: heldRow("the draft being picked up", "", 2),
  });
  await act(async () => {
    await tick();
  });
  // The key it LEFT is written, once, with the words that were in the box…
  const puts = seen.filter((r) => r.method === "PUT");
  expect(puts).toHaveLength(1);
  expect(puts[0]!.url).toBe("/api/drafts/task/d-first");
  expect(puts[0]!.body?.title).toBe("the draft being put down");
  // …and the box is now on the row it was handed.
  expect(c.box().props.value).toBe("the draft being picked up");
  forgetDraftVersion("draft:d-first");
  forgetDraftVersion("draft:d-second");
});

test("Send spends the held draft — the DELETE, on the key the box was holding", async () => {
  const seen = watchFetch();
  const c = mount({ file: "/p/held.py", sessionId: "", heldKey: "draft:d-sent" });
  c.type("send this one");
  expect(c.press("Enter")).toBe(true);
  await act(async () => {
    await tick();
  });
  expect(c.sent).toEqual([{ text: "send this one", model: DEFAULT_MODEL }]);
  const dels = seen.filter((r) => r.method === "DELETE");
  expect(dels).toHaveLength(1);
  expect(dels[0]!.url).toBe("/api/drafts/task/d-sent");
  expect(c.box().props.value).toBe("");
  // …and the unmount behind it says nothing more about a key that is spent.
  await act(async () => {
    c.unmount();
    await tick();
  });
  expect(seen.filter((r) => r.method !== "GET")).toHaveLength(1);
  forgetDraftVersion("draft:d-sent");
});


// ---- the spent-box latch (`freshBox`) --------------------------------------
//
// The rule a render older than a clear is measured against. Read here rather
// than through a render because the failure it guards is a TIMING one — the
// renders arrive in whatever order React commits them — and the question the
// rule answers is not about timing at all.

test("nothing spent means every half is the reader's", () => {
  expect(freshBox(null, "", 0)).toEqual({ text: true, tray: true });
  expect(freshBox(null, "hi", 2)).toEqual({ text: true, tray: true });
});

test("a half still showing what was spent is stale, and only that half", () => {
  const spent = { text: "gone", files: 2 };
  expect(freshBox(spent, "gone", 2)).toEqual({ text: false, tray: false });
  expect(freshBox(spent, "typed", 2)).toEqual({ text: true, tray: false });
  expect(freshBox(spent, "gone", 3)).toEqual({ text: false, tray: true });
});

test("a half that was EMPTY when the box was spent is fresh at once", () => {
  // Bugbot 4036599549: a picture-only send spends no words, so "differs from
  // what was spent" could never become true for the text half — the latch stuck
  // and the unmount save silently dropped whatever was attached next.
  expect(freshBox({ text: "", files: 2 }, "", 2)).toEqual({ text: true, tray: false });
  expect(freshBox({ text: "", files: 2 }, "", 3)).toEqual({ text: true, tray: true });
  // …and the same on the other side: a clear of a box holding no files must not
  // latch the tray shut against the very next attachment.
  expect(freshBox({ text: "gone", files: 0 }, "gone", 0)).toEqual({
    text: false, tray: true,
  });
  expect(freshBox({ text: "", files: 0 }, "", 0)).toEqual({ text: true, tray: true });
});

// ---- the pills while their value is still being read ------------------------
//
// "It takes some time to load in these model and effort … when I come to the
// page after 2-3 seconds it flips, same when I reload" (Akshil, 2026-09-19).
// The two values behind these pills come from reads that land at different
// speeds, and the pills used to paint the constant default meanwhile. The fix is
// not a faster flip, it is NO FIRST VALUE: `controls.ready` false draws a wash
// of the same size, and a pill that has shown nothing cannot flip to something
// else.

/** The model pill's <select> and the wrapper that carries the wash. */
function modelPill(c: ReturnType<typeof mount>) {
  return {
    sel: c.root.findByProps({ className: "c-pill c-model-sel" }),
    wrap: c.root.findAllByType("span").find(
      (n) => typeof n.props.className === "string"
        && n.props.className.startsWith("c-pillwrap")
        && n.findAllByProps({ className: "c-pill c-model-sel" }).length > 0,
    )!,
  };
}
function effortPill(c: ReturnType<typeof mount>) {
  return c.root.findByProps({ className: "c-pill c-effort-sel" });
}

test("an unresolved pill is a wash, not a value (Akshil, 2026-09-19)", () => {
  const c = mount({ controls: { ...controls, ready: false } });
  const { sel, wrap } = modelPill(c);
  // The wash is on, so nothing readable is painted…
  expect(wrap.props.className).toBe("c-pillwrap is-loading");
  // …and the pill refuses a pick: there is nothing to pick yet, and a choice
  // made against a value nobody has seen is the same wrong answer the flip was.
  expect(sel.props.disabled).toBe(true);
  expect(sel.props["aria-busy"]).toBe(true);
  expect(effortPill(c).props.disabled).toBe(true);
  // THE BOX DOES NOT MOVE: the <select> keeps a selected option, so `fitSelect`
  // measures the same text it will measure when the wash lifts.
  expect(sel.props.value).toBe(DEFAULT_MODEL);
});

test("the value shows ONCE the reads have landed, and never before", () => {
  const c = mount({ controls: { ...controls, ready: false } });
  expect(modelPill(c).wrap.props.className).toBe("c-pillwrap is-loading");
  // The host resolves: a different model than the one the box was sized on, and
  // it is the FIRST one this pill has ever shown.
  c.rerender({ controls: { ...controls, model: "haiku", ready: true } });
  const { sel, wrap } = modelPill(c);
  expect(wrap.props.className).toBe("c-pillwrap");
  expect(sel.props.disabled).toBe(false);
  expect(sel.props["aria-busy"]).toBeUndefined();
  expect(sel.props.value).toBe("haiku");
});

test("a host that states no `ready` at all is stating a settled pair", () => {
  // Backward compatible by construction — the cards wall and the tests hand a
  // pair they already know, and the permission pill has no read behind it.
  const c = mount();
  expect(modelPill(c).wrap.props.className).toBe("c-pillwrap");
  expect(modelPill(c).sel.props.disabled).toBe(false);
});

test("the idle line opens under the pointer and folds when it leaves; touch is ignored (Akshil, 2026-09-21)", () => {
  const m = mount();
  const form = () => m.root.findByType("form");
  const cls = () => String(form().props.className);
  expect(cls()).toContain("is-idle");
  act(() => form().props.onPointerEnter({ pointerType: "mouse" }));
  expect(cls()).not.toContain("is-idle");
  const doc = { querySelector: () => null };
  const leave = (over: Record<string, unknown>) =>
    act(() =>
      form().props.onPointerLeave({
        pointerType: "mouse",
        relatedTarget: null,
        currentTarget: { ownerDocument: doc },
        ...over,
      }),
    );
  leave({});
  expect(cls()).toContain("is-idle");
  // A pill's menu is portaled to the body: the pointer moving into it, or into
  // the gap under the pill while it is up, is not a leave (Bugbot, #1298).
  act(() => form().props.onPointerEnter({ pointerType: "mouse" }));
  leave({ relatedTarget: { closest: (sel: string) => (sel.includes("popover") ? {} : null) } });
  expect(cls()).not.toContain("is-idle");
  leave({ currentTarget: { ownerDocument: { querySelector: () => ({}) } } });
  expect(cls()).not.toContain("is-idle");
  leave({});
  expect(cls()).toContain("is-idle");
  // A finger has no hover: a tap's pointerenter must not stick the card open.
  act(() => form().props.onPointerEnter({ pointerType: "touch" }));
  expect(cls()).toContain("is-idle");
  // The landing card never folds, hovered or not.
  const home = mount({ variant: "home" });
  expect(String(home.root.findByType("form").props.className)).not.toContain("is-idle");
});

test("focus alone opens the idle line — no click or key needed — and focus on Send does not (Akshil, 2026-09-21)", () => {
  const m = mount();
  const form = () => m.root.findByType("form");
  const cls = () => String(form().props.className);
  const textarea = { closest: () => null };
  const send = { closest: (sel: string) => (sel === ".c-send" ? {} : null) };
  expect(cls()).toContain("is-idle");
  // The focus a pressed Send button gets is not the reader reaching for the card.
  act(() => form().props.onFocus({ target: send }));
  expect(cls()).toContain("is-idle");
  // The caret landing in the box — by click, Tab, or `autoFocus` on arrival — is.
  act(() => form().props.onFocus({ target: textarea }));
  expect(cls()).not.toContain("is-idle");
  // Focus leaving the form folds it back.
  act(() =>
    form().props.onBlur({
      relatedTarget: null,
      currentTarget: { contains: () => false, ownerDocument: { querySelector: () => null } },
    }),
  );
  expect(cls()).toContain("is-idle");
});
