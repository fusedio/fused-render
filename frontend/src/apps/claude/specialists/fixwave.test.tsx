// The fix wave on the task side (docs/specialists R1, C3, C5): protocol never
// shows, the summary draft as its own block, one chip per breakout, a
// readable guidance sheet, product words for the five tools, the Breakout
// menu's keys / Suggested / pre-filled brief, and the services card's button
// inside the card's own row.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";

import { publishSpecialistsEnabled, resetSpecialistsFlagForTests } from "@platform/lib/specialists-flag";
import {
  addSummaryDraft,
  readSummaryDraft,
  resetSpecialistBreakouts,
  resetSummaryDraftsForTests,
} from "@platform/lib/specialist-breakouts";
import type { TaskEvent } from "@platform/lib/specialists-api";
import type { Turn } from "../protocol/controller-api";
import { BreakoutMenu, briefFromTask, folderMatches } from "./BreakoutMenu";
import {
  extractBlock,
  parseSeed,
  parseSummary,
  protocolKind,
  protocolTurnView,
  revertWhat,
  sendDraftParts,
} from "./protocol-view";
import { ServiceCardExtra } from "./ServiceCardExtra";
import { latestPerBreakout, ReceiptSheet, TaskEventChip, viewFromText } from "./TaskEventChips";
import { draftFields, SummaryDraftBlock, useSpecialistTaskSide, type TaskSide } from "./TaskSide";
import { specialistToolLabel } from "./tool-labels";
import { setSpecialistListForTests } from "./useTaskEvents";

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  resetSpecialistBreakouts();
  resetSummaryDraftsForTests();
});
function render(el: React.ReactElement) {
  let r!: ReactTestRenderer;
  act(() => {
    r = create(el);
  });
  mounted.push(r);
  return r;
}
const textOf = (n: ReactTestInstance | string | null): string =>
  n === null ? "" : typeof n === "string" ? n : n.children.map((c) => textOf(c as ReactTestInstance | string)).join("");
const button = (r: ReactTestRenderer, label: string) => {
  const found = r.root.findAll((n) => n.type === "button" && textOf(n).trim() === label);
  if (!found.length) throw new Error(`no button ${label}: ${r.root.findAll((n) => n.type === "button").map(textOf).join(" | ")}`);
  return found[0]!;
};
const flush = () =>
  act(async () => {
    await new Promise((res) => setTimeout(res, 0));
  });

const SEED = `Brief from the task "Publish Q4 canvas":
Problem: Sync stops at step 3 of 5.
It happens on every publish.
Tried: Retry, fresh sign-in
Look at: ~/q4/sync.log

The task is still running in /w/q4 and keeps working on everything else. Its transcript is /root/.claude/projects/x.jsonl; you may read it.`;
const CLOSE_REPLY = `<<<SUMMARY
Outcome: workaround — reopen then publish
Found: Sync stalls since yesterday's update.
It also happens on a new canvas.
Do next: Reopen the canvas, then publish.
Files changed: None
SUMMARY>>>
<<<NOTE
kind: suspected-bug
basis: verified
claim: Sync stalls at step 3.
entry: m-0001
NOTE>>>`;
const u = (text: string, key = "u1") => ({ role: "user", key, text }) as unknown as Turn;
const a = (text: string, key = "a1", over: Record<string, unknown> = {}) => ({ role: "assistant", key, text, ...over }) as unknown as Turn;
function rendered(node: React.ReactNode) {
  return render(<>{node}</>);
}

