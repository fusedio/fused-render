// THE WIRING THE TRAY'S OWN SUITE CANNOT SEE (inventory 03 §C/§D).
//
// `ui/attach.test.tsx` proves the tray's rules with the pipeline replaced, and
// it does it by re-implementing the four gesture handlers — so the handlers
// ClaudeChat actually installs were the one part of PR2 with no coverage at all,
// and three of the review's MAJORs lived in exactly that gap: the blocks spread,
// `onDiscardShot`'s identity match and the dragleave counter. Two of QA round
// 1's unresolved anomalies were there too — a paste that looked like it made two
// chips, and a `.dropping` ring that never appeared.
//
// So this file mounts the REAL component over a stubbed `fetch` and a patched
// `ATTACH_API`, and drives the props it hands out. Everything is asserted
// through what reaches `/api/run` or what the chip row renders, because that is
// where getting any of it wrong actually lands.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create } from "react-test-renderer";

const { ClaudeChat } = await import("./ClaudeChat");
const { ShotViewer } = await import("./ui/ShotViewer");
const { ATTACH_API } = await import("./ui/attachApi");
const { createMemoryParamsStore } = await import("./params/store");
const { resetAgentDirCacheForTests } = await import("./protocol/agent");
const { PANE_SHOT_TAG } = await import("./protocol/wire");
const { inFlightSizeForTests } = await import("./ClaudeChat");
type Attachment = import("./shots/types").Attachment;
type AttachApi = import("./ui/attachApi").AttachApi;
type Viewable = import("./ui/attachApi").Viewable;

// ---- the server, cut down to the three endpoints a booting chat touches -----

interface RunCall {
  action: string;
  params: Record<string, string>;
}
const runs: RunCall[] = [];
/** The `start` reply: an `{error}` is the road that hands the pictures back. */
let startError = "";
/** Held so a test can be INSIDE a send — the window `inFlight` exists for. */
let holdStart = false;

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
    // The landing's Recent list long-polls this forever; answering it instantly
    // spins the loop inside `act` and the test never returns. Held open, which
    // is what the real endpoint does.
    if (url.startsWith("/api/tasks/changes")) return new Promise<Response>(() => {});
    if (url === "/api/run") {
      const body = JSON.parse(String(init?.body ?? "{}")) as {
        py: string;
        params: Record<string, string>;
      };
      const action = String(body.params?.action ?? "");
      runs.push({ action, params: body.params ?? {} });
      if (action === "start") {
        if (holdStart) return new Promise<Response>(() => {});
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

// ---- the pipeline, patched IN PLACE ---------------------------------------
// `ATTACH_API` is the single object both `ClaudeChat` and `useAttachments` reach
// through at call time, so patching its members is what stands in for injecting
// a whole fake — and the members are put back after every test, because `bun
// test` shares one process.

/** Every path handed to `addPaths`, so a test can watch the drop road. */
let pathIds = 0;

const API = ATTACH_API as unknown as Record<string, unknown>;

/**
 * PATCHED PER TEST, AND PUT BACK PER TEST. Not from a snapshot taken at module
 * load: `bun test` shares one process, so at that moment `ATTACH_API` is
 * whatever the file that ran before this one left behind — `ui/attach.test.tsx`
 * injects a whole fake and never touches the singleton, but nothing makes that
 * permanent. And `Object.assign(ATTACH_API, REAL)` could not have removed a key
 * the patch ADDED, only overwrite the ones it already had.
 *
 * So each key is snapshotted the first time it is patched, WITH whether it was
 * there at all, and the restore deletes the ones that were not.
 */
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

/** The singleton as this test found it, asserted intact in `afterEach`. */
let asFound: Record<string, unknown> = {};

beforeEach(() => {
  runs.length = 0;
  startError = "";
  holdStart = false;
  pathIds = 0;
  asFound = { ...API };
  resetAgentDirCacheForTests();
  stubFetch();
  patchApi({
    flash: () => () => {},
    // One pasted picture per paste, and it lands with a real path so the chip is
    // an ordinary attached image rather than a refusal.
    filesFromPaste: (ev) =>
      (ev as { clipboardData?: unknown }).clipboardData ? [{ name: "shot.png" } as File] : [],
    attachFiles: async function* (_dir, files) {
      for (const f of files) {
        yield {
          id: "f" + ++pathIds,
          kind: "image",
          view: "/shots/" + (f.name || "x"),
          name: f.name,
        } satisfies Attachment;
      }
    },
    // THE SAME PATH, TWICE, with different ids — the reachable shape of the
    // identity bug: a real-path drag repeated has nothing but the id to tell the
    // two chips apart.
    attachPaths: () => [
      {
        id: "p" + ++pathIds,
        kind: "file",
        view: "/x/README.md",
        name: pathIds === 1 ? "first" : "second",
        brought: true,
      } satisfies Attachment,
    ],
    attachPane: async () => ({ id: "pane1", kind: "pane", seat: "pane", view: "/shots/v.png" }),
    readDirs: () => ["/shots"],
    readDirsFor: () => ["/shots"],
    revoke: () => {},
    // A drag carries an attachment when it has a DataTransfer at all — the shape
    // of the real reader, and the shape the dragleave guard used to fail on.
    dragHasAttachment: (dt) => !!dt,
    pathsFromDrop: () => ["/x/README.md"],
  });
});

const mounted: Array<ReturnType<typeof create>> = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  (globalThis as { fetch: unknown }).fetch = realFetch;
  restoreApi();
  // THE RESTORE IS ASSERTED, not assumed: a patch left behind is a failure in
  // whichever suite runs next, which is the hardest kind of test failure to read.
  expect(Object.keys(API).sort()).toEqual(Object.keys(asFound).sort());
  for (const k of Object.keys(asFound)) expect(API[k]).toBe(asFound[k]);
});

