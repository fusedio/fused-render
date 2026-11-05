// TaskStar: no star while the favourites switch is off or the task never ran;
// a press shows its answer AT ONCE (local mark), writes the star, never
// reaches the row's own press, and a refused write puts the star back.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer, type ReactTestInstance } from "react-test-renderer";
import type { Task } from "@platform/lib/api";

const { TaskStar, STAR_ON_TITLE, STAR_OFF_TITLE } = await import("./TaskStar");
const { publishTaskFavourites, publishFavouritesParent, resetTaskFavouritesFlagForTests } = await import(
  "@platform/lib/favourites-flag"
);
const { resetPrefsChannelForTests } = await import("@platform/lib/api");
const { TASKS_CHANGED_EVENT } = await import("@platform/lib/tasksChanged");

type Call = { url: string; body: unknown };
let calls: Call[] = [];
// What GET /api/prefs answers: the flag's own read must agree with the
// publish, or it lands "off" after it.
let prefs: unknown = {};
let reply: (body: { key: string; on: boolean }) => Promise<Response>;
const realFetch = globalThis.fetch;
const mounted: ReactTestRenderer[] = [];

function okReply(body: { key: string; on: boolean }) {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: async () => ({ ok: true, key: body.key, starred: body.on, open: false }),
  } as unknown as Response);
}

beforeEach(() => {
  calls = [];
  prefs = {};
  reply = okReply;
  resetTaskFavouritesFlagForTests();
  (globalThis as { fetch: unknown }).fetch = async (url: string, init?: RequestInit) => {
    if (!String(url).includes("/api/tasks/favourite")) {
      return { ok: true, status: 200, json: async () => prefs } as unknown as Response;
    }
    const body = JSON.parse(String(init?.body ?? "{}"));
    calls.push({ url: String(url), body });
    return reply(body);
  };
});
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  resetPrefsChannelForTests();
  resetTaskFavouritesFlagForTests();
  (globalThis as { fetch: unknown }).fetch = realFetch;
});

function flagOn() {
  prefs = { task_favourites: { enabled: true }, canvas_rail_peek: { enabled: true } };
  act(() => {
    publishTaskFavourites({ enabled: true });
    publishFavouritesParent({ enabled: true });
  });
}

function task(over: Partial<Task> = {}): Pick<Task, "key" | "session_id" | "starred"> {
  return { key: "k1", session_id: "s1", starred: false, ...over };
}

async function mount(el: React.ReactElement) {
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(el);
  });
  mounted.push(r);
  return r;
}

const starOf = (r: ReactTestRenderer): ReactTestInstance | null =>
  r.root.findAll((n) => typeof n.type === "string" && n.props["data-task-star"] !== undefined)[0] ?? null;

function fakeEvent() {
  const seen = { prevented: false, stopped: false };
  return {
    seen,
    ev: { preventDefault: () => (seen.prevented = true), stopPropagation: () => (seen.stopped = true) },
  };
}

const flush = () =>
  act(async () => {
    for (let i = 0; i < 5; i++) await new Promise((res) => setTimeout(res, 0));
  });

test("FLAG OFF: no star at all", async () => {
  const r = await mount(<TaskStar task={task()} />);
  expect(starOf(r)).toBeNull();
});

test("NOT STARTED (no session_id): no star, even with the flag on", async () => {
  flagOn();
  const r = await mount(<TaskStar task={task({ session_id: "" })} />);
  expect(starOf(r)).toBeNull();
});

test("the skin: a button, aria-pressed and the two titles, filled only when on", async () => {
  flagOn();
  const off = await mount(<TaskStar task={task()} />);
  const s = starOf(off)!;
  expect(s.type).toBe("button");
  expect(s.props["aria-pressed"]).toBe(false);
  expect(s.props.title).toBe(STAR_OFF_TITLE);
  expect(s.props.className).not.toContain("is-on");
  const on = await mount(<TaskStar task={task({ key: "k2", starred: true })} />);
  const t = starOf(on)!;
  expect(t.props["aria-pressed"]).toBe(true);
  expect(t.props.title).toBe(STAR_ON_TITLE);
  expect(t.props.className).toContain("is-on");
  // lucide's svg carries the fill.
  expect(t.findByType("svg").props.fill).toBe("currentColor");
});

