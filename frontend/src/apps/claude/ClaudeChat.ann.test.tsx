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
const { NAV_LOCKED_REASON } = await import("./ann");
const { isNativeOff, resetNativeOffForTests } = await import("./shots");
const { ATTACH_API } = await import("./ui/attachApi");
const { createMemoryParamsStore } = await import("./params/store");
const { resetAgentDirCacheForTests } = await import("./protocol/agent");
const { ANN_TAG, PANE_SHOT_TAG } = await import("./protocol/wire");
const { publishProjectQueueEnabled } = await import("./feature-flag");
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
/** Set by `heldStart()` — the `start` request, parked until the test says go. */
let holdStart: Promise<void> | null = null;

/** `/api/prefs` — the project queue's switch lives there (`queue.enabled`). */
let prefsBody: Record<string, unknown> = {};
/** Every body `/api/tasks/queue/admit` was asked with, and what it answers. The
 *  queue is the one road on which a send does not reach `/api/run` at all, so
 *  the ENTRY is where a queued round of notes has to be looked for. */
const admits: Array<Record<string, unknown>> = [];
let admitAnswer: Record<string, unknown> = { run: true };

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
    if (url === "/api/prefs") return jsonRes(prefsBody);
    if (url === "/api/tasks/queue/admit") {
      admits.push(JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>);
      return jsonRes(admitAnswer);
    }
    // A34's boot probe (`captureSources`) — what this machine can record, asked
    // without prompting for permission.
    if (url === "/api/capture") return jsonRes({ sources: audioSource });
    // The mic itself: `captureAudio`'s native road (no `sources.client`), which
    // is what lets a test record a real walkthrough — the marks are then the
    // recorder's own writes, stamped with its clock.
    if (url === "/api/capture/start") {
      return jsonRes({
        id: "cap1",
        mode: "audio",
        path: "/w/p/.fused/walkthrough.wav",
        state: "recording",
        seconds: 0,
        maxSeconds: 600,
        jobId: "job1",
      });
    }
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
        // HOLDABLE, so a test can act inside the window between "the controller
        // took the message" and "the run is live" — where `status` is still
        // idle and PR #1074's lost follow-up lived.
        if (holdStart) return holdStart.then(() => jsonRes({ ok: true, result: { run_id: "r1" } }));
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
  holdStart = null;
  prefsBody = {};
  admits.length = 0;
  admitAnswer = { run: true };
  // PROCESS-GLOBAL, like the native flag beside it: left on, it would admit
  // every send in every suite that mounts a chat after this one.
  publishProjectQueueEnabled(false);
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
  publishProjectQueueEnabled(false);
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

test("DELETING THE TASK disarms the mode before it navigates (P3-09, T:13371)", async () => {
  // T:13371-13375's own words: back-to-chats "REFUSES while a comment mode
  // holds the reader here (annNavLocked) — and a page must not stay on a
  // transcript that no longer exists". So the erase drops the mode FIRST, the
  // way its own discard path does. Without it, deleting the task left pins over
  // a pane whose conversation is gone and a nav lock refusing the very
  // navigation the delete was supposed to make.
  const { r, params } = await mountChat();
  await act(async () => commentSeat(r).props.onClick());
  await settle();
  makeNote("this row is wrong");
  await settle();
  expect(annotationsForTests()!.armed).toBe(true);
  expect(rootClass(r)).toContain("annlock");
  expect(params.get("annmode")).toBe("1");

  // The menu's erase, straight at the seam `Kebab` calls (the confirm and the
  // delete itself are `Kebab`'s own tests).
  const erased = r.root.findAll(
    (n) => typeof (n.props as { onErased?: unknown }).onErased === "function",
  )[0]!;
  await act(async () => (erased.props as { onErased(id: string): void }).onErased("s1"));
  await settle();

  // The mode is off, the lock with it, and the param says so — `set(false)` is
  // T's three lines (`annSetMode(false); annBusyHold = false; annNavLock()`).
  expect(annotationsForTests()!.armed).toBe(false);
  expect(rootClass(r)).not.toContain("annlock");
  expect(params.get("annmode")).toBe("0");
  // …and the navigation it was blocking actually happened: the landing is back.
  expect(byClass(r, "c-home")).toHaveLength(1);
});