const baseProps = {
  file: "/w/p",
  // The sidebar surface: no pane, which is where QA exercised paste and drop and
  // where the tray has to work with no camera at all.
  chatOnly: true,
  compact: false,
  peek: false,
  autoFocus: false,
} as const;

async function settle(ms = 0): Promise<void> {
  await act(async () => {
    await new Promise((done) => setTimeout(done, ms));
  });
}

async function mountChat(extra: Record<string, unknown> = {}) {
  const params = createMemoryParamsStore();
  let r!: ReturnType<typeof create>;
  await act(async () => {
    r = create(<ClaudeChat {...baseProps} params={params} {...extra} />);
  });
  mounted.push(r);
  await settle();
  return r;
}

type Chat = Awaited<ReturnType<typeof mountChat>>;

/** Every attachment chip on screen, in row order. */
function chips(r: Chat) {
  return r.root.findAll(
    (n) =>
      typeof n.type === "string" &&
      String((n.props as { className?: string }).className ?? "").includes("c-shotchip"),
  );
}

/** Every `<img>` a thumbnail button is showing — the chips' while a message is
 *  being composed, the receipt rows' once it has been sent (ShotThumb). */
function thumbSrcs(r: Chat): string[] {
  return r.root
    .findAllByType("img")
    .map((n) => String((n.props as { src?: string }).src ?? ""))
    .filter(Boolean);
}

/** What one chip says it is (`.c-txt`). */
function chipText(chip: ReturnType<typeof chips>[number]): string {
  const txt = chip.findAll(
    (n) =>
      typeof n.type === "string" &&
      String((n.props as { className?: string }).className ?? "") === "c-txt",
  )[0];
  return String((txt?.props as { children?: unknown })?.children ?? "");
}

/** The chat COLUMN — the element the four drag listeners are on (T:11745). */
function column(r: Chat) {
  return r.root.findAll(
    (n) =>
      typeof n.type === "string" &&
      String((n.props as { className?: string }).className ?? "").startsWith("c-chat"),
  )[0]!;
}

function dragEv(withData = true) {
  return {
    dataTransfer: withData
      ? ({ types: ["Files"], files: [], dropEffect: "" } as unknown as DataTransfer)
      : undefined,
    preventDefault: () => {},
  } as unknown as React.DragEvent;
}

const started = () => runs.filter((c) => c.action === "start");


// ---- QA anomaly 4: one paste, one chip ------------------------------------

test("ONE paste makes ONE chip, and only a clipboard with files is taken", async () => {
  // QA round 1 saw a single dispatched `paste` produce two chips and could not
  // reproduce it. There is exactly one listener to find — the textarea's — and
  // this is what pins that: a second handler anywhere (the column, the root)
  // would show up here as a second chip.
  const r = await mountChat();
  const box = r.root.findByType("textarea");
  let prevented = 0;
  await act(async () => {
    box.props.onPaste({ clipboardData: {}, preventDefault: () => (prevented += 1) });
  });
  await settle();
  expect(chips(r)).toHaveLength(1);
  expect(prevented).toBe(1);

  // And an ordinary paste of WORDS is not stolen from the box it lands in
  // (T:11719-11728): no chip, and no `preventDefault`.
  await act(async () => {
    box.props.onPaste({ clipboardData: null, preventDefault: () => (prevented += 1) });
  });
  await settle();
  expect(chips(r)).toHaveLength(1);
  expect(prevented).toBe(1);
});

