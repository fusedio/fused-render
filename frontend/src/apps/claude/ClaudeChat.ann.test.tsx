// THE WIRING THE ANNOTATION SUBSYSTEM'S OWN SUITES CANNOT SEE (inventory 02).
//
// `ann/*.test.ts` proves each piece with its deps injected — the store against a
// memory param store, the mode machine against a fake recorder, the geometry
// against fixtures. What none of them can reach is how `ClaudeChat` JOINS them:
// the strip's arm, the chip row inside the attachment tray, the send path's
// `<annotations>` block and its badged overview riding first in the pictures,
// Escape's discard, `enterNoPane`'s clear and the narrow view's disarm.
//
// So this file mounts the REAL component over a stubbed `fetch` and a patched
// `ATTACH_API`, exactly as `ClaudeChat.attach.test.tsx` does, and asserts through
// what reaches `/api/run` or what the chip row renders — because that is where
// getting any of it wrong actually lands.
//
// A NOTE IS MADE BY A CLICK INSIDE THE FRAMED APP, and that click is DISPATCHED
// here rather than stood in for: the component is handed an `annotateTarget`
// whose document is a fake with a working listener registry, so the gesture
// travels the real road — the seven listeners the coordinator attached, the anchor
// the click handler builds, the composer's placement, the commit. That road was
// where all three of PR3's blockers lived, and `store.add()` as a stand-in for a
// click could not see any of them.
//
// Two things stay stand-ins, because `react-test-renderer` has no DOM to build
// them from: the composer NODE (`AnnPopover`'s host ref is null, so the test
// binds its own) and the TYPING (the node's own Enter handler cannot exist, so
// the commit is called as that key would call it). Everything between is real.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create } from "react-test-renderer";

const { ClaudeChat, annotationsForTests } = await import("./ClaudeChat");
const { ATTACH_API } = await import("./ui/attachApi");
const { createMemoryParamsStore } = await import("./params/store");
const { resetAgentDirCacheForTests } = await import("./protocol/agent");
const { ANN_TAG, PANE_SHOT_TAG } = await import("./protocol/wire");
type Attachment = import("./shots/types").Attachment;
type AttachApi = import("./ui/attachApi").AttachApi;

// ---- the server, cut down to what a booting PROJECT chat touches -----------
//
// A project (a folder whose `app.py` names an entry html) is the one target that
// gets a pane of its own — and a pane is what `annCapable` means, so nothing
// below can be armed without one.

interface RunCall {
  action: string;
  params: Record<string, string>;
}
const runs: RunCall[] = [];
let startError = "";
let appEntry: string | null = "/w/p/index.html";
/** `GET /api/capture`'s `sources`, so a test can be a machine with no mic. */
let audioSource: Record<string, unknown> = { audio: { available: true, reason: null } };

const realFetch = globalThis.fetch;

