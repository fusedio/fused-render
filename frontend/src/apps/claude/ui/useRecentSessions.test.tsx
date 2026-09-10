// WHEN THE SKELETON IS HONEST, and when a `null` off the subscription must be
// swallowed instead (P4-08 / C G-20).
//
// `subscribeRecent` opens every subscription with `null` — the right signal for
// the first read of the page's life, and the wrong one for every read after it.
// T's rule is "the skeleton stands in for rows we do not have yet — never for
// rows that are already up" (T:18408-18411), and separately "the two counts are
// deliberately NOT reset" on the way back to the landing, because "a stale count
// for a moment is quieter than a section that blinks" (T:13049-13053).
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import type { SessionRow } from "../protocol/types";

const { useRecentSessions } = await import("./useRecentSessions");
type SubscribeRecent = import("./useRecentSessions").SubscribeRecent;

/** The subscriptions this mount opened, newest last, each with the callback the
 *  hook handed in and whether it has been torn down. */
interface Sub {
  agentDir: string;
  file: string | null;
  cb(rows: SessionRow[] | null): void;
  stopped: boolean;
  /** T's `leftLive` — whether this subscription asked for the two extra looks. */
  coverWrite: boolean;
}
const subs: Sub[] = [];
/** HANDED IN, not module-patched: an ESM namespace object is frozen, and a
 *  `mock.module` would replace `protocol/sessions` for every suite loaded after
 *  this one in the same process. */
const subscribe = ((
  agentDir: string,
  file: string | null,
  cb: (rows: SessionRow[] | null) => void,
  _env: unknown,
  coverWrite = false,
) => {
  const sub: Sub = { agentDir, file, cb, stopped: false, coverWrite };
  subs.push(sub);
  // Every real subscription opens with the skeleton signal.
  cb(null);
  return () => {
    sub.stopped = true;
  };
}) as SubscribeRecent;

const row = (id: string): SessionRow =>
  ({ id, preview: id, last_used: 1 }) as SessionRow;

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  subs.length = 0;
});

interface Harness {
  rows(): SessionRow[] | null;
  /** Re-render with a different target, or with none (entering a chat). */
  retarget(agentDir: string | null, file?: string | null): Promise<void>;
  /** The newest subscription's callback. */
  serve(rows: SessionRow[] | null): Promise<void>;
  subs: Sub[];
}

async function mount(
  agentDir: string | null,
  file: string | null,
  coverWrite = false,
): Promise<Harness> {
  let out: SessionRow[] | null = null;
  function Probe(p: { agentDir: string | null; file: string | null }) {
    out = useRecentSessions(p.agentDir, p.file, subscribe, coverWrite);
    return null;
  }
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(createElement(Probe, { agentDir, file }));
  });
  mounted.push(r);
  return {
    rows: () => out,
    async retarget(next: string | null, nextFile: string | null = file) {
      await act(async () => {
        r.update(createElement(Probe, { agentDir: next, file: nextFile }));
      });
    },
    async serve(rows) {
      const sub = subs[subs.length - 1];
      await act(async () => sub.cb(rows));
    },
    subs,
  };
}

test("BOOT is the only place the skeleton is real", async () => {
  const h = await mount("/tpl", "/repo/x.py");
  expect(h.rows()).toBe(null);
  await h.serve([row("a")]);
  expect(h.rows()?.map((s) => s.id)).toEqual(["a"]);
});

test("a target change repaints in place — no blink into placeholder bars", async () => {
  const h = await mount("/tpl", "/repo/x.py");
  await h.serve([row("a"), row("b")]);

  // The re-subscribe fires `null` first, and a list already drawn must keep its
  // rows through it (T:18408-18411).
  await h.retarget("/tpl", "/repo/y.py");
  expect(h.subs.length).toBe(2);
  expect(h.rows()?.map((s) => s.id)).toEqual(["a", "b"]);

  // Then the new target's real answer lands and replaces them.
  await h.serve([row("c")]);
  expect(h.rows()?.map((s) => s.id)).toEqual(["c"]);
});

test("ENTERING A CHAT does not empty the list (T:13049-13053)", async () => {
  const h = await mount("/tpl", "/repo/x.py");
  await h.serve([row("a")]);

  // `ClaudeChat` passes a null agentDir while in a chat, which tears the
  // subscription down. The rows stay: the tab bar over them counts them, and
  // taking it off screen and putting it back for the trip is the blink T
  // deliberately avoids.
  await h.retarget(null);
  expect(h.subs[0].stopped).toBe(true);
  expect(h.rows()?.map((s) => s.id)).toEqual(["a"]);

  // Back: a fresh subscription, still no skeleton over the drawn rows.
  await h.retarget("/tpl");
  expect(h.subs.length).toBe(2);
  expect(h.rows()?.map((s) => s.id)).toEqual(["a"]);
});

test("an honestly EMPTY answer is still published", async () => {
  const h = await mount("/tpl", "/repo/x.py");
  await h.serve([row("a")]);
  // `[]` is not `null`: a folder whose chats really went away must go back to
  // no section at all, or the block outlives its rows.
  await h.serve([]);
  expect(h.rows()).toEqual([]);
});

test("A COLD LANDING ASKS FOR NO EXTRA LOOKS (P4-21)", async () => {
  const cold = await mount("/tpl", "/repo/x.py");
  expect(cold.subs[0].coverWrite).toBe(false);
});

test("leaving a LIVE turn asks for them (T's `leftLive`, T:13066-13071)", async () => {
  const live = await mount("/tpl", "/repo/x.py", true);
  expect(live.subs[0].coverWrite).toBe(true);
});