// ---- QA anomaly 5 / review MAJOR: the .dropping ring's depth counter -------

test("the .dropping ring follows a DEPTH counter, and a bare dragleave still counts", async () => {
  const r = await mountChat();
  const cls = () => String((column(r).props as { className: string }).className);
  expect(cls()).not.toContain("dropping");

  // dragenter/dragleave fire for every child the pointer crosses, so the class
  // is driven by a counter rather than toggled — a plain toggle flickers the
  // ring off the moment the cursor moves over a chip (T:11745-11751).
  await act(async () => column(r).props.onDragEnter(dragEv()));
  expect(cls()).toContain("dropping");
  await act(async () => column(r).props.onDragEnter(dragEv()));

  // THE REGRESSION THIS PINS: several engines expose no `types` at all on
  // `dragleave`, and the handler used to early-return on that — so the depth
  // never came back down and the ring stuck until the next drop. Both leaves
  // here carry no DataTransfer whatsoever.
  await act(async () => column(r).props.onDragLeave(dragEv(false)));
  expect(cls()).toContain("dropping");
  await act(async () => column(r).props.onDragLeave(dragEv(false)));
  expect(cls()).not.toContain("dropping");

  // The counter floors at zero: a stray leave cannot put the ring into a state
  // the next enter has to dig out of.
  await act(async () => column(r).props.onDragLeave(dragEv(false)));
  await act(async () => column(r).props.onDragEnter(dragEv()));
  expect(cls()).toContain("dropping");

  // A drop RESETS it, because a drop delivers no leave for the enters before it.
  await act(async () => column(r).props.onDrop(dragEv()));
  await settle();
  expect(cls()).not.toContain("dropping");
});

// ---- review MAJOR: Discard matches by id ----------------------------------

test("Discard removes the chip it was opened from, not its twin", async () => {
  const r = await mountChat();
  // The same real path dragged in twice: same kind, same `view`, and every
  // refusal in the tray has `view: null` too — so the old `view === shot.view &&
  // kind === shot.kind` match could not tell two chips apart at all.
  await act(async () => column(r).props.onDrop(dragEv()));
  await settle();
  await act(async () => column(r).props.onDrop(dragEv()));
  await settle();
  expect(chips(r).map(chipText)).toEqual(["first", "second"]);

  // `onDiscardShot` is the viewer's, and the viewer is a portalled dialog no
  // `react-test-renderer` tree can enter — so it is driven through the prop the
  // chat hands it, which is the very function under test.
  const viewer = r.root.findByType(ShotViewer);
  const second: Viewable = { id: "p2", kind: "file", view: "/x/README.md", pending: true };
  await act(async () => viewer.props.onDiscard?.(second));
  await settle();
  expect(chips(r).map(chipText)).toEqual(["first"]);

  // And a shot the tray no longer holds removes nothing, rather than the first
  // thing that happens to share its kind.
  await act(async () =>
    viewer.props.onDiscard?.({ id: "gone", kind: "file", view: "/x/README.md", pending: true }),
  );
  await settle();
  expect(chips(r).map(chipText)).toEqual(["first"]);
});

// ---- review MAJOR: the send seam ------------------------------------------

test("a send carries the tray's block and its Read rules, and empties the tray", async () => {
  const r = await mountChat();
  const box = r.root.findByType("textarea");
  await act(async () => {
    box.props.onPaste({ clipboardData: {}, preventDefault: () => {} });
  });
  await settle();
  expect(chips(r)).toHaveLength(1);

  // The form's submit is the real send road; `hasAttachments` is what lets a
  // picture go out with no words at all (T:17903).
  await act(async () => {
    r.root.findByType("form").props.onSubmit({ preventDefault: () => {} });
  });
  await settle(20);

  expect(started()).toHaveLength(1);
  expect(started()[0]!.params.message).toContain("<" + PANE_SHOT_TAG + ">");
  expect(started()[0]!.params.message).toContain("/shots/shot.png");
  // Granted for the SESSION, not the turn (T:16657-16668).
  expect(JSON.parse(started()[0]!.params.read_dirs)).toEqual(["/shots"]);
  // Emptied by the same call that read it, so a second Enter cannot send the
  // same picture twice (T:16532).
  expect(chips(r)).toHaveLength(0);
});