function jsonRes(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

function stubFetch(): void {
  (globalThis as { fetch: unknown }).fetch = async (
    input: unknown,
    init?: { body?: unknown },
  ): Promise<Response> => {
    const url = String(typeof input === "string" ? input : (input as { url: string }).url);
    if (url.startsWith("/api/fs/stat")) {
      return jsonRes({
        path: "/w/p",
        is_dir: true,
        templates: [{ mode: "claude", path: "/w/p/.claude/template.html" }],
      });
    }
    if (url === "/api/prefs") return jsonRes({});
    // A34's boot probe (`captureSources`) — what this machine can record, asked
    // without prompting for permission.
    if (url === "/api/capture") return jsonRes({ sources: audioSource });
    if (url.startsWith("/api/tasks/changes")) return new Promise<Response>(() => {});
    if (url === "/api/run") {
      const body = JSON.parse(String(init?.body ?? "{}")) as {
        py: string;
        params: Record<string, string>;
      };
      // The pane's own decision: an entry makes this a PROJECT and gives the
      // chat a frame to annotate; `null` makes it an ordinary folder, which is
      // `enterNoPane`'s road (D239).
      if (String(body.py).endsWith("/app.py")) {
        return jsonRes({
          ok: true,
          result: appEntry ? { entry: appEntry, noun: "project" } : { entry: "" },
        });
      }
      const action = String(body.params?.action ?? "");
      runs.push({ action, params: body.params ?? {} });
      if (action === "start") {
        return jsonRes({ ok: true, result: startError ? { error: startError } : { run_id: "r1" } });
      }
      if (action === "poll") {
        return jsonRes({ ok: true, result: { done: true, session_id: "s1", text: "ok" } });
      }
      return jsonRes({ ok: true, result: {} });
    }
    return jsonRes({});
  };
}

// ---- the pipeline, patched IN PLACE (the attach suite's idiom) -------------

const API = ATTACH_API as unknown as Record<string, unknown>;
const patched = new Map<string, { had: boolean; was: unknown }>();

function patchApi(over: Partial<AttachApi>): void {
  for (const [k, v] of Object.entries(over)) {
    if (!patched.has(k)) patched.set(k, { had: k in API, was: API[k] });
    API[k] = v;
  }
}

function restoreApi(): void {
  for (const [k, snap] of patched) {
    if (snap.had) API[k] = snap.was;
    else delete API[k];
  }
  patched.clear();
}

let asFound: Record<string, unknown> = {};
/** Every overview the send path uploaded, so a test can see it took ONE. */
let overviews = 0;
/** Everything `revoke` was handed — the overview's own road on a failed send. */
let revoked: Attachment[] = [];

// ---- Escape, which the shim's `document` cannot dispatch -------------------
//
// The shim's document has no-op listeners by design (it is not a DOM). The chat
// binds its Escape claim there, on the bubble phase, exactly as T does — so the
// handlers are collected here and called by hand, which is also the only way to
// assert the CLAIM ORDER (a handler that ran and did not preventDefault).

const keydowns: Array<(e: KeyboardEvent) => void> = [];
let realAdd: unknown;
let realRemove: unknown;
let realCreate: unknown;

function pressEscape(): { defaultPrevented: boolean } {
  const ev = {
    key: "Escape",
    target: null,
    defaultPrevented: false,
    preventDefault() {
      (this as { defaultPrevented: boolean }).defaultPrevented = true;
    },
  };
  for (const fn of [...keydowns]) fn(ev as unknown as KeyboardEvent);
  return ev;
}

beforeEach(() => {
  runs.length = 0;
  startError = "";
  appEntry = "/w/p/index.html";
  audioSource = { audio: { available: true, reason: null } };
  overviews = 0;
  revoked = [];
  keydowns.length = 0;
  asFound = { ...API };
  resetAgentDirCacheForTests();
  stubFetch();
  TARGET = targetRig();
  POP = popRig();
  swallowed = { prevented: 0, stopped: 0 };
  const doc = globalThis.document as unknown as Record<string, unknown>;
  realAdd = doc.addEventListener;
  realRemove = doc.removeEventListener;
  // THE CHAT'S OWN DOCUMENT, made buildable. `useAnnotations` treats a document
  // with no `createElement` as no document at all (its own first paragraph), and
  // that answer is right for the shim — but it also parks the composer, so the
  // click path this suite exists to drive would stop one call short. Restored in
  // `afterEach`, exactly as the listeners above are.
  realCreate = doc.createElement;
  doc.createElement = (tag: string) => elem(String(tag));
  doc.addEventListener = (type: string, fn: (e: KeyboardEvent) => void) => {
    if (type === "keydown") keydowns.push(fn);
  };
  doc.removeEventListener = (_type: string, fn: (e: KeyboardEvent) => void) => {
    const i = keydowns.indexOf(fn);
    if (i !== -1) keydowns.splice(i, 1);
  };
  patchApi({
    flash: () => () => {},
    filesFromPaste: (ev) =>
      (ev as { clipboardData?: unknown }).clipboardData ? [{ name: "shot.png" } as File] : [],
    attachFiles: async function* (_dir, files) {
      for (const f of files) {
        yield {
          id: "f1",
          kind: "image",
          view: "/shots/" + (f.name || "x"),
          name: f.name,
        } satisfies Attachment;
      }
    },
    attachPaths: () => [],
    attachPane: async () => ({ id: "pane1", kind: "pane", seat: "pane", view: "/shots/v.png" }),
    // The badged picture the annotations block tells the model to read.
    attachOverview: async () => {
      overviews += 1;
      return { id: "ov1", kind: "overview", view: "/shots/overview.png" } satisfies Attachment;
    },
    readDirs: () => ["/shots"],
    readDirsFor: () => ["/shots"],
    revoke: (a) => {
      if (a) revoked.push(a);
    },
    dragHasAttachment: (dt) => !!dt,
    pathsFromDrop: () => [],
  });
});

// ---- the framed app, and the composer node the renderer cannot build --------

interface Listener {
  type: string;
  fn: (e: unknown) => void;
  /** THE CAPTURE FLAG IS PART OF THE IDENTITY, which the real
   *  `removeEventListener` requires and this rig used to ignore: a teardown that
   *  passed the wrong flag would pass here and leak in the browser. Tracked the
   *  same way `ann/target.test.ts`'s registry tracks it, so the two rigs agree
   *  (PR3 review 2). */
  capture: boolean;
}

function isCapture(opt: unknown): boolean {
  if (opt === true) return true;
  if (!opt || typeof opt !== "object") return false;
  return !!(opt as { capture?: boolean }).capture;
}

/** ONE body object: `pathOf` walks `parentElement` until it reaches `doc.body`. */
const BODY = { tagName: "BODY", nodeType: 1 };

function elem(tag: string): Record<string, unknown> {
  const classes = new Set<string>();
  const node: Record<string, unknown> = {
    tagName: tag.toUpperCase(),
    nodeType: 1,
    id: "",
    type: "",
    hidden: false,
    innerHTML: "",
    textContent: "",
    className: "",
    dataset: {},
    style: {} as Record<string, string>,
    children: [] as unknown[],
    classList: {
      add: (c: string) => classes.add(c),
      remove: (c: string) => classes.delete(c),
      contains: (c: string) => classes.has(c),
    },
    setAttribute: () => {},
    append: (...kids: unknown[]) => {
      (node.children as unknown[]).push(...kids);
    },
    appendChild: (kid: unknown) => {
      (node.children as unknown[]).push(kid);
    },
    querySelector: () => null,
    addEventListener: () => {},
    removeEventListener: () => {},
    // `ui/fit.ts` prices text off an off-DOM canvas and memoizes the context for
    // the process: `null` is its own documented DOM-less answer.
    getContext: () => null,
  };
  return node;
}

/** The marked frame and the document inside it, with a real listener registry —
 *  this is what makes a dispatched click a dispatched click. */
function targetRig() {
  const listeners: Listener[] = [];
  const doc = {
    body: BODY,
    documentElement: { style: { cursor: "" } as { cursor: string } },
    defaultView: { scrollX: 0, scrollY: 0 } as unknown as Window,
    querySelector: () => null,
    addEventListener: (type: string, fn: (e: unknown) => void, opt?: unknown) => {
      listeners.push({ type, fn, capture: isCapture(opt) });
    },
    removeEventListener: (type: string, fn: (e: unknown) => void, opt?: unknown) => {
      const capture = isCapture(opt);
      const i = listeners.findIndex(
        (l) => l.type === type && l.fn === fn && l.capture === capture,
      );
      if (i !== -1) listeners.splice(i, 1);
    },
  } as unknown as Document;
  const frame = {
    tagName: "IFRAME",
    contentDocument: doc,
    contentWindow: { location: { href: "http://app.test/index.html" } },
    addEventListener: () => {},
    removeEventListener: () => {},
  } as unknown as HTMLIFrameElement;
  return {
    doc,
    frame,
    listeners,
    fire(type: string, e: Record<string, unknown>) {
      for (const l of [...listeners]) if (l.type === type) l.fn(e);
    },
  };
}

/** The card. Only its textarea, its style and its classList are ever read. */
function popRig() {
  const ta = { value: "", placeholder: "", focus: () => {}, setSelectionRange: () => {} };
  const classes = new Set<string>();
  const pop = {
    ownerDocument: globalThis.document,
    style: { display: "none" } as Record<string, string>,
    classList: {
      add: (c: string) => classes.add(c),
      remove: (c: string) => classes.delete(c),
      contains: (c: string) => classes.has(c),
    },
    querySelector: (sel: string) => (sel === "textarea" ? ta : null),
    contains: () => false,
    getRootNode: () => null,
  };
  return { pop: pop as unknown as HTMLElement, ta };
}

/** An element in the app, with a resolvable id. */
function appEl(id = "send", text = "Send") {
  return {
    tagName: "BUTTON",
    id,
    nodeType: 1,
    textContent: text,
    children: [],
    previousElementSibling: null,
    parentElement: BODY,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 40, height: 20 }),
    closest: () => null,
  } as unknown as Element;
}

