// MODEL AND THINKING, END TO END — the card's two run settings from the press
// on the dropdown to the body on the wire (Akshil, 2026-09-18: "when we change
// model and effort from the new task modal it doesn't get reflected").
//
// Everything about these two was already pinned at the EDGES — `buildSchedulePayload`
// puts them on the wire (new-task-form.test.ts), `seededDraftForm` reads them
// back off a stored draft (same file), `schedule.create` stores them and
// `_send` hands them to the spawn (test_schedule_api.py, test_schedule_images.py).
// What nothing covered was the middle: that pressing an option in the card's own
// dropdown reaches any of them. A picker wired to the wrong setter, a `value`
// read off a stale variable or a `<details>` that slams shut on re-render would
// pass every one of those assertions and still be the reported bug.
//
// So this mounts the real card, presses the real options, and looks at what
// comes out — the trigger's own label, the draft PUT, and the create POST.
//
// A stubbed `globalThis.fetch`, not `mock.module`, and a dynamic `import()` of
// the card AFTER the shim is installed: both for the reasons
// `schedule-hop.render.test.tsx`'s header gives (module-scope reads of
// `location`, and a process-wide module replacement leaking into other suites).
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();

import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";

const realFetch = globalThis.fetch;
const doc = globalThis.document as unknown as { body?: unknown };

/** The card's own refs resolve against the CONTAINER their node landed in, and
 *  the modal chassis portals into `document.body` — so the stand-in body is
 *  also where `createNodeMock` has to live. Same fixture as the hop suite's,
 *  and removed in `afterEach` for its reason: `bun test` shares one
 *  `globalThis`, and a standing `document.body` changes what other libraries
 *  decide to do. */
function node() {
  return {
    focus() {}, blur() {}, select() {}, setSelectionRange() {}, scrollIntoView() {},
    addEventListener() {}, removeEventListener() {},
    contains: () => false, closest: () => null,
    querySelector: () => null, querySelectorAll: () => [] as unknown[],
    children: [] as unknown[], style: {} as Record<string, string>, value: "",
    getBoundingClientRect: () => ({
      top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0,
    }),
  };
}

// The measuring apparatus the card's mount effects reach for. Inert: nothing
// here resizes, and every style reads as "" — which is what an unstyled element
// answers anyway.
const inert = class {
  observe() {} unobserve() {} disconnect() {}
  takeRecords() { return [] as unknown[]; }
};
const g = globalThis as Record<string, unknown>;
g.ResizeObserver = inert;
g.MutationObserver = inert;
g.getComputedStyle = () => new Proxy(
  { getPropertyValue: () => "", getPropertyPriority: () => "", item: () => "", length: 0 } as Record<string, unknown>,
  { get: (t, k) => (k in t ? t[k as string] : "") },
);

/**
 * THE TWO TIMER MEMBERS THE CARD REACHES FOR ON `window` — its path check
 * debounces with `window.setTimeout` (NewJobModal, `targetVerdict`).
 *
 * `testDomShim` states them, and states them with `??=` — so a suite earlier in
 * the run that put a `window` of its own on the shared `globalThis` (bun runs
 * every suite against one) leaves the shim with nothing to do and this card
 * throwing out of a passive effect, which React answers by unmounting the tree
 * to the root. In isolation these tests pass; in the full run they died on
 * whichever neighbour happened to go first. Same class of leak the hop suite's
 * `installMeasuring` states its observers for, and the same answer: say what
 * this suite needs, and hand the global back exactly as it was found.
 *
 * FILLED IN, NEVER REPLACED. The shim's own header forbids a competing `window`
 * — so this adds the two members to whatever object is already there, and only
 * when they are missing.
 */
const timersWas = new Map<string, unknown>();

function installWindowTimers() {
  const w = globalThis.window as unknown as Record<string, unknown>;
  if (!w) return;
  for (const name of ["setTimeout", "clearTimeout"]) {
    if (typeof w[name] === "function") continue;
    timersWas.set(name, name in w ? w[name] : undefined);
    w[name] = (globalThis[name as "setTimeout"] as (...a: never[]) => unknown)
      .bind(globalThis);
  }
}

function removeWindowTimers() {
  const w = globalThis.window as unknown as Record<string, unknown>;
  for (const [name, before] of timersWas) {
    if (before === undefined) delete w[name];
    else w[name] = before;
  }
  timersWas.clear();
}

let box: ReactTestRenderer | null = null;

afterEach(async () => {
  if (box) {
    const b = box;
    box = null;
    await act(async () => b.unmount());
  }
  globalThis.fetch = realFetch;
  removeWindowTimers();
  delete doc.body;
});

type Sent = { url: string; body: Record<string, unknown> | null };

function json(body: unknown): Promise<Response> {
  return Promise.resolve(
    { ok: true, status: 200, json: () => Promise.resolve(body) } as unknown as Response,
  );
}

/** The card, on a server that answers everything it asks with the emptiest true
 *  answer there is, recording every write. */
