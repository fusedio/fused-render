// THE LANDING'S MOVE, EXERCISED THROUGH THE REAL COMPONENT (bug report,
// 2026-09-15, on top of Bugbot #1166).
//
// `ui/home-lists.test.tsx` proves `Lists` calls `onFillDraft(task)` on a
// draft row's press; it stubs `onFillDraft` itself, so it cannot see what
// `ClaudeChat.onFillDraft` actually DOES with that call — which is where all
// four bugs here lived: a second and third press on the same row before it
// left the list minted a second and third draft (bug 5), a move JOINED the
// row's words onto whatever the box already held instead of replacing it
// (bug 4), the destination's own tray was dropped rather than replaced
// (Bugbot #1166's "attachments"), and an older write already on the wire from
// this box's own keystrokes could land after the move's and undo it (Bugbot
// #1166's "an older PUT lands after and overwrites"). All four are wiring
// between this box's own autosave and a write from OUTSIDE it, which is
// exactly what a stubbed `onFillDraft` cannot show — so this file mounts the
// real component over a stubbed `fetch`, the same harness `ClaudeChat.boot.
// test.tsx` and `ClaudeChat.attach.test.tsx` use for the same reason.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create } from "react-test-renderer";

const { ClaudeChat } = await import("./ClaudeChat");
const { createMemoryParamsStore } = await import("./params/store");
const { resetAgentDirCacheForTests } = await import("./protocol/agent");
const { resetListingFeedForTests } = await import("@shell/tasksPulse");
type Task = import("@platform/lib/api").Task;

// `DEST_FILE`/`DEST_KEY` are fixed — `baseProps` below closes over `DEST_FILE`
// once, at module load, so ClaudeChat always mounts on this same file. `SRC_
// KEY` is MUTABLE and set fresh at the top of every test instead (never
// `const`): `spent` in `@platform/lib/drafts` is MODULE STATE that outlives
// any one test in this `bun test` process, so a source key one test's move
// genuinely deletes must not be the same key a LATER test reads a draft back
// from — the second test would see it "spent" from the first and read an
// empty draft, exactly as a stale GET does for the real bug this module's
// `spent` set exists to close.
const DEST_FILE = "/w/p";
const DEST_KEY = "new:/w/p";
let SRC_KEY = "new:/w/p/other.py";

/** The one foreign chat draft row the fixtures draw — same shape
 *  `home-lists.test.tsx`'s own `chatDraft()` uses. */
function srcDraftTask(over: Partial<Task> = {}): Task {
  // `taskInPane` (the Recent list's own narrowing, `useRecentTasks.ts`) keeps
  // a folder pane's rows by `project`, so the fixture's `target`/`file` are
  // derived off `SRC_KEY` rather than fixed — a hardcoded path here matched
  // only the FIRST test to run in this file and left the second reading an
  // empty list.
  const srcFile = SRC_KEY.slice("new:".length);
  return {
    key: SRC_KEY,
    task_id: "TASK-900",
    kind: "draft",
    state: "draft",
    draft_kind: "chat",
    draft_id: "",
    project: DEST_FILE,
    target: srcFile,
    file: srcFile,
    session_id: "",
    title: "the other file's draft",
    title_source: "draft",
    description: "",
    status: "draft",
    unread: 0,
    message_count: 0,
    draft: { preview: "words from the other file", updated_at: Date.now() / 1000, kind: "chat" },
    last_active: Date.now() / 1000,
    ...over,
  } as unknown as Task;
}

interface Call {
  method: string;
  url: string;
  body: unknown;
}
const calls: Call[] = [];
const putsTo = (url: string) => calls.filter((c) => c.method === "PUT" && c.url === url);
const deletesTo = (url: string) => calls.filter((c) => c.method === "DELETE" && c.url === url);

/** A PUT to `DEST_KEY` can be held open, so a test can prove the move waits
 *  for it (Bugbot #1166) rather than racing it. */
let holdDestPut = false;
let releaseDestPut: () => void = () => {};

const realFetch = globalThis.fetch;

function jsonRes(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

function stubFetch(): void {
  (globalThis as { fetch: unknown }).fetch = async (
    input: unknown,
    init?: { method?: string; body?: unknown },
  ): Promise<Response> => {
    const url = decodeURIComponent(
      String(typeof input === "string" ? input : (input as { url: string }).url),
    );
    const method = init?.method ?? "GET";
    if (url.startsWith("/api/fs/stat")) {
      return jsonRes({
        path: DEST_FILE,
        is_dir: true,
        templates: [{ mode: "claude", path: `${DEST_FILE}/.claude/template.html` }],
      });
    }
    // The landing's long poll — parked, exactly as the boot suite's own.
    if (url.startsWith("/api/tasks/changes")) return new Promise<Response>(() => {});
    if (url === "/api/tasks") return jsonRes({ tasks: [srcDraftTask()] });
    if (url === "/api/schedule") return jsonRes({ entries: [] });
    if (url === "/api/prefs") return jsonRes({});
    // THE SOURCE'S WHOLE RECORD (`draftContentOf`, Bugbot #1166): the row
    // only carries a clipped preview, so a move reads the record here first.
    if (url === "/api/drafts") {
      return jsonRes({
        chat: {
          [SRC_KEY]: {
            text: "the whole sentence from the other file",
            attachments: [],
            updated_at: 1,
          },
        },
        task: {},
      });
    }
    if (url === `/api/drafts/chat/${DEST_KEY}` && method === "PUT") {
      const body = JSON.parse(String(init?.body ?? "{}"));
      calls.push({ method, url, body });
      if (holdDestPut && putsTo(`/api/drafts/chat/${DEST_KEY}`).length === 1) {
        return new Promise<Response>((res) => {
          releaseDestPut = () => res(jsonRes({}));
        });
      }
      return jsonRes({});
    }
    if (url === `/api/drafts/chat/${SRC_KEY}` && method === "DELETE") {
      calls.push({ method, url, body: undefined });
      return jsonRes({});
    }
    return jsonRes({});
  };
}

beforeEach(() => {
  calls.length = 0;
  holdDestPut = false;
  releaseDestPut = () => {};
  resetAgentDirCacheForTests();
  resetListingFeedForTests();
  stubFetch();
});

const mounted: Array<ReturnType<typeof create>> = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  (globalThis as { fetch: unknown }).fetch = realFetch;
});