test("PRESS: fills at once, writes the star, pokes the tasks store, never reaches the row", async () => {
  flagOn();
  // The DOM shim's window drops events, so the poke is read off dispatchEvent.
  const fired: string[] = [];
  const realDispatch = window.dispatchEvent;
  window.dispatchEvent = (e: Event) => {
    fired.push(e.type);
    return true;
  };
  try {
    const r = await mount(<TaskStar task={task()} />);
    const { ev, seen } = fakeEvent();
    act(() => starOf(r)!.props.onClick(ev));
    // The mark, before the write has answered.
    expect(starOf(r)!.props["aria-pressed"]).toBe(true);
    expect(seen).toEqual({ prevented: true, stopped: true });
    await flush();
    expect(calls.map((c) => c.body)).toEqual([{ key: "k1", on: true }]);
    expect(fired.filter((t) => t === TASKS_CHANGED_EVENT).length).toBe(1);
    // The row has not caught up yet: the mark holds.
    expect(starOf(r)!.props["aria-pressed"]).toBe(true);
    // The row agrees: the mark goes, the row's answer shows.
    await act(async () => r.update(<TaskStar task={task({ starred: true })} />));
    expect(starOf(r)!.props["aria-pressed"]).toBe(true);
  } finally {
    window.dispatchEvent = realDispatch;
  }
});

test("UNSTAR: an on star pressed goes hollow at once and writes off", async () => {
  flagOn();
  const r = await mount(<TaskStar task={task({ starred: true })} />);
  act(() => starOf(r)!.props.onClick(fakeEvent().ev));
  expect(starOf(r)!.props["aria-pressed"]).toBe(false);
  await flush();
  expect(calls.map((c) => c.body)).toEqual([{ key: "k1", on: false }]);
});

test("REFUSED: the star goes back to the row's own answer", async () => {
  flagOn();
  reply = () =>
    Promise.resolve({
      ok: false,
      status: 409,
      statusText: "Conflict",
      json: async () => ({ detail: "nope" }),
      text: async () => JSON.stringify({ detail: "nope" }),
    } as unknown as Response);
  const r = await mount(<TaskStar task={task()} />);
  act(() => starOf(r)!.props.onClick(fakeEvent().ev));
  expect(starOf(r)!.props["aria-pressed"]).toBe(true);
  await flush();
  expect(starOf(r)!.props["aria-pressed"]).toBe(false);
});

test("NESTED (a host that is a <button>): a span with role=button, Enter presses", async () => {
  flagOn();
  const r = await mount(<TaskStar task={task()} nested />);
  const s = starOf(r)!;
  expect(s.type).toBe("span");
  expect(s.props.role).toBe("button");
  expect(s.props.tabIndex).toBe(0);
  const { ev, seen } = fakeEvent();
  act(() => s.props.onKeyDown({ ...ev, key: "Enter" }));
  expect(seen.stopped).toBe(true);
  expect(starOf(r)!.props["aria-pressed"]).toBe(true);
  await flush();
  expect(calls.length).toBe(1);
});

test("reveal=\"hover\" marks the star for the row-hover rule (task-star.css)", async () => {
  flagOn();
  const r = await mount(<TaskStar task={task()} reveal="hover" />);
  expect(starOf(r)!.props.className).toContain("task-star--hover");
});

test("TWO QUICK PRESSES: write 1's repaint does not flip the star; the end state is press 2", async () => {
  flagOn();
  const releases: Array<() => void> = [];
  reply = (body) =>
    new Promise<Response>((res) => {
      releases.push(() => res(okReply(body) as unknown as Response));
    }).then((r) => r);
  const r = await mount(<TaskStar task={task()} />);
  act(() => starOf(r)!.props.onClick(fakeEvent().ev));
  act(() => starOf(r)!.props.onClick(fakeEvent().ev));
  expect(starOf(r)!.props["aria-pressed"]).toBe(false);
  // Write 1 lands and its announce repaints the row as starred: the second
  // press is still out, so the star must not show the intermediate state.
  await act(async () => {
    for (let i = 0; i < 5 && releases.length < 1; i++) await new Promise((res) => setTimeout(res, 0));
    releases[0]!();
    await new Promise((res) => setTimeout(res, 0));
  });
  await act(async () => {
    r.update(<TaskStar task={task({ starred: true })} />);
  });
  expect(starOf(r)!.props["aria-pressed"]).toBe(false);
  await act(async () => {
    for (let i = 0; i < 5 && releases.length < 2; i++) await new Promise((res) => setTimeout(res, 0));
    releases[1]!();
  });
  await flush();
  expect(calls.map((c) => c.body)).toEqual([
    { key: "k1", on: true },
    { key: "k1", on: false },
  ]);
  await act(async () => {
    r.update(<TaskStar task={task({ starred: false })} />);
  });
  expect(starOf(r)!.props["aria-pressed"]).toBe(false);
});