let TARGET = targetRig();
let POP = popRig();
/** What the app's own handler would have seen — A11's swallow, observed. */
let swallowed = { prevented: 0, stopped: 0 };

const mounted: Array<ReturnType<typeof create>> = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  (globalThis as { fetch: unknown }).fetch = realFetch;
  const doc = globalThis.document as unknown as Record<string, unknown>;
  doc.addEventListener = realAdd;
  doc.removeEventListener = realRemove;
  if (realCreate === undefined) delete doc.createElement;
  else doc.createElement = realCreate;
  restoreApi();
  expect(Object.keys(API).sort()).toEqual(Object.keys(asFound).sort());
  for (const k of Object.keys(asFound)) expect(API[k]).toBe(asFound[k]);
});

const baseProps = {
  file: "/w/p",
  chatOnly: false,
  compact: false,
  peek: false,
  autoFocus: false,
} as const;

async function settle(ms = 0): Promise<void> {
  await act(async () => {
    await new Promise((done) => setTimeout(done, ms));
  });
}

/** The annotate target this suite mounts with. Overridable, because "no pane"
 *  means no HOST mark either — the strip follows the target, not the layout, so
 *  a rig that kept handing out a marked frame would keep the seats alive in the
 *  one test whose whole subject is a folder that has nothing to annotate. */