describe("protocol never shows (R1, C3)", () => {
  test("each app-written message is recognised by its fixed prefix", () => {
    expect(protocolKind(u(SEED))).toBe("seed");
    expect(protocolKind(u("The user is closing this breakout. Reply with exactly two blocks"))).toBe("close-request");
    expect(protocolKind(u("The user is closing this breakout again. Summarise only"))).toBe("close-request");
    expect(protocolKind(a(CLOSE_REPLY))).toBe("close-reply");
    expect(protocolKind(u('Undo this change from the latest update: "Prefer staging"'))).toBe("revert");
    expect(protocolKind(u("Review and send this draft to Ana through Slack (@ana). Show me"))).toBe("send-draft");
    expect(protocolKind(u("An ordinary message"))).toBeNull();
    expect(protocolKind(a("An ordinary reply"))).toBeNull();
  });

  test("the seed is a brief box: Problem / Tried / Look at, and none of its instructions", () => {
    const p = parseSeed(SEED);
    expect(p.title).toBe("Publish Q4 canvas");
    expect(p.brief).toEqual({
      problem: "Sync stops at step 3 of 5.\nIt happens on every publish.",
      tried: "Retry, fresh sign-in",
      look_at: "~/q4/sync.log",
    });
    const r = rendered(protocolTurnView(u(SEED)));
    const t = textOf(r.root);
    expect(t).toContain("Brief from Publish Q4 canvas");
    expect(t).toContain("Sync stops at step 3 of 5.");
    expect(t).not.toContain("transcript");
    expect(t).not.toContain("/root/.claude");
  });

  test("the breakout record's brief wins over the seed's text", () => {
    const r = rendered(protocolTurnView(u(SEED), { brief: { problem: "From the record", tried: "", look_at: "" } }));
    expect(textOf(r.root)).toContain("From the record");
  });

  test("the close-out request draws nothing", () => {
    expect(protocolTurnView(u("The user is closing this breakout. Reply with exactly two blocks"))).toBeNull();
  });

  test("the close-out reply is a tidy summary; the NOTE block and its markers never show", () => {
    expect(extractBlock(CLOSE_REPLY, "SUMMARY")!.open).toBe(false);
    const fields = parseSummary(extractBlock(CLOSE_REPLY, "SUMMARY")!.body);
    expect(fields.map((f) => f.label)).toEqual(["Outcome", "Found", "Do next", "Files changed"]);
    expect(fields[1]!.text).toBe("Sync stalls since yesterday's update.\nIt also happens on a new canvas.");
    const t = textOf(rendered(protocolTurnView(a(CLOSE_REPLY))).root);
    expect(t).toContain("Outcome");
    expect(t).toContain("Reopen the canvas, then publish.");
    for (const bad of ["<<<", ">>>", "NOTE", "m-0001", "basis", "suspected-bug"]) expect(t).not.toContain(bad);
  });

  test("a close-out reply still streaming says it is writing the summary", () => {
    const t = textOf(rendered(protocolTurnView(a("<<<SUMMARY\nOutcome: sol", "a2", { streaming: true }))).root);
    expect(t).toBe("Writing the summary…");
  });

  test("a revert request is a quiet line naming the change", () => {
    const msg =
      'Undo this change from the latest update: "Prefer staging before live" (Learned). In memory.md the line - [m-0003] … becomes …. Change only that.';
    expect(revertWhat(msg)).toBe("Prefer staging before live");
    const t = textOf(rendered(protocolTurnView(u(msg))).root);
    expect(t).toBe("Asked to undo: Prefer staging before live");
    // The older server's wording is read the same way.
    expect(revertWhat('Revert last night\'s change: Learned, "Use staging" (nightly update r1, commit abc)')).toBe("Use staging");
  });

  test("a send-draft request is 'Review and send: <subject>' with the text quoted", () => {
    const msg =
      "Review and send this draft to Ana through Slack (@ana). Show me the exact text first, then send it with the service tool; I will approve the send.\n\nSubject: Sync stalls on publish\n\nHi Ana, sync stalls at step 3.";
    expect(sendDraftParts(msg)).toEqual({ to: "Ana", subject: "Sync stalls on publish", body: "Hi Ana, sync stalls at step 3." });
    const r = rendered(protocolTurnView(u(msg)));
    expect(textOf(r.root)).toBe("Review and send: Sync stalls on publishHi Ana, sync stalls at step 3.");
    expect(r.root.findAll((n) => n.type === "blockquote")).toHaveLength(1);
  });

  test("inside a specialist session the chat gets replaceTurn; a task does not", () => {
    publishSpecialistsEnabled(true);
    let side: TaskSide | null = null;
    function Probe(p: { specialist?: boolean }) {
      side = useSpecialistTaskSide({
        claude: true,
        sessionId: "bs1",
        file: "/h",
        turns: [],
        ...(p.specialist ? { specialist: { id: "c1", kind: "breakout" as const, name: "Canvas" } } : {}),
      });
      return null;
    }
    render(<Probe specialist />);
    expect(side!.replaceTurn).toBeDefined();
    expect(side!.replaceTurn!(u("hello"))).toBeUndefined();
    expect(side!.replaceTurn!(u("The user is closing this breakout."))).toBeNull();
    render(<Probe />);
    expect(side!.replaceTurn).toBeUndefined();
    resetSpecialistsFlagForTests();
  });
});

