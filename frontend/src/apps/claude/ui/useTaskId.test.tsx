// TWO MOUNTS, ONE READ, BOTH ANSWERED. A card and its own TaskPeek are two
// mounts on the same session, and the dedupe that keeps `/api/tasks` to one
// read must not cost the second mount its answer — it used to sit on the
// session hash until something unrelated re-rendered it.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const { useTaskId, forgetTaskCaches } = await import("./Kebab");

const SESSION = "abcdef0123456789";
let calls = 0;
let answer: () => Promise<unknown> = async () => ({
  tasks: [{ key: SESSION, task_id: 42, status: "done" }],
});
const realFetch = globalThis.fetch;
beforeEach(() => {
  calls = 0;
  (globalThis as { fetch: unknown }).fetch = async () => {
    calls += 1;
    const body = await answer();
    return { ok: true, json: async () => body } as unknown as Response;
  };
});
const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  forgetTaskCaches(SESSION);
  (globalThis as { fetch: unknown }).fetch = realFetch;
});

function Probe({ onLabel }: { onLabel: (label: string) => void }) {
  onLabel(useTaskId(SESSION));
  return null;
}
/** Mount a probe and return every label it has been handed, in order. */
function probe() {
  const labels: string[] = [];
  let r!: ReactTestRenderer;
  act(() => {
    r = create(<Probe onLabel={(l) => labels.push(l)} />);
  });
  mounted.push(r);
  return labels;
}
const settle = () =>
  act(async () => {
    await new Promise((done) => setTimeout(done, 0));
  });

test("a second mount on the same session gets the number too, off ONE read", async () => {
  const first = probe();
  const second = probe(); // the card and its peek, same session, same tick
  expect(first[0]).toBe("abcdef01"); // the hash paints first
  await settle();
  expect(calls).toBe(1); // still one `/api/tasks` read for the pair
  expect(first[first.length - 1]).toBe("42");
  expect(second[second.length - 1]).toBe("42"); // …and NOT the hash
});

test("a failed read leaves both mounts on the hash and does not cache", async () => {
  answer = async () => {
    throw new Error("offline");
  };
  const first = probe();
  const second = probe();
  await settle();
  expect(first[first.length - 1]).toBe("abcdef01");
  expect(second[second.length - 1]).toBe("abcdef01");
  expect(calls).toBe(1);
  // Nothing was cached, so a later mount tries again.
  answer = async () => ({ tasks: [{ key: SESSION, task_id: 7, status: "done" }] });
  const third = probe();
  await settle();
  expect(calls).toBe(2);
  expect(third[third.length - 1]).toBe("7");
});