type Target = () => HTMLIFrameElement | null;

async function mountChat(seed: Record<string, string> = {}, target?: Target) {
  const params = createMemoryParamsStore();
  params.set(seed);
  const annotateTarget: Target = target ?? (() => TARGET.frame);
  let r!: ReturnType<typeof create>;
  await act(async () => {
    r = create(
      <ClaudeChat {...baseProps} params={params} annotateTarget={annotateTarget} />,
    );
  });
  mounted.push(r);
  // Two settles: the agent-dir stat, then the pane's own `app.py` decision.
  await settle();
  await settle();
  return { r, params };
}

type Chat = Awaited<ReturnType<typeof mountChat>>["r"];

function byClass(r: Chat, cls: string) {
  return r.root.findAll(
    (n) =>
      typeof n.type === "string" &&
      String((n.props as { className?: string }).className ?? "")
        .split(" ")
        .includes(cls),
  );
}

/** The Comment seat in the `#anncta` strip. */
function commentSeat(r: Chat) {
  return byClass(r, "c-annbtn")[0]!;
}

/** The annotation chips, in row order (`.c-annchip` minus the screenshots'). */
function annChips(r: Chat) {
  return byClass(r, "c-annchip").filter(
    (n) =>
      !String((n.props as { className?: string }).className ?? "")
        .split(" ")
        .includes("c-shotchip"),
  );
}

function rootClass(r: Chat): string {
  return String((byClass(r, "chat-root")[0]!.props as { className: string }).className);
}

const started = () => runs.filter((c) => c.action === "start");

/**
 * ONE NOTE, MADE BY A CLICK — the real road, end to end.
 *
 * A click on NOTHING — narration about whitespace, layout, or something missing
 * — which is a point note, the kind these tests assert about (the crosshair
 * badge, `kind: "point"`). A click on an element is the other road; the test
 * below drives that one. The card is bound here because the renderer cannot
 * build it, and the commit stands in for the Enter key on a node that does not
 * exist.
 */
function makeNote(content: string): void {
  const ann = annotationsForTests();
  if (!ann) throw new Error("no annotation subsystem mounted");
  ann.bindPop(POP.pop);
  clickInApp(BODY as unknown as Element);
  POP.ta.value = content;
  ann.popHandlers.commit(content);
}

/** A capture-phase click on an element inside the framed app. */
function clickInApp(el: Element = appEl()): void {
  TARGET.fire("click", {
    target: el,
    clientX: 40,
    clientY: 60,
    altKey: false,
    preventDefault() {
      swallowed.prevented += 1;
    },
    stopPropagation() {
      swallowed.stopped += 1;
    },
  });
}

// ---- the strip's arm, and the nav lock it takes ---------------------------