describe("the summary draft, its own block above the composer", () => {
  function Host(p: { submit: (t: string) => boolean }) {
    const side = useSpecialistTaskSide({ claude: true, sessionId: "s1", file: "/w/proj", turns: [], submit: p.submit });
    return <>{side.dock}</>;
  }
  test("labelled, laid out, Send to the task sends through the composer (typed words first) and clears it", async () => {
    publishSpecialistsEnabled(true);
    setSpecialistListForTests([]);
    addSummaryDraft("s1", "/w/proj", {
      text: "Outcome: solved\nFound: a bug\nDo next: publish\nFiles changed: None\nFull session: Canvas breakout: sync",
      label: "Draft from the Canvas specialist · not sent yet",
      name: "Canvas",
      at: 1,
    });
    const sent: string[] = [];
    const r = render(<Host submit={(t) => (sent.push(t), true)} />);
    const t = textOf(r.root);
    expect(t).toContain("Draft from the Canvas specialist · not sent yet");
    for (const label of ["Outcome", "Found", "Do next", "Files changed", "Full session"]) expect(t).toContain(label);
    expect(() => button(r, "Clear draft")).not.toThrow();
    await act(async () => {
      button(r, "Send to the task").props.onClick();
    });
    expect(sent).toEqual(["Outcome: solved\nFound: a bug\nDo next: publish\nFiles changed: None\nFull session: Canvas breakout: sync"]);
    expect(readSummaryDraft("s1", "/w/proj")).toHaveLength(0);
    expect(textOf(r.root)).not.toContain("Draft from");
    resetSpecialistsFlagForTests();
  });

  test("a send the composer refuses keeps the draft and says so; Clear draft drops it", async () => {
    publishSpecialistsEnabled(true);
    setSpecialistListForTests([]);
    addSummaryDraft("s1", "/w/proj", { text: "Outcome: solved", label: "L", name: "Canvas", at: 1 });
    const r = render(<Host submit={() => false} />);
    await act(async () => {
      button(r, "Send to the task").props.onClick();
    });
    expect(readSummaryDraft("s1", "/w/proj")).toHaveLength(1);
    expect(textOf(r.root)).toContain("can't send right now");
    await act(async () => {
      button(r, "Clear draft").props.onClick();
    });
    expect(readSummaryDraft("s1", "/w/proj")).toHaveLength(0);
    resetSpecialistsFlagForTests();
  });
});

describe("polish: the summary draft fits the narrow pane", () => {
  const first = { text: "Outcome: first try\nFound: old finding\nDo next: old step", label: "L", name: "Tiles", at: 1 };
  const second = { text: "Found: a long finding\nFiles changed: None\nDo next: check the caller\nOutcome: reported", label: "L", name: "Tiles", at: 2 };

  test("Outcome and Do next come first", () => {
    expect(draftFields(second.text).map((f) => f.label)).toEqual(["Outcome", "Do next", "Found", "Files changed"]);
  });

  test("capped at 40% of the chat pane, newest summary on top, the earlier one behind Earlier summary", () => {
    let r!: ReactTestRenderer;
    const pane = { clientHeight: 500 };
    act(() => {
      r = create(<SummaryDraftBlock parts={[first, second]} onClear={() => {}} onSend={() => {}} />, {
        createNodeMock: (el) => (el.type === "section" ? { closest: (sel: string) => (sel === ".c-chat" ? pane : null) } : null),
      });
    });
    mounted.push(r);
    const section = r.root.find((n) => n.type === "section");
    expect(section.props.style).toEqual({ maxHeight: "200px" });
    const body = r.root.find((n) => n.props.className === "spx-draft-body");
    // the newest summary's rows are the first thing in the body, Outcome first
    expect(textOf(body).startsWith("Outcomereported")).toBe(true);
    const earlier = r.root.findAll((n) => n.type === "details");
    expect(earlier).toHaveLength(1);
    expect(textOf(earlier[0]!.find((n) => n.type === "summary"))).toBe("Earlier summary");
    expect(textOf(earlier[0]!)).toContain("first try");
  });
});