test("the locked ← Chats SAYS WHY, in the title and in its accessible name", async () => {
  // T:6896 writes exactly this sentence onto `#back.title` while the lock holds
  // and clears it on unlock. It goes into the accessible NAME too, because
  // `disabled` takes the button out of tab order — a hover-only answer is no
  // answer for a control the keyboard can no longer land on, and a dead way out
  // that will not say why is the worst of the refusal faces.
  //
  // Back is only in the strip once there is a chat to leave, so this sends
  // first (the landing has no way back).
  const { r } = await mountChat();
  await typeInBox(r, "hello");
  await pressEnterInBox(r);
  await settle();

  const back = () => byClass(r, "c-back")[0]!;
  expect(back().props.disabled).toBe(false);
  expect(back().props.title).toBeUndefined();
  expect(back().props["aria-label"]).toBe("Back to chats");

  await act(async () => commentSeat(r).props.onClick());
  await settle();

  expect(back().props.disabled).toBe(true);
  expect(back().props.title).toBe(NAV_LOCKED_REASON);
  expect(String(back().props["aria-label"])).toContain(NAV_LOCKED_REASON);
  expect(String(back().props["aria-label"])).toContain("Back to chats");

  // …and it is given back, sentence and all, when the mode goes.
  await act(async () => commentSeat(r).props.onClick());
  await settle();
  expect(back().props.disabled).toBe(false);
  expect(back().props.title).toBeUndefined();
  expect(back().props["aria-label"]).toBe("Back to chats");
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
  // THE ATTACHMENT CHIP'S PILL, DOWN TO THE MARKUP (P3R1-2): one `.c-chip-door`
  // button carrying the glyph slot and the words, with `.c-chip-x` as its only
  // sibling — so `styles/composer.css`'s one chip block dresses both kinds and
  // the ✕ is the same borderless hover control on each.
  const door = chips[0]!.findAll(
    (n) => typeof n.type === "string" && n.props.className === "c-chip-door",
  );
  expect(door).toHaveLength(1);
  expect(door[0]!.type).toBe("button");
  expect(
    door[0]!.findAll((n) => typeof n.type === "string" && n.props.className === "c-pinlbl"),
  ).toHaveLength(1);
  expect(
    door[0]!.findAll((n) => typeof n.type === "string" && n.props.className === "c-txt"),
  ).toHaveLength(1);
  expect(door[0]!.props.title).toBe("make this bigger — click to edit");
  // The note's WORDS are in the accessible name too, not just the letter — an
  // `aria-label` replaces the name, and "Note A — click to edit" for every chip
  // in the row tells a reader nothing apart (Bugbot, PR #1074).
  expect(door[0]!.props["aria-label"]).toBe("Note A: make this bigger — click to edit");
  // Notes alone are sendable, with no words at all (T:17903) — which is what ✓
  // Done's whole gesture is.
  expect(r.root.findByType("form")).toBeTruthy();

  // The ✕ (T:7009) — the attachment chip's `.c-chip-x`, which is the pill both
  // kinds of chip now share down to the markup (P3R1-2).
  const drop = chips[0]!.findAll(
    (n) => typeof n.type === "string" && n.props.className === "c-chip-x",
  )[0]!;
  expect(drop.props["aria-label"]).toBe("Remove note A");
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

test("and it SAYS WHY the seat went, in the reason the probe gave", async () => {
  // T:7801 — `console.warn("spoken walkthroughs unavailable:", src.audio.reason
  // || "")`. The reason is CP-11's and it names a browser that CAN record, so a
  // reader with no seat has the console to go on. Swallowing it left "the
  // feature is missing" indistinguishable from "the feature is broken".
  const said: unknown[][] = [];
  const realWarn = console.warn;
  console.warn = (...a: unknown[]) => said.push(a);
  try {
    audioSource = { audio: { available: false, reason: "this browser cannot record" } };
    await mountChat();
    await settle();
    const line = said.find((a) => String(a[0]).includes("spoken walkthroughs unavailable"));
    expect(line).toBeDefined();
    expect(line![1]).toBe("this browser cannot record");

    // A refusal with no reason still says the seat went, with an empty tail
    // rather than "undefined".
    said.length = 0;
    audioSource = { audio: { available: false } };
    await mountChat();
    await settle();
    expect(
      said.find((a) => String(a[0]).includes("spoken walkthroughs unavailable")),
    ).toEqual(["spoken walkthroughs unavailable:", ""]);

    // And a machine that CAN record says nothing at all.
    said.length = 0;
    audioSource = { audio: { available: true, reason: null } };
    await mountChat();
    await settle();
    expect(
      said.some((a) => String(a[0]).includes("spoken walkthroughs unavailable")),
    ).toBe(false);
  } finally {
    console.warn = realWarn;
  }
});

test("the boot probe closes the native-still road, so an XO arm can prompt", async () => {
  // T:7840-7847 sets `shotNativeOff` from `screenshot.available === false` at
  // BOOT; native only ever learned it from a live 409. T:7688 then reads it on
  // the arm — `if (annOn && annXO && shotNativeOff) annXOStreamGet()` — which is
  // the one moment carrying the user activation `getDisplayMedia` needs. Gated
  // on a 409 that had not happened, that branch was false on the first arm and
  // the first cross-origin walkthrough silently produced no pictures.
  resetNativeOffForTests();
  audioSource = {
    audio: { available: true, reason: null },
    screenshot: { available: false, reason: "no still on this platform" },
  };
  await mountChat();
  await settle();
  expect(isNativeOff()).toBe(true);
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

    // The mode is off — and the SEAT ITSELF is gone (T:3822 `body.view-chat
    // #annbtn { display: none }`), which is the stronger half of the same rule:
    // the disarm-on-arrival covers only arriving, so absence is what stops a
    // FRESH arm afterwards, keyboard included.
    expect(byClass(r, "c-annbtn")).toHaveLength(0);
    expect(annotationsForTests()!.armed).toBe(false);
    expect(rootClass(r)).not.toContain("annlock");
  } finally {
    win.matchMedia = realMatch;
  }
});

test("the narrow CHAT view takes the Comment seat away, and Preview gives it back", async () => {
  // T:3822/3823 hide the two seats whose pane is parked off screen there — the
  // camera has nothing to photograph, Comment has nothing to pin onto — while
  // Annotate stays, because a spoken walkthrough is about the app the reader is
  // describing rather than about what is on screen at the moment.
  const win = globalThis.window as unknown as Record<string, unknown>;
  const realMatch = win.matchMedia;
  win.matchMedia = () => ({
    matches: true,
    addEventListener() {},
    removeEventListener() {},
  });
  try {
    const { r, params } = await mountChat();
    await act(async () => params.set({ paneview: "chat" }));
    await settle();
    expect(byClass(r, "c-annbtn")).toHaveLength(0);
    // THE CAMERA STAYS (visual pass 2, FIX-18): T:3823's own rule loses on
    // specificity and legacy renders `#viewshot` here — measured live, 106px at
    // a 736px pane — and the owner's rule is identical UX. The pane is parked,
    // not unmounted, so the picture it takes is real.
    expect(byClass(r, "c-viewshot")).toHaveLength(1);
    // The walkthrough seat is NOT taken away either.
    expect(byClass(r, "c-annrec").length).toBeGreaterThan(0);

    await act(async () => params.set({ paneview: "preview" }));
    await settle();
    expect(byClass(r, "c-annbtn")).toHaveLength(1);
    expect(byClass(r, "c-viewshot")).toHaveLength(1);
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

/** What is in the composer's box right now. */
function boxValue(r: Chat): string {
  return String(r.root.findByType("textarea").props.value ?? "");
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

// ---- the start window: taken, but not yet live ----------------------------

/** A `start` that will not answer until the test says so. */
function heldStart(): () => void {
  let open!: () => void;
  holdStart = new Promise<void>((resolve) => {
    open = resolve;
  });
  return open;
}

test("a line typed while the run is only STARTING stays in the box, and sends after", async () => {
  // `sendMessage` sets its own `sending` gate before its first await and holds
  // it for the whole turn, but the STATUS the composer routes on stays `idle`
  // until `pollLoop` reports — one `start` round-trip away. The door used to
  // open the moment the controller took the message, so a line typed inside
  // that window read as "no run yet": the composer sent it as a FRESH message,
  // the controller refused it out loud, and the refusal took the optimistic
  // bubble down with it. The words were nowhere (Bugbot, PR #1074).
  const open = heldStart();
  const { r } = await mountChat();
  await typeInBox(r, "first message");
  await act(async () => {
    r.root.findByType("form").props.onSubmit({ preventDefault: () => {} });
  });
  await settle();

  // The message is out and the run is NOT live yet: this is the window.
  expect(started()).toHaveLength(1);
  expect(boxValue(r)).toBe("");

  // A follow-up typed inside it. THE DOOR IS SHUT — in the submit handler,
  // which is where T shuts every one of them (T:4187 sets no `disabled` on this
  // button, ever): the run is not live, so this is not yet a follow-up the
  // controller could take.
  await typeInBox(r, "second message");
  const sendBtn = () =>
    r.root.findAll((n) => typeof n.type === "string" && n.props["aria-label"] === "Send")[0]!;
  expect(sendBtn().props.disabled).toBeUndefined();

  await pressEnterInBox(r);

  // Refused by the latch — so it costs the user nothing: the words are still in
  // the box, and no second `start` was spawned for the controller to refuse.
  expect(boxValue(r)).toBe("second message");
  expect(started()).toHaveLength(1);
  // ONE bubble, the first message's; the second is still a draft.
  expect(byClass(r, "bubble").map((n) => String(n.props.children))).toEqual(["first message"]);

  await act(async () => open());
  await settle(30);

  // The turn is over, the door is open, and the words that were held are still
  // there to send — never lost.
  expect(boxValue(r)).toBe("second message");
  await pressEnterInBox(r);
  await settle(30);
  expect(started()).toHaveLength(2);
  expect(started()[1]!.params.message).toContain("second message");
});

test("the run going live opens the door without waiting for the turn to end", async () => {
  // The latch cannot simply be held for the whole turn: a follow-up has to be
  // sendable inside one, and the composer routes it to `sendFollowUp` as soon
  // as the status says the run is live.
  const { r } = await mountChat();
  await typeInBox(r, "first message");
  await act(async () => {
    r.root.findByType("form").props.onSubmit({ preventDefault: () => {} });
  });
  await settle(30);
  // The run ran to its end, so the door is open and the box is free again.
  expect(started()).toHaveLength(1);
  await typeInBox(r, "a second one");
  await pressEnterInBox(r);
  await settle(30);
  expect(boxValue(r)).toBe("");
  expect(started()).toHaveLength(2);
});

// ---- a send during a walkthrough -----------------------------------------

/** The Send button (the composer's `.c-send`, which is a Stop while a run is
 *  live). */
function sendBtn(r: Chat) {
  return byClass(r, "c-send")[0]!;
}

/** The mic seat in the `#anncta` strip. */
function micSeat(r: Chat) {
  return byClass(r, "c-annrec")[0]!;
}

/**
 * A REAL WALKTHROUGH, RECORDING, with one wordless mark in it.
 *
 * The mic seat is pressed and the capture is the native road (`/api/capture/start`
 * above), so the mode machine calls this a recording exactly as it does in the
 * browser; the mark is then made by a CLICK in the framed app, which is the
 * recorder's own write — its id, its `t` stamp, no words until a transcript
 * lands.
 */
async function recordingWithAMark(r: Chat): Promise<void> {
  await act(async () => micSeat(r).props.onClick());
  await settle();
  await act(async () => clickInApp(BODY as unknown as Element));
  await settle();
}

test("Enter mid-walkthrough sends the typed words and NOT the wordless marks", async () => {
  // `hasAttachments` called a mark sendable as soon as it had a `t`, and
  // `beginSend` folds every pending sendable note in — while `annlock` only
  // greys the ways OUT of the chat, so the composer stays live. An Enter typed
  // during the walkthrough therefore uploaded still-wordless marks, and the
  // transcription then wrote words onto notes already stamped `sent` (Bugbot,
  // PR #1074).
  const { r } = await mountChat();
  await recordingWithAMark(r);
  expect(annotationsForTests()!.mode).toBe("recording");

  // The chip is there — it is a spot the reader clicked — but it is not a
  // MESSAGE yet: `isSendableNow` refuses a mark whose words are still coming.
  expect(annChips(r)).toHaveLength(1);
  expect(typeof annotationsForTests()!.annotations[0]!.t).toBe("number");
  // The Send BUTTON is not the assertion any more: T never disables it for
  // having nothing to send (T:2956-2981, FIX-10) and neither do we, so the
  // guarantee is proven by what a send CARRIES — which is the rest of this
  // test — rather than by the button's face. Pressing it with nothing but a
  // pending mark still sends nothing at all.
  expect(sendBtn(r).props.disabled).toBeUndefined();
  await act(async () => {
    r.root.findByType("form").props.onSubmit({ preventDefault: () => {} });
  });
  await settle(10);
  expect(started()).toHaveLength(0);

  // A line typed meanwhile is a normal thing to send, and it goes.
  await typeInBox(r, "while I am talking");
  await act(async () => {
    r.root.findByType("form").props.onSubmit({ preventDefault: () => {} });
  });
  await settle(30);

  expect(started()).toHaveLength(1);
  const message = started()[0]!.params.message;
  expect(message).toContain("while I am talking");
  // No annotations block, no picture, and the mark is still the walkthrough's:
  // pending, chipped, waiting on its words.
  expect(message).not.toContain("<" + ANN_TAG + ">");
  expect(overviews).toBe(0);
  expect(annChips(r)).toHaveLength(1);
  expect(annotationsForTests()!.annotations[0]!.sent).toBeFalsy();
});

test("the transcript's words make the mark sendable, walkthrough or no", async () => {
  const { r } = await mountChat();
  await recordingWithAMark(r);

  // What `assignWords` does when the transcription lands, and it lands while
  // the walkthrough still owns the mode — Transcribing… is where the
  // walkthrough's OWN auto-send fires from, so words have to be enough on
  // their own or that send would go out carrying nothing.
  await act(async () => {
    const ann = annotationsForTests()!;
    const mark = ann.annotations[0]!;
    ann.store.merge([{ ...mark, content: "this header is wrong" }]);
  });
  await settle();
  expect(sendBtn(r).props.disabled).toBeUndefined();

  await act(async () => {
    r.root.findByType("form").props.onSubmit({ preventDefault: () => {} });
  });
  await settle(30);

  const message = started()[0]!.params.message;
  expect(message).toContain("<" + ANN_TAG + ">");
  expect(message).toContain("this header is wrong");
  expect(annChips(r)).toHaveLength(0);
});

// ---- the project queue: a send the folder was too busy to take -------------
//
// A queued send never reaches `/api/run`. It becomes a scheduler entry that
// fires minutes later with WHATEVER IS WRITTEN ON IT — so everything the live
// wire composes has to be composed before the admission, or it is simply not in
// the message that eventually runs. The pictures already travelled
// (`carryForQueue`); the NOTES did not, and they stayed unmarked, which is the
// worse half: the next send into a free folder took somebody else's round
// (Bugbot, PR #1124).

/** The chat with the queue on, ready to admit. Published rather than left to
 *  the prefs read, so the switch cannot land a tick after the send. */
async function queuedChat(answer: Record<string, unknown>, content?: string) {
  prefsBody = { queue: { enabled: true } };
  admitAnswer = answer;
  const rig = content === undefined ? await armedWithANote() : await armedWithANote(content);
  // Inside `act`: the switch has subscribers on screen (`useProjectQueueEnabled`
  // through the schedule hook), so publishing it is a state update like any
  // other.
  await act(async () => publishProjectQueueEnabled(true));
  return rig;
}

test("a QUEUED send writes its notes ONTO the entry, and spends the round", async () => {
  const { r } = await queuedChat(
    {
      run: false,
      entry: { id: "q1" },
      key: "pending:q1",
      position: 2,
      ahead: "TASK-041",
      ahead_title: "Pull today's news",
    },
    "the header is wrong",
  );
  await typeInBox(r, "please fix this");
  await pressEnterInBox(r);
  await settle(30);

  // Nothing spawned — which is the whole point of the admission.
  expect(started()).toHaveLength(0);
  // …and the entry carries the message the live send would have carried: the
  // typed line and the annotations block, composed the one way
  // (`composeOutgoing`).
  expect(admits).toHaveLength(1);
  const message = String(admits[0]!.message);
  expect(message).toContain("please fix this");
  expect(message).toContain("<" + ANN_TAG + ">");
  expect(message).toContain("the header is wrong");
  // THE ROUND IS SPENT. Unmarked notes are notes the NEXT send takes — a queued
  // message's words arriving a second time, under somebody else's prompt.
  expect(annChips(r)).toHaveLength(0);
  expect(annotationsForTests()!.annotations.every((n) => !!n.sent)).toBe(true);
  // ONE picture, taken once: its copy is what the entry carries
  // (`carryForQueue` led with it), and the original's blob is put down rather
  // than pinned for the life of the document — nothing on screen draws it.
  expect(overviews).toBe(1);
  expect(revoked).toHaveLength(1);
  // The WAITING ROW is up, and it is the one thing on screen still saying the
  // words — the reader's own bubble, dashed, at its place in the transcript
  // (ui/Waiting), drawn from the admission until the next poll carries the
  // entry the server just created.
  expect(byClass(r, "c-waiting")).toHaveLength(1);
  expect(byClass(r, "c-waiting-bubble").map((n) => String(n.props.children))).toEqual([
    "please fix this",
  ]);
  // …and exactly ONE copy of it: the optimistic transcript row is dropped on the
  // same paint the waiting row goes up, so the words are never in two places.
  expect(byClass(r, "bubble")).toHaveLength(1);
});

test("an ADMITTED send takes its notes the ordinary way — once, and only in beginSend", async () => {
  // `run: true` is today's road byte for byte: the notes are still pending when
  // the verdict lands, `beginSend` takes them, and the capture the admission
  // paid for is put down (revoked) rather than double-stamping the round — the
  // same price `carryForQueue` pays for asking before spending.
  const { r } = await queuedChat({ run: true }, "this button is too small");
  await typeInBox(r, "have a look at this");
  await pressEnterInBox(r);
  await settle(30);

  expect(admits).toHaveLength(1);
  expect(started()).toHaveLength(1);
  const message = started()[0]!.params.message;
  expect(message).toContain("have a look at this");
  expect(message).toContain("<" + ANN_TAG + ">");
  expect(message).toContain("this button is too small");
  // ONE round of notes on the wire, never two.
  expect(message.split("<" + ANN_TAG + ">")).toHaveLength(2);
  // The admission's own capture is the only thing spent for nothing.
  expect(overviews).toBe(2);
  expect(revoked).toHaveLength(1);
  expect(annChips(r)).toHaveLength(0);
});

test("a REFUSED admission leaves the round exactly where the reader left it", async () => {
  // The queue would not take the message, so nothing was sent — and nothing may
  // be spent either: the chips stay, the notes stay pending, and the words go
  // back in the box. Only the capture is put down, because a picture of a pane
  // that has moved on is no use to the retry.
  const { r } = await queuedChat({ nope: true }, "this row is wrong");
  await typeInBox(r, "words that did not go");
  await pressEnterInBox(r);
  await settle(30);

  expect(started()).toHaveLength(0);
  expect(byClass(r, "c-waiting")).toHaveLength(0);
  expect(boxValue(r)).toBe("words that did not go");
  expect(annChips(r)).toHaveLength(1);
  expect(annotationsForTests()!.annotations[0]!.sent).toBeFalsy();
  expect(revoked).toHaveLength(1);
});