test("nothing is sent while a chip is still attaching", async () => {
  // `hasAttachments` counts the in-flight placeholder but `take()` leaves it in
  // the tray, so this submit used to launch an EMPTY send — the words (if any)
  // going out without the files they were written about (Bugbot, PR #1064).
  let release = (): void => {};
  const landed = new Promise<void>((done) => {
    release = done;
  });
  patchApi({
    attachFiles: async function* (_dir, files) {
      await landed;
      for (const f of files) {
        yield {
          id: "f" + ++pathIds,
          kind: "image",
          view: "/shots/" + (f.name || "x"),
          name: f.name,
        } satisfies Attachment;
      }
    },
  });
  const r = await mountChat();
  const box = r.root.findByType("textarea");
  const send = () => r.root.findByProps({ className: "c-send" });
  await act(async () => {
    box.props.onPaste({ clipboardData: {}, preventDefault: () => {} });
  });
  await settle();
  // A chip the user can see, and a Send they cannot press.
  expect(chips(r)).toHaveLength(1);
  expect(send().props.disabled).toBe(true);

  await act(async () => {
    r.root.findByType("form").props.onSubmit({ preventDefault: () => {} });
  });
  await settle(20);
  expect(started()).toHaveLength(0);
  // Still in the tray, waiting for the message it belongs to.
  expect(chips(r)).toHaveLength(1);

  // The bytes land ⇒ the door opens, and the picture rides the send.
  release();
  await settle(20);
  expect(send().props.disabled).toBe(false);
  await act(async () => {
    r.root.findByType("form").props.onSubmit({ preventDefault: () => {} });
  });
  await settle(20);
  expect(started()).toHaveLength(1);
  expect(started()[0]!.params.message).toContain("<" + PANE_SHOT_TAG + ">");
  expect(chips(r)).toHaveLength(0);
});

test("a send that LANDED lets go of its pictures", async () => {
  // The other half of the `inFlight` map, and the half with no symptom: the
  // entry is keyed by the `Receipt[]` the controller was handed, and on the road
  // that landed nothing ever comes looking for it — `onSendReturned` only fires
  // when the send did NOT launch. Never deleted, the map keeps every receipt row
  // and every Attachment (with its blob URL) the page has ever sent alive for as
  // long as the chat is open.
  const r = await mountChat();
  const box = r.root.findByType("textarea");
  await act(async () => {
    box.props.onPaste({ clipboardData: {}, preventDefault: () => {} });
  });
  await settle();
  expect(chips(r)).toHaveLength(1);
  expect(inFlightSizeForTests()).toBe(0);

  await act(async () => {
    r.root.findByType("form").props.onSubmit({ preventDefault: () => {} });
  });
  await settle(20);

  // It went out, the tray is empty — and so is the bookkeeping.
  expect(started()).toHaveLength(1);
  expect(started()[0]!.params.message).toContain("<" + PANE_SHOT_TAG + ">");
  expect(chips(r)).toHaveLength(0);
  expect(inFlightSizeForTests()).toBe(0);
});

test("a send that never launched hands the very pictures it took back", async () => {
  // The `inFlight` map is keyed by the `Receipt[]` the controller is handed, and
  // the merge now BUILDS that array (it may hold two owners' rows) — so if the
  // key and the array ever drift, `onSendReturned` finds nothing and the user's
  // picture is gone for good. This is that key's test.
  startError = "no session";
  const r = await mountChat();
  const box = r.root.findByType("textarea");
  await act(async () => {
    box.props.onPaste({ clipboardData: {}, preventDefault: () => {} });
  });
  await settle();

  await act(async () => {
    r.root.findByType("form").props.onSubmit({ preventDefault: () => {} });
  });
  await settle(20);

  expect(started()).toHaveLength(1);
  // Back in the tray, and never revoked on this road — those very thumbnails are
  // what the returned chip shows (T:16693-16720).
  expect(chips(r)).toHaveLength(1);
  expect(chipText(chips(r)[0]!)).toContain("shot.png");
});

// ---- Bugbot: the camera's window is part of the send gate ------------------

/**
 * A HOST'S CONTENT FRAME, which is what gives this chat-only mount a camera at
 * all (`annotateTarget`; ClaudeChat's `appFrame`). Only the two properties
 * `frameIsCrossOrigin` and the capture path read off it —
 * PLUS A LISTENER SURFACE, because PR3's annotation coordinator subscribes to
 * this frame's `load` to re-attach after a boot navigation (`ann/target.ts`
 * `watch`). A stub without it does not fail the annotation feature under test
 * here; it throws out of the mount effect and takes the whole chat with it.
 */