describe("chips", () => {
  const ev = (over: Partial<TaskEvent>): TaskEvent => ({ kind: "pull", at: 0, specialist: { id: "c1", name: "Canvas" }, ...over });
  test("one chip per breakout: its latest state", () => {
    const evs = [
      ev({ kind: "breakout_open", breakout_id: "b-1", at: 1 }),
      ev({ kind: "pull", at: 2 }),
      ev({ kind: "breakout_closed", breakout_id: "b-1", at: 3 }),
      ev({ kind: "breakout_discarded", breakout_id: "b-1", at: 4 }),
      ev({ kind: "breakout_open", breakout_id: "b-2", at: 5 }),
    ];
    expect(latestPerBreakout(evs).map((e) => `${e.kind}:${e.breakout_id ?? ""}`)).toEqual([
      "pull:",
      "breakout_discarded:b-1",
      "breakout_open:b-2",
    ]);
  });

  test("View: a readable sheet from the receipt's view, never the raw payload", async () => {
    const r = render(
      <TaskEventChip
        ev={ev({ receipt_id: "r-1", entries: 1, skills: 1 })}
        host={{ parentSessionId: "s1", parentFile: "/w", refresh: () => {} }}
        receipt={async () => ({
          receipt: {},
          text: "[fused:specialist-guidance receipt=r-1] - [m-0001] skip None {basis=user-said; seen=0}",
          view: {
            memory: [{ id: "m-0001", text: "mean() skips None values", basis: "user-said" }],
            said: [{ text: "Never divide by len+1" }],
            knowledge: [{ title: "Sync failures" }],
            skills: [{ name: "clean-data", description: "Drop None first" }],
          },
        })}
      />,
    );
    await act(async () => {
      button(r, "View").props.onClick();
    });
    const t = textOf(r.root);
    for (const s of ["From memory", "mean() skips None values", "You said", "Topic knowledge", "Skills", "clean-data"]) expect(t).toContain(s);
    for (const s of ["[fused:", "receipt=", "m-0001", "{basis", "seen="]) expect(t).not.toContain(s);
  });

  test("an older server's text is cleaned of ids and braces", () => {
    expect(viewFromText("[fused:x receipt=r]\n- [m-0001] Skip None values {basis=user-said; seen=0}")).toEqual({
      memory: [{ text: "Skip None values" }],
    });
    const r = render(<ReceiptSheet view={{}} />);
    expect(textOf(r.root)).toContain("Nothing from memory matched");
  });

  test("Reopen of a deleted specialist says so and offers nothing more", async () => {
    const r = render(
      <TaskEventChip
        ev={ev({ kind: "breakout_closed", breakout_id: "b-1" })}
        host={{ parentSessionId: "s1", parentFile: "/w", refresh: () => {} }}
        reopen={async () => Promise.reject(Object.assign(new Error("gone"), { status: 410, body: { reason: "deleted" } }))}
        records={async () => ({ breakouts: [] })}
      />,
    );
    await act(async () => {
      button(r, "Reopen").props.onClick();
    });
    expect(textOf(r.root)).toContain("was deleted");
    expect(r.root.findAll((n) => n.type === "button")).toHaveLength(0);
  });
});