test("arming Comment presses the seat and LOCKS the chat", async () => {
  const { r, params } = await mountChat();
  const seat = commentSeat(r);
  // At rest: pressable, and nothing about the chat is locked.
  expect(seat.props["aria-pressed"]).toBe("false");
  expect(seat.props.disabled).toBe(false);
  expect(rootClass(r)).not.toContain("annlock");

  await act(async () => seat.props.onClick());
  await settle();

  expect(commentSeat(r).props["aria-pressed"]).toBe("true");
  // T:7490 — the armed seat's tooltip says what the click will do, which is
  // SEND, and names both ways out.
  expect(String(commentSeat(r).props.title)).toContain("send the notes and finish");
  // T:1466 — a mode holds the reader on THIS chat: the notes are about the app
  // beside it, and ← Chats would carry them off to a chat they are not about.
  expect(rootClass(r)).toContain("annlock");
  // THE ONE PARAM WRITER is the mode machine, and it wrote (T:7761).
  expect(params.get("annmode")).toBe("1");

  // And the camera beside it is inert while a round is armed — spoken, not only
  // dimmed by the stylesheet (T:320, `seatsAria`).
  expect(byClass(r, "c-viewshot")[0]!.props["aria-disabled"]).toBe("true");
});

// ---- a note becomes a chip in the tray's own row --------------------------

test("a point note rides the attachment tray as a chip, and its ✕ takes it back off", async () => {
  const { r } = await mountChat();
  await act(async () => commentSeat(r).props.onClick());
  await settle();
  expect(annChips(r)).toHaveLength(0);

  await act(async () => makeNote("make this bigger"));
  await settle();

  const chips = annChips(r);
  expect(chips).toHaveLength(1);
  // The badge letter is the note's position in the WHOLE list, and a POINT note
  // leads with the crosshair so the two kinds read apart unopened (T:6993).
  const label = chips[0]!.findAll(
    (n) => typeof n.type === "string" && n.props.className === "c-pinlbl",
  )[0]!;
  expect(String(label.props.children)).toBe("⌖A");
  // Notes alone are sendable, with no words at all (T:17903) — which is what ✓
  // Done's whole gesture is.
  expect(r.root.findByType("form")).toBeTruthy();

  // The ✕ (T:7009).
  const drop = chips[0]!.findAll(
    (n) => typeof n.type === "string" && n.props["aria-label"] === "Remove annotation",
  )[0]!;
  await act(async () => drop.props.onClick());
  await settle();
  expect(annChips(r)).toHaveLength(0);
});

test("a click on an ELEMENT inside the framed app is swallowed and becomes a note", async () => {
  // THE BLOCKER'S OWN TEST, at the integration level: the mode armed, a click
  // dispatched into the framed document, and nothing else touched. It used to
  // reach nothing at all — the seven listeners were never attached, so the click
  // ran the app's own handler instead and no composer, pin or chip appeared.
  const { r } = await mountChat();
  await act(async () => commentSeat(r).props.onClick());
  await settle();

  const ann = annotationsForTests()!;
  ann.bindPop(POP.pop);
  await act(async () => clickInApp(appEl("freq", "Frequency")));
  await settle();

  // A11: annotate mode exists to point AT controls without triggering them.
  expect(swallowed.prevented).toBe(1);
  expect(swallowed.stopped).toBe(1);
  // The composer opened on the clicked element, aimed rather than parked.
  expect(POP.pop.style.display).toBe("block");
  expect(POP.ta.placeholder).toBe("What about this element?");

  POP.ta.value = "this input is too wide";
  await act(async () => ann.popHandlers.commit(POP.ta.value));
  await settle();

  // The note carries the anchor the CLICK built — the element's id, its tag and
  // the digest the `<annotations>` stanza prints.
  const note = annotationsForTests()!.annotations[0]!;
  expect(note.content).toBe("this input is too wide");
  expect(note.anchorId).toBe("freq");
  expect(note.tag).toBe("button");
  expect(note.text).toBe("Frequency");
  // And it is a chip in the tray, with the element badge (no crosshair).
  const chips = annChips(r);
  expect(chips).toHaveLength(1);
  const label = chips[0]!.findAll(
    (n) => typeof n.type === "string" && n.props.className === "c-pinlbl",
  )[0]!;
  expect(String(label.props.children)).toBe("A");
});

