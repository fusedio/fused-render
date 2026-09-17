import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const { ComposerCard, BLOCKED_SEND_TITLE, CHAT_PLACEHOLDER, HOME_PLACEHOLDER } =
  await import("./Composer");
const { DEFAULT_EFFORT, DEFAULT_MODEL, DEFAULT_PERMISSION } =
  await import("./composer-defaults");
const { forgetDraftVersion, resetDraftSyncers } = await import("@platform/lib/drafts");

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
    mods: { shiftKey?: boolean; metaKey?: boolean } = {},
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
  // …and every one of those refusals is still MADE, in `submit`: an empty box
  // with nothing typed, and a worded send through either door that is shut.
  const empty = mount();
  empty.submitForm();
  expect(empty.sent).toEqual([]);
  for (const props of [{ blocked: true }, { sendBusy: true }, { hasAttachments: true, attachPending: true }]) {
    const c = mount(props);
    c.type("try it");
    c.submitForm();
    expect(c.sent).toEqual([]);
    expect(c.followups).toEqual([]);
  }
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

test("but a capture in flight DOES hold the door — in the handler", () => {
  // `sendBusy` is not "nothing to send": it is the shutter window, which can
  // run to seconds on a large pane, and T had no equivalent of it because it
  // had no such window. A transient refusal with a cause — and, like every
  // other refusal here, one the SUBMIT makes rather than the attribute.
  const c = mount({ sendBusy: true, hasAttachments: true });
  const send = c.root.findAllByType("button").find((b) => b.props.className === "c-send")!;
  expect(send.props.disabled).toBeUndefined();
  // …and the `title` is what says why, since the attribute no longer can
  // (Bugbot, PR #1074): this window can run to seconds on a large pane.
  expect(send.props.title).toBe("Taking the picture…");
  c.type("with this shot");
  c.press("Enter");
  expect(c.sent).toEqual([]);
  c.submitForm();
  expect(c.sent).toEqual([]);
  // The words are kept for the send that follows the shutter.
  expect(c.box().props.value).toBe("with this shot");
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

test("the send window's latch refuses the second submit and KEEPS its words", () => {
  // The parent takes the latch inside `onSend`, in the very tick this call is
  // made — which is the race: a second Enter arriving before React has
  // re-rendered used to start a second send window.
  const busyRef = { current: false };
  const sent: string[] = [];
  const c = mount({
    busyRef,
    onSend: (text: string) => {
      busyRef.current = true;
      sent.push(text);
    },
  });
  c.type("ship it");
  c.press("Enter");
  c.type("and again");
  c.press("Enter");
  expect(sent).toEqual(["ship it"]);
  // Refused BEFORE the box was cleared: the keystroke cost the user nothing.
  expect(c.box().props.value).toBe("and again");
});

test("a latched composer REFUSES the send — but never disarms Stop", () => {
  const send = (c: ReturnType<typeof mount>) =>
    c.root.findAllByType("button").find((b) => b.props.className === "c-send")!;
  const latched = mount({ sendBusy: true });
  latched.type("hi");
  // No attribute (T:4187) — the latch is `submit`'s, and the words are kept.
  expect(send(latched).props.disabled).toBeUndefined();
  latched.submitForm();
  expect(latched.sent).toEqual([]);
  expect(latched.box().props.value).toBe("hi");
  // A live run's button is the only way to stop it (T:17909-17914): a latch on
  // the way in must not take that away.
  const live = mount({ sendBusy: true, status: "running" });
  expect(send(live).props["aria-label"]).toBe("Stop");
  expect(send(live).props.disabled).toBeUndefined();
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
// I'm already there. If I do an operation that could lose the text, pop up a
// warning: save as draft or discard. After that, the composer is cleared."
//
// So the whole of this section is about REQUESTS THAT DO NOT HAPPEN, plus the
// four gestures that do make one: Save as draft, pagehide, and the Schedule
// hop's Continue. Every test counts the wire.

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
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
      keepalive: !!init?.keepalive,
    });
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

test("a session's typing AUTOSAVES, on the syncer's own ordered PUT", async () => {
  const seen = storeWith({});
  const c = mount({ file: "/p/typed.py", sessionId: "sess-typing" });
  await act(async () => {
    await tick();
  });
  c.type("a follow-up in progress");
  await act(async () => {
    await new Promise((r) => setTimeout(r, 700));
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
  // persist the wipe before the chips arrived.
  c.type("two files and a sentence, plus one more");
  await act(async () => {
    await new Promise((r) => setTimeout(r, 700));
    await tick();
  });
  expect(seen.filter((r) => r.method !== "GET")).toEqual([]);
  // The chips land; the next keystroke goes out, and it carries both files.
  await act(async () => {
    land!();
    await tick();
  });
  c.type("two files and a sentence, plus two more");
  await act(async () => {
    await new Promise((r) => setTimeout(r, 700));
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
  // the render that reads it, and the PUT it produces still carries b.csv.
  c.type("still here");
  await act(async () => {
    await new Promise((r) => setTimeout(r, 700));
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
  await act(async () => {
    await new Promise((r) => setTimeout(r, 700));
    await tick();
  });
  const puts = seen.filter((r) => r.method === "PUT");
  expect(puts).toHaveLength(1);
  expect(puts[0]!.url).toBe("/api/drafts/chat/sess-flip");
  expect(puts[0]!.body?.text).toBe("the follow-up I typed while it was starting");
  forgetDraftVersion("sess-flip");
});

test("a session's composer is left alone on the way out", async () => {
  // Nothing here is unsaved, so there is nothing to save on the way out and
  // nothing to say about it — and the hop stays the synchronous call it has
  // always been (platform/lib/router.ts).
  storeWith({});
  const pushes = watchPushes();
  const { navigateUrl } = await import("@platform/lib/router");
  const { _resetNotificationsForTest, getPopupNotification } =
    await import("@platform/lib/notifications");
  _resetNotificationsForTest();
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
    // No toast: the box's own autosave is the writer on this road, and it has
    // been writing all along.
    expect(getPopupNotification()).toBeNull();
  } finally {
    pushes.restore();
    _resetNotificationsForTest();
    forgetDraftVersion("sess-leave");
  }
});

// ---- leaving a never-sent chat, which SAVES ---------------------------------
//
// The three-button "Unsent message" dialog is gone (Akshil, 2026-09-17). Every
// door out of a chat that has never been sent now writes the draft, goes, and
// says so on a toast with one press on it.

/** `history.pushState`, watched: this is the fact "the navigation happened",
 *  and the whole point of the change is that it happens straight away. */
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

/** ONE READY CHIP holding a chat tempdir path — the shape every unload and
 *  every leave save has to decide what to do about. */
const tempShot = {
  id: "att-1",
  kind: "image" as const,
  view: "/tmp/fused-chat/shot.png",
  name: "shot.png",
  pending: false,
};

test("an in-app navigation with words in the box SAVES them and goes", async () => {
  const seen = watchFetch();
  const pushes = watchPushes();
  const { navigateUrl } = await import("@platform/lib/router");
  const { _resetNotificationsForTest, getPopupNotification } =
    await import("@platform/lib/notifications");
  _resetNotificationsForTest();
  try {
    const c = mount({ file: "/p/leaving.py", sessionId: "" });
    c.type("a sentence nobody sent");
    // The hop is made the way every row, breadcrumb and notification makes it.
    await act(async () => {
      navigateUrl("/tasks");
      await tick();
    });
    // THE NAVIGATION HAPPENED. Nothing was held up and nothing was asked.
    expect(pushes.urls).toEqual(["/tasks"]);
    // ONE PUT, and it mints a TASK draft rather than writing this folder's one
    // chat record (Akshil, 2026-09-16). A chat that has never been sent has no
    // unsent message to keep — it has an Upcoming task nobody finished writing,
    // and there can be as many of those as the reader writes.
    const puts = seen.filter((r) => r.method === "PUT");
    expect(puts).toHaveLength(1);
    expect(puts[0]!.url.startsWith("/api/drafts/task/")).toBe(true);
    expect(puts[0]!.url).not.toContain("new%3A");
    expect(puts[0]!.body?.title).toBe("a sentence nobody sent");
    expect(puts[0]!.body?.target).toBe("/p/leaving.py");
    // …THROUGH THE KEY'S ONE WRITER, which is what `client` + `seq` say (Bugbot
    // review of caef75eb1, HIGH-1). A bare `saveChatDraft` here carried neither,
    // so a straggling write from this page could not be ordered against the
    // card's edits and simply landed on top of them.
    expect(typeof (puts[0]!.body as { client?: unknown }).client).toBe("string");
    expect((puts[0]!.body as { seq?: unknown }).seq).toBe(1);
    // …the box is clean: the record is the copy now.
    expect(c.box().props.value).toBe("");
    // …and the only thing the reader is asked is whether they meant it.
    const toast = getPopupNotification();
    expect(toast?.title).toBe("Saved as draft");
    expect(toast?.action?.label).toBe("Undo");
    // It pops and is forgotten — an Undo still sitting in the Notifications
    // panel tomorrow would delete a draft that has been written since.
    const { getRetainedNotifications } = await import("@platform/lib/notifications");
    expect(getRetainedNotifications()).toEqual([]);
  } finally {
    pushes.restore();
    _resetNotificationsForTest();
  }
});

test("UNDO deletes the draft and takes its row off Upcoming", async () => {
  const seen = watchFetch();
  const pushes = watchPushes();
  const { navigateUrl } = await import("@platform/lib/router");
  const { _resetNotificationsForTest, getPopupNotification } =
    await import("@platform/lib/notifications");
  const { onGone } = await import("@shell/tasksPulse");
  _resetNotificationsForTest();
  const dropped: string[][] = [];
  const offGone = onGone((keys) => dropped.push(keys));
  try {
    const c = mount({ file: "/p/undone.py", sessionId: "" });
    c.type("said by mistake");
    await act(async () => {
      navigateUrl("/tasks");
      await tick();
    });
    const saved = seen.filter((r) => r.method === "PUT");
    expect(saved).toHaveLength(1);

    await act(async () => {
      getPopupNotification()!.action!.onClick();
      await tick();
    });
    // ONE DELETE, on the record that was just written…
    const dels = seen.filter((r) => r.method === "DELETE");
    expect(dels).toHaveLength(1);
    expect(dels[0]!.url).toBe(saved[0]!.url);
    // …and the row left the listing under the pointer, before the server was
    // asked — the same optimistic drop the List's trash makes.
    expect(dropped).toEqual([[saved[0]!.url.slice("/api/drafts/task/".length)]
      .map((id) => "draft:" + id)]);
    expect(c.box().props.value).toBe("");
  } finally {
    offGone();
    pushes.restore();
    _resetNotificationsForTest();
  }
});

test("a never-sent chat saves a NEW draft every time, never over the last one", async () => {
  // THE BUG, in one test (Akshil, 2026-09-16): type, leave, save; come back,
  // type something else, leave, save — and the second save landed on the first.
  // It had to: the key was `new:<file>`, ONE record per folder, and the save
  // stated the whole of it. Each save now mints its own `draft:<id>`, so the
  // reader ends up with the two Upcoming rows they wrote.
  const seen = watchFetch();
  const pushes = watchPushes();
  const { navigateUrl } = await import("@platform/lib/router");
  try {
    const c = mount({ file: "/p/twice.py", sessionId: "" });
    c.type("the first thing");
    await act(async () => {
      navigateUrl("/tasks");
      await tick();
    });
    // …the box is clean again, which is what starts the next draft.
    expect(c.box().props.value).toBe("");

    c.type("the second thing");
    await act(async () => {
      navigateUrl("/tasks");
      await tick();
    });

    expect(seen).toHaveLength(2);
    // TWO RECORDS, both task drafts, both under ids of their own…
    expect(seen.map((r) => r.body?.title))
      .toEqual(["the first thing", "the second thing"]);
    for (const r of seen) {
      expect(r.method).toBe("PUT");
      expect(r.url.startsWith("/api/drafts/task/")).toBe(true);
      expect(r.body?.target).toBe("/p/twice.py");
    }
    expect(seen[0]!.url).not.toBe(seen[1]!.url);
    // …and `new:<file>` was never written at all.
    expect(seen.some((r) => r.url.includes("new%3A"))).toBe(false);
  } finally {
    pushes.restore();
  }
});

test("a CLEAN composer writes nothing, and the hop stays synchronous", async () => {
  // The guard registers only while there is something to lose, which is what
  // keeps every other navigation in this app the same synchronous call it has
  // always been (platform/lib/router.ts).
  const seen = watchFetch();
  const pushes = watchPushes();
  const { navigateUrl } = await import("@platform/lib/router");
  const { _resetNotificationsForTest, getPopupNotification } =
    await import("@platform/lib/notifications");
  _resetNotificationsForTest();
  try {
    mount({ file: "/p/empty.py", sessionId: "" });
    act(() => {
      navigateUrl("/tasks");
    });
    // Pushed in the same tick, with nothing written and nothing said.
    expect(pushes.urls).toEqual(["/tasks"]);
    expect(seen).toEqual([]);
    expect(getPopupNotification()).toBeNull();
  } finally {
    pushes.restore();
    _resetNotificationsForTest();
  }
});

test("a reload is not asked either: no prompt, and the words are SAVED", async () => {
  // `beforeunload` used to raise the browser's native "leave site?" prompt while
  // the box was dirty. It is gone (Akshil, 2026-09-17): an in-app hop no longer
  // asks, and a reload that stopped to ask would be the same design saying two
  // different things about the same words. `pagehide` fires when the page is
  // actually going and writes them with `keepalive`.
  const winListeners: Record<string, ((ev: unknown) => void)[]> = {};
  const win = window as unknown as {
    addEventListener(t: string, fn: (ev: unknown) => void): void;
    removeEventListener(t: string, fn: (ev: unknown) => void): void;
  };
  const real = { add: win.addEventListener, remove: win.removeEventListener };
  win.addEventListener = (t, fn) => {
    (winListeners[t] ||= []).push(fn);
  };
  win.removeEventListener = (t, fn) => {
    winListeners[t] = (winListeners[t] || []).filter((f) => f !== fn);
  };
  const seen = watchFetch();
  try {
    const c = mount({ file: "/p/reloaded.py", sessionId: "" });
    c.type("the last sentence");
    // NOTHING IS ASKED, dirty box or not.
    expect(winListeners.beforeunload || []).toHaveLength(0);
    // …and the page actually goes.
    await act(async () => {
      for (const fn of winListeners.pagehide || []) fn({ type: "pagehide" });
      await tick();
    });
    expect(seen).toHaveLength(1);
    expect(seen[0]!.method).toBe("PUT");
    expect(seen[0]!.keepalive).toBe(true);
    // The same record leaving would have made — a door slammed on a never-sent
    // chat leaves the Upcoming row an in-app hop leaves.
    expect(seen[0]!.url.startsWith("/api/drafts/task/")).toBe(true);
    expect(seen[0]!.body?.title).toBe("the last sentence");
    // …AND IT GOES THROUGH THE SYNCER LIKE EVERY OTHER WRITE ON THIS KEY.
    expect(typeof (seen[0]!.body as { client?: unknown }).client).toBe("string");
  } finally {
    win.addEventListener = real.add;
    win.removeEventListener = real.remove;
  }
});

test("a door-slam saves the WORDS and drops the tray, on purpose", async () => {
  // Bugbot review of caef75eb1, HIGH-2. A chat attachment's path is a tempdir on
  // a 12 h TTL and `POST /api/schedule` refuses anything outside the task-shots
  // dir, so writing those raw paths onto the record makes a draft the card
  // cannot schedule and that 404s tomorrow. The copy that fixes it is a round
  // trip per file, which `pagehide` has no room for — so the words go and the
  // files do not.
  const winListeners: Record<string, ((ev: unknown) => void)[]> = {};
  const win = window as unknown as {
    addEventListener(t: string, fn: (ev: unknown) => void): void;
    removeEventListener(t: string, fn: (ev: unknown) => void): void;
  };
  const real = { add: win.addEventListener, remove: win.removeEventListener };
  win.addEventListener = (t, fn) => {
    (winListeners[t] ||= []).push(fn);
  };
  win.removeEventListener = (t, fn) => {
    winListeners[t] = (winListeners[t] || []).filter((f) => f !== fn);
  };
  const seen = watchFetch();
  try {
    const c = mount({ file: "/p/slammed.py", sessionId: "", attachments: () => [tempShot] });
    c.type("words and a picture");
    await act(async () => {
      for (const fn of winListeners.pagehide || []) fn({ type: "pagehide" });
      await tick();
    });
    expect(seen).toHaveLength(1);
    expect(seen[0]!.body?.title).toBe("words and a picture");
    expect(seen[0]!.body?.attachments).toEqual([]);
  } finally {
    win.addEventListener = real.add;
    win.removeEventListener = real.remove;
  }
});

test("a file that will not copy costs the FILE, never the words", async () => {
  // The reverse of Bugbot 4027244608's fail-closed, and it is the dialog going
  // that reverses it (Akshil, 2026-09-17). Refusing to write was right while
  // there was a dialog to refuse into — the reader was still standing in front
  // of it. Nothing is standing in front of this: the hop has already happened,
  // so "nothing was saved" would mean the words are gone.
  const seen: Req[] = [];
  (globalThis as { fetch: unknown }).fetch = (
    url: string,
    init?: RequestInit & { keepalive?: boolean },
  ) => {
    seen.push({
      url: String(url),
      method: init?.method ?? "GET",
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
      keepalive: !!init?.keepalive,
    });
    // The copy's first step is reading the bytes back, and it is what fails —
    // the draft write itself is answered normally.
    if (!String(url).startsWith("/api/drafts")) {
      return Promise.resolve(new Response("nope", { status: 500 }));
    }
    return Promise.resolve(
      new Response(JSON.stringify({ ok: true, task: {} }), { status: 200 }),
    );
  };
  const pushes = watchPushes();
  const { navigateUrl } = await import("@platform/lib/router");
  const { _resetNotificationsForTest, getPopupNotification } =
    await import("@platform/lib/notifications");
  _resetNotificationsForTest();
  try {
    const c = mount({
      file: "/p/half.py",
      sessionId: "",
      attachments: () => [tempShot],
    });
    c.type("with a picture");
    await act(async () => {
      navigateUrl("/tasks");
      await tick();
    });
    // THE WORDS WENT, WITH NO FILES ON THEM…
    const puts = seen.filter((r) => r.method === "PUT");
    expect(puts).toHaveLength(1);
    expect(puts[0]!.body?.title).toBe("with a picture");
    expect(puts[0]!.body?.attachments).toEqual([]);
    // …the navigation happened anyway…
    expect(pushes.urls).toEqual(["/tasks"]);
    expect(c.box().props.value).toBe("");
    // …and the toast names what did not make it, so the reader can put the
    // picture back on the record rather than discovering the gap later.
    const toast = getPopupNotification();
    expect(toast?.title).toBe("Saved as draft");
    expect(toast?.detail).toBe("1 file could not be attached");
  } finally {
    pushes.restore();
    _resetNotificationsForTest();
  }
});

test("a pane taken away mid-sentence still writes the words", async () => {
  // Every door this file knows about saves first. A host that closes the pane
  // from its own chrome does not go through one, and an unmount is too late for
  // a round trip — so the words are written and the tray is dropped, which is
  // the same trade `pagehide` makes.
  const seen = watchFetch();
  const pushes = watchPushes();
  const { navigateUrl } = await import("@platform/lib/router");
  try {
    const c = mount({ file: "/p/yanked.py", sessionId: "" });
    c.type("mid-sentence");
    await act(async () => {
      c.unmount();
      await tick();
    });
    expect(seen).toHaveLength(1);
    expect(seen[0]!.method).toBe("PUT");
    expect(seen[0]!.body?.title).toBe("mid-sentence");
    // …and the guard went with the composer, so the app navigates synchronously
    // again.
    act(() => {
      navigateUrl("/tasks");
    });
    expect(pushes.urls).toEqual(["/tasks"]);
  } finally {
    pushes.restore();
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