describe("tool labels", () => {
  test("the five tools read as product text", () => {
    setSpecialistListForTests([{ id: "c1", name: "Canvas", subject: "", face: "", status: "", working: false }]);
    expect(specialistToolLabel("mcp__fused_render__specialist_check", { specialist: "c1", stage: "plan" })).toBe(
      "Asking the Canvas specialist to review the plan…",
    );
    expect(specialistToolLabel("mcp__fused_render__specialist_check", { specialist: "Canvas", stage: "done" }, { done: true })).toBe(
      "Asked the Canvas specialist to review the result",
    );
    expect(specialistToolLabel("mcp__fused_render__specialist_pull", { specialist: "c1" })).toBe(
      "Getting guidance from the Canvas specialist…",
    );
    expect(specialistToolLabel("mcp__fused_render__specialist_note", { specialist: "Canvas" })).toBe(
      "Sending a note to the Canvas specialist…",
    );
    expect(specialistToolLabel("mcp__fused_render__specialist_note", {})).toBe("Sending a note to the general inbox…");
    expect(specialistToolLabel("mcp__fused_render__specialist_breakout", { specialist: "c1" })).toBe(
      "Offering a breakout to the Canvas specialist…",
    );
    expect(specialistToolLabel("mcp__fused_render__specialist_list", {})).toBe("Listing specialists…");
    // The status line knows only the detail; an unknown id is never shown.
    expect(specialistToolLabel("mcp__fused_render__specialist_pull", null, { key: "c1" })).toBe(
      "Getting guidance from the Canvas specialist…",
    );
    expect(specialistToolLabel("mcp__fused_render__specialist_pull", null, { key: "6b557f6a" })).toBe(
      "Getting guidance from a specialist…",
    );
    expect(specialistToolLabel("Bash", {})).toBeNull();
  });
});

describe("the Breakout menu", () => {
  const SPECS = [
    { id: "c1", name: "Canvas", subject: "", face: "", status: "", working: false },
    { id: "d2", name: "Data", subject: "", face: "", status: "", working: false },
  ];
  const host = { parentSessionId: "s1", parentFile: "/w/q4" };

  test("the brief is pre-filled from the task: last request and last error, trimmed", () => {
    const turns = [
      u("Old ask", "u0"),
      u("Publish the Q4 canvas to staging", "u1"),
      {
        role: "assistant",
        key: "a1",
        text: "",
        segments: [
          { kind: "tool", id: "t1", name: "Bash", input: { command: "fused publish q4" }, status: "error", output: "\nError: sync stopped at step 3\nmore", images: [] },
        ],
      } as unknown as Turn,
    ];
    expect(briefFromTask(turns)).toEqual({
      problem: "Publish the Q4 canvas to staging\nLast error: Error: sync stopped at step 3",
      tried: "",
      look_at: "fused publish q4",
    });
    expect(briefFromTask([]).problem).toBe("");
  });

  test("an error the task got past is not the problem; 'Exit code N' is skipped; a multi-line command is flattened", () => {
    const tool = (id: string, command: string, status: string, output: string) => ({
      kind: "tool", id, name: "Bash", input: { command }, status, output, images: [],
    });
    const turn = (segments: unknown[]) => ({ role: "assistant", key: "a", text: "", segments }) as unknown as Turn;
    const ask = u("Implement tile_bounds", "u1");
    // Seen live: a recovered failure became "Last error: Exit code 1", Look at `python3 -I -c "`.
    expect(briefFromTask([ask, turn([tool("t1", 'python3 -I -c "\nimport x"', "error", "Exit code 1"), tool("t2", "pytest", "ok", "1 passed")])]))
      .toEqual({ problem: "Implement tile_bounds", tried: "", look_at: "" });
    expect(briefFromTask([ask, turn([tool("t1", 'python3 -c "\nimport x\n"', "error", "Exit code 1\nModuleNotFoundError: x")])]))
      .toEqual({ problem: "Implement tile_bounds\nLast error: ModuleNotFoundError: x", tried: "", look_at: 'python3 -c " import x "' });
  });

  test("folder triggers", () => {
    expect(folderMatches("/root/work/q4", ["/root/work/q4"])).toBe(true);
    expect(folderMatches("/root/work/q4/sub", ["/root/work/q4"])).toBe(true);
    expect(folderMatches("/root/work/q40", ["/root/work/q4"])).toBe(false);
    expect(folderMatches("/home/me/work/q4-canvas", ["~/work/q4-canvas"])).toBe(true);
    expect(folderMatches("/home/me/work/other", ["~/work/q4-canvas"])).toBe(false);
  });

  test("specialists whose triggers match the task's folder come first under Suggested", async () => {
    const r = render(
      <BreakoutMenu
        specialists={SPECS}
        host={host}
        folder="/w/q4"
        detail={async (id) => ({ triggers: { folders: id === "d2" ? ["/w/q4"] : [], keywords: [] } }) as never}
      />,
    );
    act(() => button(r, "Breakout").props.onClick());
    await flush();
    const order = r.root
      .findAll((n) => (n.props.role === "menuitem" && n.type === "button") || n.props.className === "spx-bmenu-group")
      .map((n) => textOf(n).trim());
    expect(order).toEqual(["Suggested", "Data specialist", "Other specialists", "Canvas specialist"]);
  });

  test("arrow keys move, Enter picks, Escape closes", () => {
    // The menu moves real DOM focus; the test renderer has none, so its two
    // items are stand-ins handed out through the menu's ref.
    let active: unknown = null;
    const picked: string[] = [];
    const fake = ["Canvas specialist", "Data specialist"].map((label) => {
      const el = { label, focus: () => void (active = el), click: () => picked.push(label) };
      return el;
    });
    const doc = globalThis.document as unknown as Record<string, unknown>;
    const saved = Object.getOwnPropertyDescriptor(doc, "activeElement");
    Object.defineProperty(doc, "activeElement", { configurable: true, get: () => active });
    try {
      let r!: ReactTestRenderer;
      act(() => {
        r = create(<BreakoutMenu specialists={SPECS} host={host} turns={[u("Fix the sync")]} />, {
          createNodeMock: (el) => (el.props.role === "menu" ? { querySelectorAll: () => fake } : {}),
        });
      });
      mounted.push(r);
      act(() => button(r, "Breakout").props.onClick());
      // Opening focuses the first item.
      expect((active as { label: string }).label).toBe("Canvas specialist");
      const menu = () => r.root.find((n) => n.props.role === "menu" && n.type === "div");
      const key = (k: string) => act(() => menu().props.onKeyDown({ key: k, preventDefault: () => {}, stopPropagation: () => {} }));
      key("ArrowDown");
      expect((active as { label: string }).label).toBe("Data specialist");
      key("ArrowDown");
      expect((active as { label: string }).label).toBe("Canvas specialist");
      key("ArrowUp");
      expect((active as { label: string }).label).toBe("Data specialist");
      key("Enter");
      expect(picked).toEqual(["Data specialist"]);
      key("Escape");
      expect(r.root.findAll((n) => n.props.role === "menu")).toHaveLength(0);
      // Picking a specialist opens the brief, pre-filled from the task.
      act(() => button(r, "Breakout").props.onClick());
      act(() => button(r, "Data specialist").props.onClick());
      expect(r.root.findAll((n) => n.type === "textarea")[0]!.props.value).toBe("Fix the sync");
    } finally {
      if (saved) Object.defineProperty(doc, "activeElement", saved);
      else delete doc.activeElement;
    }
  });
});