test("a click in the framed app while DISARMED is the app's own", async () => {
  const { r } = await mountChat();
  annotationsForTests()!.bindPop(POP.pop);
  await act(async () => clickInApp(appEl()));
  await settle();
  // Not swallowed, no note: the mode is the gate, and toggling it off is how the
  // reader gets their app back.
  expect(swallowed.prevented).toBe(0);
  expect(annotationsForTests()!.annotations).toHaveLength(0);
  expect(annChips(r)).toHaveLength(0);
});

test("A34: a machine that cannot record gets NO mic seat", async () => {
  // T:7788's boot probe. "Absent beats dead" — the same rule the camera follows:
  // a seat that could only ever alert is worse than no seat, and this one was
  // never probed at all, so it showed on every machine.
  const withMic = await mountChat();
  expect(byClass(withMic.r, "c-annrec")).toHaveLength(1);

  audioSource = {
    audio: { available: false, reason: "this browser cannot record" },
  };
  const without = await mountChat();
  await settle();
  expect(byClass(without.r, "c-annrec")).toHaveLength(0);
  // The rest of the strip is untouched: commenting and screenshots need no mic.
  expect(byClass(without.r, "c-annbtn")).toHaveLength(1);
  expect(byClass(without.r, "c-viewshot")).toHaveLength(1);
});

test("a probe that cannot be answered KEEPS the mic seat", async () => {
  audioSource = {}; // no `audio` key at all: not a no
  const { r } = await mountChat();
  await settle();
  expect(byClass(r, "c-annrec")).toHaveLength(1);
});

// ---- the send path -------------------------------------------------------

test("a send carries the <annotations> block and the badged overview FIRST", async () => {
  const { r } = await mountChat();
  await act(async () => commentSeat(r).props.onClick());
  await settle();
  await act(async () => makeNote("this button is too small"));
  await settle();
  // One of the user's own pictures too, so the ORDER inside the pictures block
  // is observable: the overview is the one every annotation row refers to
  // (T:16549).
  const box = r.root.findByType("textarea");
  await act(async () => {
    box.props.onPaste({ clipboardData: {}, preventDefault: () => {} });
  });
  await settle();

  await act(async () => {
    r.root.findByType("form").props.onSubmit({ preventDefault: () => {} });
  });
  await settle(30);

  expect(started()).toHaveLength(1);
  const message = started()[0]!.params.message;
  expect(message).toContain("<" + ANN_TAG + ">");
  // The note's words, and the letter the badge burned in.
  expect(message).toContain("this button is too small");
  expect(message).toContain("**A**");
  // ONE picture of the whole pane, taken once.
  expect(overviews).toBe(1);
  expect(message).toContain("<" + PANE_SHOT_TAG + ">");
  expect(message.indexOf("/shots/overview.png")).toBeGreaterThan(-1);
  expect(message.indexOf("/shots/overview.png")).toBeLessThan(
    message.indexOf("/shots/shot.png"),
  );
  // T:10449's reading order: the pictures, then the notes about them, then the
  // typed text. `composeBlocks` is what makes that true whoever emitted them.
  expect(message.indexOf("<" + PANE_SHOT_TAG + ">")).toBeLessThan(
    message.indexOf("<" + ANN_TAG + ">"),
  );

  // T:16064 — the send took them: the chips go with the message, and the run
  // ending cleanly RESOLVES them for good (T:10608, `onRunEnded`).
  expect(annChips(r)).toHaveLength(0);
  expect(annotationsForTests()!.annotations).toHaveLength(0);
});

test("a send that never launched gives the notes back and revokes the overview", async () => {
  // The overview is the page's own picture of a pane that has since moved on, so
  // it is REVOKED where the user's own pictures are handed back — they were
  // attached deliberately and may not be retakeable (T:16698-16708).
  startError = "no session";
  const { r } = await mountChat();
  await act(async () => commentSeat(r).props.onClick());
  await settle();
  await act(async () => makeNote("this one came back"));
  await settle();

  await act(async () => {
    r.root.findByType("form").props.onSubmit({ preventDefault: () => {} });
  });
  await settle(30);

  expect(started()).toHaveLength(1);
  // Pending again, so the chip is back and ✓ Done can send it a second time.
  expect(annChips(r)).toHaveLength(1);
  expect(annotationsForTests()!.annotations[0]!.sent).toBe(0);
  expect(revoked.map((a) => a.kind)).toContain("overview");
});

