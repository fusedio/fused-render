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
  expect(send().props.disabled).toBe(true);
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
  expect(c.root.findByProps({ className: "c-send" }).props.disabled).toBe(false);
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

test("send is disabled with nothing to send, enabled once there is", () => {
  const c = mount();
  const send = () =>
    c.root.findAllByType("button").find((b) => b.props.className === "c-send")!;
  expect(send().props.disabled).toBe(true);
  c.type("hi");
  expect(send().props.disabled).toBe(false);
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
