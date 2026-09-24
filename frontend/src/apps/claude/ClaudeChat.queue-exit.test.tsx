// DELETING THE ONLY QUEUED MESSAGE OF AN EMPTY CHAT LEAVES THE CHAT.
//
// With the project queue on, a send into a busy folder is not run: it is
// admitted as a scheduler entry and drawn as a dashed bubble under the log. A
// BRAND-NEW conversation whose only content is that one message is, the moment
// the message is cancelled, a conversation with nothing in it — and the pane
// used to stay up over an empty transcript, so the reader was told their
// message was gone and left standing in front of the hole it came out of
// (Akshil, 2026-09-19).
//
// The two halves are asserted here because neither is visible anywhere else:
// the decision reads the CONTROLLER's state (turns, inbox, a run in flight) and
// the ROWS on screen, and only a real mount holds both at once. `sched/waiting`
// owns the rule itself (`emptyAfterDrop`, unit-tested there); this file is about
// the wiring — that it is asked after the cancel answered, that it spends
// `onBack`, and that a chat with history is left exactly where it was.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create } from "react-test-renderer";

const { ClaudeChat } = await import("./ClaudeChat");
const { createMemoryParamsStore } = await import("./params/store");
const { resetAgentDirCacheForTests } = await import("./protocol/agent");
const { publishProjectQueueEnabled } = await import("./chat-prefs");

// ---- the server, cut down to what a queued chat touches ---------------------

/** Every `POST /api/schedule/cancel` body, so a test can see the press reached
 *  the one endpoint the Tasks page's cancel also spends. */
const cancels: Array<Record<string, unknown>> = [];
/** What that cancel answers: a rejection is the road on which nothing may
 *  navigate. */
let cancelFails = false;
/** A gate the cancel answer waits behind, so a test can land something in the
 *  chat WHILE the request is in flight (Bugbot, PR #1228). */
let cancelHold: Promise<void> = Promise.resolve();
let releaseCancel: () => void = () => {};
/** The turns `GET /api/claude-sessions/history` hands back — the difference
 *  between an empty chat and one with something in it. */
let historyTurns: Array<Record<string, unknown>> = [];
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
        templates: [{ mode: "claude", path: "/w/p/.claude/agent.py" }],
      });
    }
    if (url === "/api/prefs") return jsonRes({ queue: { enabled: true } });
    if (url === "/api/tasks/queue/admit") return jsonRes(admitAnswer);
    if (url === "/api/schedule/cancel") {
      cancels.push(JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>);
      await cancelHold;
      if (cancelFails) return { ok: false, status: 500, json: async () => ({}) } as Response;
      return jsonRes({ entry: { id: "q1", state: "cancelled" } });
    }
    if (url.startsWith("/api/claude-sessions/history")) {
      return jsonRes({ turns: historyTurns, session_id: "s9" });
    }
    // The landing's long poll holds the request open until something changes; a
    // stub that answers it is an infinite re-arm inside `act`.
    if (url.startsWith("/api/tasks/changes")) return new Promise<Response>(() => {});
    if (url === "/api/tasks") return jsonRes({ tasks: [] });
    if (url === "/api/run") {
      const body = JSON.parse(String(init?.body ?? "{}")) as {
        py: string;
        params: Record<string, string>;
      };
      // An ordinary folder: no app entry, so no pane and nothing to annotate.
      if (String(body.py).endsWith("/app.py")) return jsonRes({ ok: true, result: { entry: "" } });
      return jsonRes({ ok: true, result: {} });
    }
    return jsonRes({});
  };
}

const mounted: Array<ReturnType<typeof create>> = [];

beforeEach(() => {
  cancelHold = Promise.resolve();
  releaseCancel = () => {};
  cancels.length = 0;
  cancelFails = false;
  historyTurns = [];
  admitAnswer = { run: true };
  resetAgentDirCacheForTests();
  stubFetch();
  publishProjectQueueEnabled(false);
});

afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  // PROCESS-GLOBAL: left on it would admit every send in every suite that
  // mounts a chat after this one.
  publishProjectQueueEnabled(false);
  (globalThis as { fetch: unknown }).fetch = realFetch;
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

type Chat = ReturnType<typeof create>;

async function mountChat(seed: Record<string, string> = {}): Promise<Chat> {
  const params = createMemoryParamsStore();
  params.set(seed);
  let r!: Chat;
  await act(async () => {
    r = create(<ClaudeChat {...baseProps} params={params} />);
  });
  mounted.push(r);
  // Two settles: the agent-dir stat, then the pane's own `app.py` decision.
  await settle();
  await settle();
  // The switch has subscribers on screen, so publishing it is a state update.
  await act(async () => publishProjectQueueEnabled(true));
  return r;
}

function byClass(r: Chat, cls: string) {
  return r.root.findAll(
    (n) =>
      typeof n.type === "string" &&
      String((n.props as { className?: string }).className ?? "")
        .split(" ")
        .includes(cls),
  );
}