describe("the services card", () => {
  const actions = { decidePermission: async () => {} } as never;
  test("'Approve, don't ask again' sits inside the card's own action row", async () => {
    let more: unknown = "unset";
    render(
      <ServiceCardExtra
        specialistId="c1"
        requestId="req-1"
        tool="mcp__specialist_services__slack_post"
        actions={actions}
        card={(m) => ((more = m), null)}
        load={async () => ({ services: [{ key: "slack", title: "Slack", kind: "connect", connected: true, allowed: false }] })}
      />,
    );
    await flush();
    expect((more as { text: string }[]).map((x) => x.text)).toEqual(["Approve, don't ask again"]);
  });

  test("not offered when the service is already allowed, nor for a write in a breakout", async () => {
    const seen: unknown[] = [];
    render(
      <ServiceCardExtra
        specialistId="c1"
        requestId="req-1"
        tool="mcp__specialist_services__slack_post"
        actions={actions}
        card={(m) => (seen.push(m), null)}
        load={async () => ({ services: [{ key: "slack", title: "Slack", kind: "connect", connected: true, allowed: true }] })}
      />,
    );
    render(
      <ServiceCardExtra
        specialistId="c1"
        requestId="req-2"
        kind="breakout"
        tool="mcp__specialist_services__slack_post"
        actions={actions}
        card={(m) => (seen.push(m), null)}
        load={async () => ({
          services: [
            { key: "slack", title: "Slack", kind: "connect", connected: true, allowed: false, tools: [{ name: "mcp__specialist_services__slack_post", write: true }] },
          ],
        })}
      />,
    );
    await flush();
    expect(seen.every((m) => m === undefined)).toBe(true);
  });
});
