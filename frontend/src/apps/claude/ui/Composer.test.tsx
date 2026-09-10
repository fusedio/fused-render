import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const { ComposerCard, CHAT_PLACEHOLDER, HOME_PLACEHOLDER } =
  await import("./Composer");
const { DEFAULT_EFFORT, DEFAULT_MODEL, DEFAULT_PERMISSION } =
  await import("./composer-defaults");

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
  act(() => {
    renderer = create(
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
      />,
    );
  });
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

test("send is NEVER disabled — nothing to send, blocked, attaching or busy (T:4187)", () => {
  // The DELETED test's subject, with T's expectation. `.c-send` carries no
  // `disabled` at all: not for an empty box, not for a pending scheduled
  // message, not through either transient window. T has no `.send:disabled`
  // rule (T:2956-2981), no attribute in the markup (T:4187, T:4246) and no
  // script line that sets one — `applyComposerBlockState` reaches the box
  // (T:17218) and the Schedule pill (T:17238) and stops there. T has no
  // `canSend` either: that name is T:7720's annotation send gate.
  const send = (c: ReturnType<typeof mount>) =>
    c.root.findAllByType("button").find((b) => b.props.className === "c-send")!;
  for (const props of [
    {},
    { blocked: true },
    { hasAttachments: true, attachPending: true },
    { sendBusy: true, hasAttachments: true },
    { status: "running" as const, blocked: true },
  ]) {
    expect(send(mount(props)).props.disabled).toBeUndefined();
  }
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

test("Send is NEVER disabled by the block (T:17193-17195, T:4187)", () => {
  // The send door already refuses a blocked composer at `submit`'s first guard,
  // so the dim bought nothing — and cost the load-bearing half: `disabled` also
  // kills the STOP this button becomes mid-run, and a reader who cannot stop a
  // turn has no way out of it.
  const send = (c: ReturnType<typeof mount>) =>
    c.root.findAllByType("button").find((b) => b.props.className === "c-send")!;
  expect(send(mount({ blocked: true, hasAttachments: true })).props.disabled).toBeUndefined();
  // And mid-run it is the Stop, live.
  const running = mount({ blocked: true, status: "running" });
  expect(send(running).props.disabled).toBeUndefined();
  expect(send(running).props["aria-label"]).toBe("Stop");
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