function hostFrame(): () => HTMLIFrameElement | null {
  const frame = {
    isConnected: true,
    contentDocument: {},
    contentWindow: { document: {}, location: { href: "http://localhost/render" } },
    addEventListener: () => {},
    removeEventListener: () => {},
  } as unknown as HTMLIFrameElement;
  return () => frame;
}

test("a send fired DURING the camera's window waits for the picture", async () => {
  // `capture()` plants NOTHING in the tray until the bytes are in hand — the
  // seat swap is the whole of its commit — so `attachPending`, which read the
  // chips, could not see the camera's window at all. The flash had already
  // fired, so the picture looked taken; an Enter in that window went out
  // WITHOUT it and it then landed in the tray for the NEXT message.
  //
  // This is also the flip D7 is about: `attachPending` reads `attach.capturing`,
  // which moves while the tray does not, so the `card` memo has to list it.
  let release = () => {};
  const landed = new Promise<void>((done) => {
    release = done;
  });
  patchApi({
    attachPane: async () => {
      await landed;
      return { id: "pane1", kind: "pane", seat: "pane", view: "/shots/v.png" } as Attachment;
    },
  });
  const r = await mountChat({ annotateTarget: hostFrame() });
  const box = r.root.findByType("textarea");
  const send = () => r.root.findByProps({ className: "c-send" });
  // Words, so the send would otherwise be perfectly sendable on its own.
  await act(async () => box.props.onChange({ currentTarget: { value: "what is this" } }));
  await settle();
  expect(send().props.disabled).toBe(false);

  // The shutter opens…
  await act(async () => r.root.findByProps({ className: "c-viewshot" }).props.onClick());
  await settle();
  // …and no chip exists yet, which is exactly why the gate could not see it.
  expect(chips(r)).toHaveLength(0);
  expect(send().props.disabled).toBe(true);
  await act(async () => {
    r.root.findByType("form").props.onSubmit({ preventDefault: () => {} });
  });
  await settle(20);
  expect(started()).toHaveLength(0);

  // The bytes land ⇒ the chip appears, the door opens, and the picture rides
  // the message it was taken for.
  release();
  await settle(20);
  expect(chips(r)).toHaveLength(1);
  expect(send().props.disabled).toBe(false);
  await act(async () => {
    r.root.findByType("form").props.onSubmit({ preventDefault: () => {} });
  });
  await settle(20);
  expect(started()).toHaveLength(1);
  expect(started()[0]!.params.message).toContain("<" + PANE_SHOT_TAG + ">");
});

// ---- Bugbot: the blobs a send is still carrying ---------------------------