const baseProps = {
  file: DEST_FILE,
  chatOnly: true,
  compact: false,
  peek: false,
  autoFocus: false,
} as const;

async function mountChat() {
  const params = createMemoryParamsStore();
  let r!: ReturnType<typeof create>;
  await act(async () => {
    r = create(<ClaudeChat {...baseProps} params={params} />);
  });
  mounted.push(r);
  await settle(300);
  return r;
}

async function settle(ms = 0): Promise<void> {
  await act(async () => {
    await new Promise((done) => setTimeout(done, ms));
  });
}

/** The one `.tasks-row` the fixtures draw (`ScheduleTaskViews.TaskRowItem`),
 *  same helper `ui/home-lists.test.tsx` uses. */
function taskRow(r: ReturnType<typeof create>) {
  return r.root.find(
    (n) =>
      typeof n.type === "string" &&
      /(^| )tasks-row( |$)/.test(
        String((n.props as { className?: string }).className || ""),
      ),
  );
}

function box(r: ReturnType<typeof create>) {
  return r.root.findByType("textarea");
}

test("bug 5: pressing the same draft row three times moves it exactly once", async () => {
  SRC_KEY = "new:/w/p/other-5.py";
  const r = await mountChat();
  const press = () => (taskRow(r).props as { onClick(): void }).onClick();
  await act(async () => {
    // ALL THREE IN ONE TURN, before the first press's own async work has had a
    // chance to run at all — the shape the bug report described ("2-3 times").
    // `movingKeys` has to refuse the second and third SYNCHRONOUSLY, before
    // `draftContentOf`'s first `await`, or all three would already be past the
    // guard by the time any of them resolves.
    press();
    press();
    press();
    await new Promise((done) => setTimeout(done, 30));
  });
  // Exactly one PUT, one DELETE — the coordinator's own words for this bug.
  // (The row itself is gone from the list once `discardDraft` drops it, which
  // is what actually stops a REAL fourth click — this guard's own job is the
  // window before that update ever reaches the list.)
  expect(putsTo(`/api/drafts/chat/${DEST_KEY}`).length).toBe(1);
  expect(deletesTo(`/api/drafts/chat/${SRC_KEY}`).length).toBe(1);
});

test("bug 4 + Bugbot #1166: a move settles this box's own in-flight write first, then replaces (never joins)", async () => {
  SRC_KEY = "new:/w/p/other-4.py";
  const r = await mountChat();
  // TYPE FIRST, so the box's own ordinary autosave has something to write —
  // the "older PUT" Bugbot #1166 named.
  await act(async () => {
    box(r).props.onChange({ currentTarget: { value: "existing words" } });
  });
  holdDestPut = true;
  // Past the 600ms debounce: the box's own write is now dispatched and HELD —
  // in flight, exactly as a keystroke a moment before a press would leave it.
  await settle(650);
  expect(putsTo(`/api/drafts/chat/${DEST_KEY}`).length).toBe(1);

  const press = () => (taskRow(r).props as { onClick(): void }).onClick();
  await act(async () => {
    press();
    // Long enough for `draftContentOf` and everything up to `await
    // destAutosave.settle()` to run, nowhere near long enough for a real
    // debounce — if the move's own PUT fired without waiting, it would show
    // up here.
    await new Promise((done) => setTimeout(done, 30));
  });
  expect(putsTo(`/api/drafts/chat/${DEST_KEY}`).length).toBe(1);

  // RELEASE THE OLD WRITE. Only now may the move's own PUT go out.
  await act(async () => {
    releaseDestPut();
    await new Promise((done) => setTimeout(done, 30));
  });
  const puts = putsTo(`/api/drafts/chat/${DEST_KEY}`);
  expect(puts.length).toBe(2);
  // REPLACES, NEVER JOINS (bug report, 2026-09-15): the second PUT carries
  // exactly the source's words, not "existing words" folded in front of them.
  expect(puts[1]!.body).toEqual({
    text: "the whole sentence from the other file",
    attachments: [],
  });
  expect(box(r).props.value).toBe("the whole sentence from the other file");
  expect(deletesTo(`/api/drafts/chat/${SRC_KEY}`).length).toBe(1);
});