/** Is the pane showing a conversation, or the Claude home? `← Chats` is drawn
 *  by the chat view and by nothing else (`inChat`). */
function inChat(r: Chat): boolean {
  return r.root.findAll((n) => n.type === "button" && String(n.props.children).includes("Chats"))
    .length > 0;
}

async function sendQueued(r: Chat, text: string, entryId = "q1"): Promise<void> {
  admitAnswer = {
    run: false,
    entry: { id: entryId, due: new Date().toISOString() },
    key: "pending:" + entryId,
    position: 2,
    ahead: "TASK-041",
    ahead_title: "Pull today's news",
  };
  await act(async () => {
    r.root.findByType("textarea").props.onChange({ currentTarget: { value: text } });
  });
  await act(async () => {
    r.root
      .findByType("textarea")
      .props.onKeyDown({ key: "Enter", shiftKey: false, preventDefault() {} });
  });
  await settle(30);
}

/** The `delete` under the nth waiting bubble. */
async function pressDelete(r: Chat, nth = 0): Promise<void> {
  await act(async () => {
    byClass(r, "c-waiting-del")[nth]!.props.onClick();
  });
  await settle(30);
}

// ---- an empty chat --------------------------------------------------------

test("delete of the only queued message in an empty chat navigates home", async () => {
  const r = await mountChat();
  await sendQueued(r, "say hi");
  // The chat is open, on one dashed bubble and nothing else: no session, no
  // transcript, no run — the whole conversation is the message in the line.
  expect(inChat(r)).toBe(true);
  expect(byClass(r, "c-waiting")).toHaveLength(1);

  await pressDelete(r);

  // The entry was cancelled through the one endpoint that cancels entries…
  expect(cancels).toHaveLength(1);
  expect(cancels[0]!.id).toBe("q1");
  // …and the pane went with it, back to the Claude home — the same door
  // `← Chats` opens, which is why the landing's composer is what is left.
  expect(inChat(r)).toBe(false);
  expect(byClass(r, "c-waiting")).toHaveLength(0);
});

test("a cancel that FAILED navigates nowhere", async () => {
  // Nothing may leave before the server has agreed the message is gone: a pane
  // that had already left would be the reader told their message is dropped
  // when it is still in the line.
  const r = await mountChat();
  await sendQueued(r, "say hi");
  cancelFails = true;

  await pressDelete(r);

  expect(cancels).toHaveLength(1);
  expect(inChat(r)).toBe(true);
  expect(byClass(r, "c-waiting")).toHaveLength(1);
});

test("a SECOND queued message still waiting keeps the chat open", async () => {
  const r = await mountChat();
  await sendQueued(r, "say hi", "q1");
  await sendQueued(r, "and then say bye", "q2");
  expect(byClass(r, "c-waiting")).toHaveLength(2);

  await pressDelete(r, 0);

  expect(cancels).toHaveLength(1);
  // One message of this chat's is still in the folder's line, so the chat is
  // still about something.
  expect(inChat(r)).toBe(true);
  expect(byClass(r, "c-waiting")).toHaveLength(1);
});

test("a second message queued WHILE the cancel is in flight keeps the chat open", async () => {
  // Bugbot, PR #1228: the rows were read off the render that created the
  // callback, so a listing that landed during the request was invisible and
  // the pane left a chat that still had a message in the line.
  const r = await mountChat();
  await sendQueued(r, "say hi", "q1");
  cancelHold = new Promise<void>((res) => {
    releaseCancel = res;
  });
  await pressDelete(r, 0);
  expect(cancels).toHaveLength(1);
  // …and while the server has not answered, the reader queues another one.
  await sendQueued(r, "and then say bye", "q2");
  expect(byClass(r, "c-waiting")).toHaveLength(2);
  releaseCancel();
  await settle(30);
  expect(inChat(r)).toBe(true);
  expect(byClass(r, "c-waiting")).toHaveLength(1);
});

// ---- a chat with history --------------------------------------------------

test("delete in a chat with history stays", async () => {
  historyTurns = [
    { role: "user", text: "what is a ring buffer", uuid: "u1" },
    { role: "assistant", text: "a fixed-size queue.", uuid: "a1" },
  ];
  const r = await mountChat({ session_id: "s9" });
  await settle(30);
  // The transcript really is on screen — without this the case below would pass
  // for the wrong reason (an empty chat that simply failed to navigate).
  expect(
    byClass(r, "bubble").map((n) => String((n.props as { children?: unknown }).children)),
  ).toContain("what is a ring buffer");
  await sendQueued(r, "now write it up");
  expect(inChat(r)).toBe(true);
  expect(byClass(r, "c-waiting")).toHaveLength(1);

  await pressDelete(r);

  expect(cancels).toHaveLength(1);
  // The transcript is the conversation; dropping one unsent message off the end
  // of it changes nothing about where the reader is.
  expect(inChat(r)).toBe(true);
  expect(byClass(r, "c-waiting")).toHaveLength(0);
});