test("a send that LANDED re-points its receipts at the copy on disk and drops the blobs", async () => {
  // The half of the same leak with no symptom at all. `done()` used to just
  // DELETE the entry, and the receipts under the sent bubble were drawn with the
  // attachment's own object URL — so the bubble held the only handle to a
  // full-pane Blob, and `newChat`, a file change or a later unmount threw those
  // turns away with the blobs still pinned for the life of the document.
  //
  // The bytes are on disk at `view` by then, so the rows move to `rawUrl(view)`
  // — the very URL a RESTORED turn is drawn with — and only then is the handle
  // released. Both halves are asserted: a revoke with the row left on `blob:`
  // would be a broken picture, and a rewrite with no revoke would be the leak.
  //
  // AND THE ORDER IS THE WHOLE FIX. `settleAttachments` is a store write, not a
  // render: the first cut revoked in that same tick, so the <img> under the
  // bubble was still showing the object URL when it stopped resolving — the img
  // errored and `ShotRow` read that as the pruner having deleted the file, so
  // every successful send of a pasted picture ended in "screenshot no longer on
  // disk" (Bugbot, PR #1064). `srcsAtRevoke` is what the rows were ACTUALLY
  // drawn with at the moment the handle went.
  const revoked: string[] = [];
  const srcsAtRevoke: string[][] = [];
  let chat: Chat | null = null;
  patchApi({
    revoke: (att: Attachment | null | undefined) => {
      if (att) revoked.push(att.id);
      if (chat) srcsAtRevoke.push(thumbSrcs(chat));
    },
    // A PASTED PICTURE WITH PIXELS: `attachFile`'s drawable road mints an object
    // URL for the thumbnail and saves the bytes under `view` (shots/attach.ts).
    attachFiles: async function* (_dir, files) {
      for (const f of files) {
        yield {
          id: "f" + ++pathIds,
          kind: "image",
          view: "/shots/" + (f.name || "x"),
          name: f.name,
          thumb: "blob:fused/shot-" + pathIds,
        } satisfies Attachment;
      }
    },
  });
  const r = await mountChat();
  chat = r;
  const box = r.root.findByType("textarea");
  await act(async () => {
    box.props.onPaste({ clipboardData: {}, preventDefault: () => {} });
  });
  await settle();
  expect(chips(r)).toHaveLength(1);
  // The chip is showing the blob, which is what makes the handle worth keeping
  // until the send is over.
  expect(thumbSrcs(r)).toEqual(["blob:fused/shot-1"]);

  await act(async () => {
    r.root.findByType("form").props.onSubmit({ preventDefault: () => {} });
  });
  await settle(20);

  expect(started()).toHaveLength(1);
  expect(chips(r)).toHaveLength(0);
  expect(inFlightSizeForTests()).toBe(0);
  // THE RECEIPT ROW under the sent bubble, on the restored turn's own road.
  const onDisk = "/api/fs/raw?path=" + encodeURIComponent("/shots/shot.png");
  expect(thumbSrcs(r)).toEqual([onDisk]);
  expect(revoked).toEqual(["f1"]);
  // …and the row was already SHOWING it when the handle was released, which is
  // the difference between a receipt that keeps its picture and one that says
  // the screenshot is gone.
  expect(srcsAtRevoke).toEqual([[onDisk]]);
  // …and nothing is left to revoke twice when the chat closes.
  revoked.length = 0;
  await act(() => {
    r.unmount();
  });
  expect(revoked).toEqual([]);
});

test("a send that never launched keeps its blob thumbnails alive", async () => {
  // The settle is gated on the entry STILL BEING THERE, because that is what
  // says the send went out. `onSendReturned` removed it first on this road and
  // the chips the user is looking at are those very thumbnails, so a settle here
  // would revoke the picture out from under the tray.
  startError = "no session";
  const revoked: string[] = [];
  patchApi({
    revoke: (att: Attachment | null | undefined) => {
      if (att) revoked.push(att.id);
    },
    attachFiles: async function* (_dir, files) {
      for (const f of files) {
        yield {
          id: "f" + ++pathIds,
          kind: "image",
          view: "/shots/" + (f.name || "x"),
          name: f.name,
          thumb: "blob:fused/shot-" + pathIds,
        } satisfies Attachment;
      }
    },
  });
  const r = await mountChat();
  const box = r.root.findByType("textarea");
  await act(async () => {
    box.props.onPaste({ clipboardData: {}, preventDefault: () => {} });
  });
  await settle();
  await act(async () => {
    r.root.findByType("form").props.onSubmit({ preventDefault: () => {} });
  });
  await settle(20);

  expect(chips(r)).toHaveLength(1);
  expect(thumbSrcs(r)).toEqual(["blob:fused/shot-1"]);
  expect(revoked).toEqual([]);
});

test("closing a chat mid-send revokes the pictures that send is carrying", async () => {
  // `take()` moves them OUT of the tray and into `inFlight`, so the tray's own
  // unmount revoke (which walks its live list) cannot see them — and
  // `attachBack`'s cleanup nulls `giveBack`, so the hand-back cannot reach them
  // either. Nothing was left holding a full-pane Blob's only handle.
  const revoked: string[] = [];
  patchApi({
    revoke: (att: Attachment | null | undefined) => {
      if (att) revoked.push(att.id);
    },
  });
  holdStart = true;
  const r = await mountChat();
  const box = r.root.findByType("textarea");
  await act(async () => {
    box.props.onPaste({ clipboardData: {}, preventDefault: () => {} });
  });
  await settle();
  expect(chips(r)).toHaveLength(1);

  await act(async () => {
    r.root.findByType("form").props.onSubmit({ preventDefault: () => {} });
  });
  await settle(20);
  // Out of the tray and into the send, which is parked: this is the window.
  expect(chips(r)).toHaveLength(0);
  expect(inFlightSizeForTests()).toBe(1);
  expect(revoked).toEqual([]);

  await act(() => {
    r.unmount();
  });
  expect(revoked).toEqual(["f1"]);
  expect(inFlightSizeForTests()).toBe(0);
});