// ---- Escape ---------------------------------------------------------------

test("Escape in comment mode DISCARDS the round, and claims the press", async () => {
  // Akshil, 2026-09-06 ("I press escape, it doesn't discard it"): Esc in a typed
  // comment mode is CANCEL — this round's unsent notes go, not just the mode.
  const { r } = await mountChat();
  await act(async () => commentSeat(r).props.onClick());
  await settle();
  await act(async () => makeNote("never mind"));
  await settle();
  expect(annChips(r)).toHaveLength(1);

  let ev: { defaultPrevented: boolean } = { defaultPrevented: false };
  await act(async () => {
    ev = pressEscape();
  });
  await settle();

  expect(annChips(r)).toHaveLength(0);
  expect(commentSeat(r).props["aria-pressed"]).toBe("false");
  expect(rootClass(r)).not.toContain("annlock");
  // Claimed, so the host's own Esc (TaskPeek's close) does not act on the same
  // press — the whole point of the order at T:15956.
  expect(ev.defaultPrevented).toBe(true);
});

test("Escape with nothing armed is the HOST's press, not this chat's", async () => {
  const { r } = await mountChat();
  await act(async () => {
    pressEscape();
  });
  await settle();
  // Nothing to claim and nothing broken: the seat is still at rest.
  expect(commentSeat(r).props["aria-pressed"]).toBe("false");
});

// ---- enterNoPane ---------------------------------------------------------

test("enterNoPane drops the notes a bookmark's param brought in", async () => {
  // A live bug found by opening a FOLDER url that still carried an old
  // bookmark's `annotations`: the boot had already painted a chip per composer
  // from the param, and the `noPane` early-return froze it there — a chip
  // showing a note that cannot be sent (`enterNoPane` steps 1a/1b).
  appEntry = null; // an ordinary folder: no pane, ever
  const { r, params } = await mountChat(
    {
      annotations: JSON.stringify([
        { id: "n1", content: "from an old bookmark", createdAt: 1, kind: "point", x: 1, y: 2 },
      ]),
    },
    // …and no host mark either, which is what "nothing to annotate" means once
    // the strip follows the TARGET rather than the layout: a host still holding
    // up a marked app frame is a target, folder url or not.
    () => null,
  );
  await settle();

  expect(annChips(r)).toHaveLength(0);
  expect(annotationsForTests()!.annotations).toHaveLength(0);
  // The seats are gone with the pane they act on — HIDDEN, not disabled.
  expect(byClass(r, "c-annbtn")).toHaveLength(0);
  // And the URL is left ALONE: deleting the param would break the bookmark's
  // round trip for the day that folder becomes an app.
  expect(params.get("annotations")).toContain("from an old bookmark");
});

// ---- the narrow view's disarm --------------------------------------------

test("arriving in the narrow CHAT view disarms the mode", async () => {
  // T:8940 — the toggle that would undo the mode is hidden in that view, and an
  // armed mode behind a hidden toggle keeps the frame's capture-phase click
  // swallower live over a document the user cannot see.
  const win = globalThis.window as unknown as Record<string, unknown>;
  const realMatch = win.matchMedia;
  win.matchMedia = () => ({
    matches: true,
    addEventListener() {},
    removeEventListener() {},
  });
  try {
    const { r, params } = await mountChat();
    // Boot lands on the chat view and must NOT disarm — a URL carrying an
    // explicit `annmode=1` belongs to the wide layout too.
    await act(async () => params.set({ paneview: "preview" }));
    await settle();
    await act(async () => commentSeat(r).props.onClick());
    await settle();
    expect(commentSeat(r).props["aria-pressed"]).toBe("true");

    await act(async () => params.set({ paneview: "chat" }));
    await settle();

    expect(commentSeat(r).props["aria-pressed"]).toBe("false");
    expect(rootClass(r)).not.toContain("annlock");
  } finally {
    win.matchMedia = realMatch;
  }
});

// ---- the send WINDOW: the capture is async, and it is one door ------------
//
// `beginSend` photographs the pane before the wire can be composed, so a send
// that carries notes is async before the controller has heard of it. Both tests
// below hold the shutter open by hand and act inside that window, which is
// where PR #1074's two send-path findings lived.