async function openCard(props: Record<string, unknown>, sent: Sent[]) {
  const { default: NewJobModal } = await import("./NewJobModal");
  installWindowTimers();
  doc.body = { nodeType: 1, children: [] as unknown[], createNodeMock: node };
  globalThis.fetch = ((url: string, init?: RequestInit) => {
    const u = String(url);
    if (init?.method && init.method !== "GET") {
      sent.push({ url: u, body: init.body ? JSON.parse(String(init.body)) : null });
    }
    if (u.startsWith("/api/config")) return json({ home: "/Users/me" });
    if (u === "/api/schedule") return json({ entry: { id: "e1" } });
    // The global Claude preference the card opens on (2026-09-21): no
    // "Default" row any more, the pair the run will get is what is shown.
    if (u === "/api/claude-sessions/defaults") return json({ model: "fable", effort: "low" });
    return json({ folders: [], entries: [], tasks: [], sessions: [], ok: true, version: 1 });
  }) as unknown as typeof fetch;
  await act(async () => {
    box = create(createElement(NewJobModal as never, {
      initialTime: null, permissionModes: ["auto", "default"], initialTarget: "/p",
      onClose: () => {}, onCreated: () => {}, ...props,
    } as never), { createNodeMock: node });
  });
  return box!;
}

/** What one of the card's dropdown triggers is currently SAYING. */
function label(b: ReactTestRenderer, aria: string): string {
  const trigger = b.root.findAll(
    (n) => n.type === "button" && n.props["aria-label"] === aria
      && n.props["aria-haspopup"] === "listbox",
  )[0];
  const said = trigger.findAll((n) => n.props?.className === "schedule-select-label")[0];
  return String(said.children[0]);
}

/** Open one dropdown and press the option that says `choice` — the reader's own
 *  gesture, not a `setState`. */
async function pick(b: ReactTestRenderer, aria: string, choice: string) {
  const trigger = b.root.findAll(
    (n) => n.type === "button" && n.props["aria-label"] === aria
      && n.props["aria-haspopup"] === "listbox",
  )[0];
  await act(async () => { trigger.props.onClick(); });
  const option = b.root
    .findAll((n) => n.type === "button" && n.props.role === "option")
    .find((o) => String(o.children[0]) === choice);
  expect(option, `no "${choice}" option under ${aria}`).toBeTruthy();
  await act(async () => { option!.props.onClick(); });
}

/** Type into one of the card's text boxes, addressed the way a reader finds it
 *  — by the label it shows. */
async function type(b: ReactTestRenderer, placeholderOrLabel: RegExp, value: string) {
  const field = b.root
    .findAll((n) => n.type === "input" || n.type === "textarea")
    .find((n) => placeholderOrLabel.test(
      String(n.props["aria-label"] ?? n.props.placeholder ?? ""),
    ));
  expect(field, `no field matching ${placeholderOrLabel}`).toBeTruthy();
  await act(async () => { field!.props.onChange({ target: { value } }); });
}

/** ✕ / Esc / click-out, as far as the autosave is concerned: the card unmounts
 *  and its flush is what writes. "Stated, not sent" — the card writes when it
 *  goes, not 600 ms after every keystroke. */
async function closeCard() {
  await act(async () => { const b = box!; box = null; b.unmount(); });
  await act(async () => { await new Promise((r) => setTimeout(r, 60)); });
}

test("picking a model and a thinking level is what the card then says", async () => {
  const b = await openCard({}, []);
  // Opens on the global preference the stub answers — Fable / Low — not on a
  // "Default" placeholder.
  expect([label(b, "Model"), label(b, "Thinking")]).toEqual(["Fable", "Low"]);

  await pick(b, "Model", "Opus");
  await pick(b, "Thinking", "Max");

  // The LABEL, from the key the option carried — `taskRunLabel`'s round trip.
  expect([label(b, "Model"), label(b, "Thinking")]).toEqual(["Opus", "Max"]);
  // …and the other field on that row is untouched. They are one row, not one
  // value: a picker wired to its neighbour's setter would still look right in
  // the assertion above.
  expect(label(b, "Permissions")).toBe("Auto");
});

test("what was picked is what Create sends", async () => {
  const sent: Sent[] = [];
  const b = await openCard({}, sent);
  await type(b, /What should Claude do/i, "My task");
  await type(b, /Additional instructions/i, "do the thing");
  await pick(b, "Model", "Opus");
  await pick(b, "Thinking", "Max");

  const create = b.root
    .findAll((n) => n.type === "button")
    .find((n) => String(n.children?.[0] ?? "") === "Create");
  await act(async () => { create!.props.onClick(); });
  await act(async () => { await new Promise((r) => setTimeout(r, 50)); });

  const post = sent.find((s) => s.url === "/api/schedule");
  expect(post?.body).toMatchObject({ model: "opus", effort: "max" });
});

test("what was picked survives the card being closed and reopened", async () => {
  // A card the reader has already typed into — which is what makes a draft at
  // all (`draftContent`: words or files, never a setting on its own).
  const sent: Sent[] = [];
  const first = await openCard(
    { initialDraft: { id: "d1", form: { title: "T", description: "d", target: "/p" } } },
    sent,
  );
  await pick(first, "Model", "Opus");
  await pick(first, "Thinking", "Max");
  await closeCard();

  const put = sent.find((s) => s.url === "/api/drafts/task/d1");
  expect(put?.body).toMatchObject({ model: "opus", effort: "max" });

  // …and the stored form is what the card comes back up on, which is the half
  // the reader actually sees. Seeded from the PUT's own body, so the two
  // halves cannot pass while disagreeing about the spelling.
  const again = await openCard({ initialDraft: { id: "d1", form: put!.body } }, []);
  expect([label(again, "Model"), label(again, "Thinking")]).toEqual(["Opus", "Max"]);
});

test("an edit opens on the model the task was saved with", async () => {
  const b = await openCard({
    editing: {
      id: "s1", target: "/p", message: "hi", title: "T",
      due: new Date().toISOString(), state: "pending", immediate: false,
      model: "sonnet", effort: "high",
    },
  }, []);
  expect([label(b, "Model"), label(b, "Thinking")]).toEqual(["Sonnet", "High"]);
});
