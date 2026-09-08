// The seat's four faces and the strings on them. Verbatim matters here: the
// aria-label is what a live recording announced as "Done" got wrong (Bugbot,
// PR #664), and the inert states are what a click during a settle used to
// disarm the mode through (Bugbot, PR #665).
//
// react-test-renderer, the cards.test.tsx pattern: no DOM, so the tests read
// the rendered props and drive `onClick` directly.
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

import { RecControls, commentSeatName } from "./RecControls";
import type { RecSnapshot, RecState } from "./rec";

const mounted: Array<ReturnType<typeof create>> = [];
function mount(el: React.ReactElement): ReturnType<typeof create> {
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(el);
  });
  mounted.push(r);
  return r;
}
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

type Json = ReactTestRendererJSON;
type Props = Record<string, unknown>;

function buttons(node: Json | Json[] | null): Props[] {
  const out: Props[] = [];
  const walk = (n: Json | string | null) => {
    if (!n || typeof n === "string") return;
    if (n.type === "button") out.push(n.props as Props);
    for (const kid of n.children || []) walk(kid as Json | string);
  };
  for (const n of Array.isArray(node) ? node : [node]) walk(n);
  return out;
}

function text(node: Json | Json[] | null): string {
  const parts: string[] = [];
  const walk = (n: Json | string | null) => {
    if (!n) return;
    if (typeof n === "string") {
      parts.push(n);
      return;
    }
    for (const kid of n.children || []) walk(kid as Json | string);
  };
  for (const n of Array.isArray(node) ? node : [node]) walk(n);
  return parts.join("|");
}

const snap = (state: RecState, over: Partial<RecSnapshot> = {}): RecSnapshot => ({
  state,
  status: "",
  marks: 0,
  seconds: 0,
  busy: state === "stopping" || state === "transcribing" || state === "discarding",
  ...over,
});

function draw(rec: RecSnapshot, props: Partial<React.ComponentProps<typeof RecControls>> = {}) {
  const fired: string[] = [];
  const r = mount(
    <RecControls
      rec={rec}
      onBegin={() => fired.push("begin")}
      onEnd={() => fired.push("end")}
      onDiscard={() => fired.push("discard")}
      {...props}
    />,
  );
  const json = r.toJSON() as Json | Json[] | null;
  return { fired, seats: buttons(json), words: text(json) };
}

test("at rest: the mic, the resting word, and the resting name (T:3995, 8422)", () => {
  const { seats, words } = draw(snap("off"));
  expect(seats).toHaveLength(1);
  expect(seats[0]["aria-label"]).toBe("Annotate with a spoken walkthrough");
  expect(seats[0].title).toBe(
    "Annotate with a spoken walkthrough — talk while you click, and each click becomes a note",
  );
  expect(seats[0]["aria-pressed"]).toBe("false");
  expect(seats[0].disabled).toBe(false);
  expect(words).toBe("Annotate");
});

test("recording: ■ plus the clock, named for the STOP, with the trash beside it", () => {
  const { seats, words, fired } = draw(snap("recording", { status: "0:12 · 3", marks: 3 }));
  expect(seats).toHaveLength(2);
  expect(seats[0]["aria-label"]).toBe("Stop the recording");
  expect(seats[0].title).toBe("Recording — click to stop · Esc also stops it");
  expect(seats[0]["aria-pressed"]).toBe("true");
  expect(seats[0].className).toContain("on");
  expect(words).toBe("0:12 · 3");
  expect(seats[1]["aria-label"]).toBe("Discard the recording");
  expect(seats[1].title).toBe("Discard the recording — nothing is transcribed or sent");
  (seats[0].onClick as () => void)();
  (seats[1].onClick as () => void)();
  expect(fired).toEqual(["end", "discard"]);
});

// `disabled` is the assertion, not an unfired handler: react-test-renderer
// hands back the prop and will happily call it, where a browser will not — so
// the inert tests below check the ATTRIBUTES that make it inert (the `aria`
// mirror included, T:6826), and `ann/rec.ts`'s own guards are what refuse a
// call that gets through anyway.
test("the start request's own width is inert: a second click cannot open two recordings (T:7868)", () => {
  const { seats } = draw(snap("starting"));
  expect(seats[0].disabled).toBe(true);
  expect(seats[0]["aria-disabled"]).toBe("true");
});

test("a settle is a STATUS, never an enabled button (Bugbot PR #665, T:8167)", () => {
  for (const [state, label, status] of [
    ["stopping", "Stopping the recording", "Stopping…"],
    ["transcribing", "Transcribing the walkthrough", "Transcribing…"],
    ["discarding", "Discarding the recording", "Discarding…"],
  ] as Array<[RecState, string, string]>) {
    const { seats, words } = draw(snap(state, { status }));
    expect(seats).toHaveLength(1); // the trash is gone with the clicks (T:6233)
    expect(seats[0].disabled).toBe(true);
    expect(seats[0]["aria-disabled"]).toBe("true");
    expect(seats[0]["aria-label"]).toBe(label);
    expect(words).toBe(status); // the resting word yields the space (T:312)
  }
});

test("a typed comment mode makes the mic inert — one mode at a time (T:8329)", () => {
  const { seats } = draw(snap("off"), { commentArmed: true });
  expect(seats[0].disabled).toBe(true);
  expect(seats[0]["aria-disabled"]).toBe("true");
});

test("no pane to annotate: HIDDEN, not dead (T:238)", () => {
  const { seats } = draw(snap("off"), { shown: false });
  expect(seats).toHaveLength(0);
});

test("commentSeatName names the neighbour for whichever half owns the mode", () => {
  expect(commentSeatName(snap("recording"))?.label).toBe(
    "Comment — unavailable while recording",
  );
  expect(commentSeatName(snap("transcribing"))?.label).toBe(
    "Comment — unavailable while the recording settles",
  );
  expect(commentSeatName(snap("off"))).toBeNull();
});