/** An `attachOverview` that will not answer until the test says so. */
function heldShutter(): () => void {
  let open!: () => void;
  const held = new Promise<void>((resolve) => {
    open = resolve;
  });
  patchApi({
    attachOverview: async () => {
      overviews += 1;
      await held;
      return { id: "ov1", kind: "overview", view: "/shots/overview.png" } satisfies Attachment;
    },
  });
  return open;
}

function typeInBox(r: Chat, value: string): Promise<void> {
  return act(async () => {
    r.root.findByType("textarea").props.onChange({ currentTarget: { value } });
  });
}

function pressEnterInBox(r: Chat): Promise<void> {
  return act(async () => {
    r.root
      .findByType("textarea")
      .props.onKeyDown({ key: "Enter", shiftKey: false, preventDefault() {} });
  });
}

async function armedWithANote(content = "this button is too small") {
  const rig = await mountChat();
  await act(async () => commentSeat(rig.r).props.onClick());
  await settle();
  await act(async () => makeNote(content));
  await settle();
  return rig;
}

test("two rapid submits are ONE send, and the delivered one keeps its notes", async () => {
  // Nothing used to hold the door while the overview was being taken: the
  // second Enter started a second `beginSend`, the controller refused ITS
  // `sendMessage` out loud, and that hand-back was read by the FIRST send —
  // already delivered — as its own failure. It unmarked the notes the agent had
  // been given and revoked their picture (Bugbot, PR #1074).
  const open = heldShutter();
  const { r } = await armedWithANote();
  await typeInBox(r, "have a look at this");

  await act(async () => {
    r.root.findByType("form").props.onSubmit({ preventDefault: () => {} });
  });
  // ...and the second press, INSIDE the first send's capture window.
  await pressEnterInBox(r);
  // Nothing has been dispatched yet — which is exactly why the window needs a
  // door of its own.
  expect(started()).toHaveLength(0);

  await act(async () => open());
  await settle(30);

  // ONE start, ONE picture, and the notes went with it: not handed back, not
  // revoked, and resolved by the run that ended cleanly.
  expect(started()).toHaveLength(1);
  expect(overviews).toBe(1);
  expect(revoked).toHaveLength(0);
  expect(annChips(r)).toHaveLength(0);
  expect(annotationsForTests()!.annotations).toHaveLength(0);
  const message = started()[0]!.params.message;
  expect(message).toContain("have a look at this");
  expect(message).toContain("this button is too small");
});

test("the typed words are in the transcript WHILE the picture is taken, and once", async () => {
  // The composer clears its box on the keystroke and the controller's own
  // bubble only goes up inside `sendMessage`, so for the width of the capture
  // the message was NOWHERE and read as dropped (Bugbot, PR #1074).
  const open = heldShutter();
  const { r } = await armedWithANote();
  await typeInBox(r, "look at the header");
  const bubbles = () => byClass(r, "bubble").map((n) => String(n.props.children));

  await act(async () => {
    r.root.findByType("form").props.onSubmit({ preventDefault: () => {} });
  });

  // Mid-capture: the box is empty, and the words are in the log.
  expect(r.root.findByType("textarea").props.value).toBe("");
  expect(bubbles()).toEqual(["look at the header"]);

  await act(async () => open());
  await settle(30);

  // The send's own bubble ADOPTED that row rather than adding a second.
  expect(bubbles()).toEqual(["look at the header"]);
  expect(started()).toHaveLength(1);
});

test("a send that never launched takes its optimistic bubble back down", async () => {
  startError = "no session";
  const { r } = await armedWithANote("this one came back");
  await typeInBox(r, "words with nothing behind them");
  await act(async () => {
    r.root.findByType("form").props.onSubmit({ preventDefault: () => {} });
  });
  await settle(30);

  // The controller drops the row it adopted, so a failed send leaves no bubble
  // pretending the agent was told anything.
  expect(byClass(r, "bubble")).toHaveLength(0);
  // And the round is pending again, exactly as the wordless road already was.
  expect(annChips(r)).toHaveLength(1);
  expect(annotationsForTests()!.annotations[0]!.sent).toBe(0);
});
