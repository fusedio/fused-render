// The Specialists page (docs/specialists PLAN C.4 tests): routes, the list and
// create, the one chat's Clear and folded sessions, the side panel's Revert
// and drafts, the settings tabs, and the flag gates in the shell.
import { installDomShim, installPortalContainer, removePortalContainer } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";

import type { SpecialistDetail } from "@platform/lib/specialists-api";
import { isSpecialistsPath, parseSpecialistsPath, specialistHref } from "./routes";
import { SpecialistList } from "./components/SpecialistList";
import { MainChat, type ChatHandle } from "./components/MainChat";
import { FoldedSession, dayLabel, foldedLabel } from "./components/FoldedSession";
import { SidePanel, type PanelApi } from "./components/SidePanel";
import { CreateSpecialist, parseKeywords, triggersFrom } from "./dialogs/CreateSpecialist";
import { LearnedTab, fromLine, type LearnedApi } from "./dialogs/LearnedTab";
import { GeneralTab, countRows } from "./dialogs/GeneralTab";
import { SpecialistSettings, TABS } from "./dialogs/SpecialistSettings";

const mounted: ReactTestRenderer[] = [];
beforeEach(() => installPortalContainer());
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  removePortalContainer();
});
function render(el: React.ReactElement) {
  let r!: ReactTestRenderer;
  act(() => {
    r = create(el);
  });
  mounted.push(r);
  return r;
}
const textOf = (n: ReactTestInstance | string): string =>
  typeof n === "string" ? n : n.children.map((c) => textOf(c as ReactTestInstance | string)).join("");
const button = (r: ReactTestRenderer, label: string) => {
  const found = r.root.findAll((n) => n.type === "button" && textOf(n).trim() === label);
  if (!found.length) throw new Error(`no button ${label}: ${r.root.findAll((n) => n.type === "button").map(textOf).join(" | ")}`);
  return found[0]!;
};
const hasText = (r: ReactTestRenderer, s: string) => textOf(r.root).includes(s);
const flush = () =>
  act(async () => {
    await new Promise((res) => setTimeout(res, 0));
  });

const SPEC: SpecialistDetail = {
  id: "c1",
  name: "Canvas",
  subject: "Fused canvases",
  face: "",
  model: "",
  effort: "",
  triggers: { folders: ["/w/q4"], keywords: ["sync"] },
  home: "/w/specialists/canvas",
  connections: [],
  created: 0,
  shipped: "canvas",
  status: "Working · memory updated last night",
};

describe("routes", () => {
  test("the prefix and one specialist", () => {
    expect(isSpecialistsPath("/specialists")).toBe(true);
    expect(isSpecialistsPath("/specialists/c1")).toBe(true);
    expect(isSpecialistsPath("/specialistsx")).toBe(false);
    expect(parseSpecialistsPath("/specialists")).toEqual({ kind: "list" });
    expect(parseSpecialistsPath("/specialists/c1/extra")).toEqual({ kind: "specialist", id: "c1" });
    expect(parseSpecialistsPath(specialistHref("a b"))).toEqual({ kind: "specialist", id: "a b" });
    expect(parseSpecialistsPath("/specialists/..")).toEqual({ kind: "list" });
  });
});

describe("the list and create (C)", () => {
  test("face, name, status; the selected row is the current page; + creates", () => {
    let created = 0;
    const picked: string[] = [];
    const r = render(
      <SpecialistList
        rows={[{ id: "c1", name: "Canvas", subject: "canvases", face: "", status: "Working on the sales canvas", working: true }]}
        selected="c1"
        loading={false}
        error=""
        onSelect={(id) => picked.push(id)}
        onCreate={() => created++}
      />,
    );
    const row = r.root.find((n) => n.type === "a");
    expect(row.props["aria-current"]).toBe("page");
    expect(textOf(row)).toContain("Working on the sales canvas");
    act(() => row.props.onClick({ preventDefault() {}, button: 0 }));
    expect(picked).toEqual(["c1"]);
    act(() => r.root.find((n) => n.props["aria-label"] === "New specialist").props.onClick());
    expect(created).toBe(1);
  });

  test("create posts name, subject and triggers", async () => {
    const posted: unknown[] = [];
    let made: SpecialistDetail | null = null;
    const r = render(
      <CreateSpecialist onClose={() => {}} onCreated={(s) => (made = s)} create={async (b) => (posted.push(b), SPEC)} />,
    );
    const inputs = r.root.findAll((n) => n.type === "input");
    const area = r.root.find((n) => n.type === "textarea");
    act(() => inputs[0]!.props.onChange({ target: { value: "Canvas" } }));
    act(() => inputs[1]!.props.onChange({ target: { value: "Fused canvases" } }));
    act(() => area.props.onChange({ target: { value: "/w/q4\n\n" } }));
    act(() => inputs[2]!.props.onChange({ target: { value: "sync, publish, Sync" } }));
    await act(async () => {
      r.root.find((n) => n.type === "form").props.onSubmit({ preventDefault() {} });
    });
    expect(posted[0]).toEqual({ name: "Canvas", subject: "Fused canvases", triggers: { folders: ["/w/q4"], keywords: ["sync", "publish"] } });
    expect(made).toBe(SPEC);
  });

  test("triggers parse", () => {
    expect(parseKeywords("a, b\nA")).toEqual(["a", "b"]);
    expect(triggersFrom("", "")).toEqual({ folders: [], keywords: [] });
  });
});

describe("the one chat (D, E)", () => {
  function chatRig(chat = { current: "s-now" as string | null, folded: [{ session_id: "s-old", started: 0, ended: 0, title: "Set up the Q4 canvas", messages: 22 }] }) {
    const mounts: Record<string, unknown>[] = [];
    const Spy = (p: Record<string, unknown>) => {
      mounts.push(p);
      return null;
    };
    const cleared: string[] = [];
    const api = {
      getChat: async () => chat,
      clearChat: async (id: string) => (cleared.push(id), { current: null, folded: [...chat.folded, { session_id: "s-now", started: 0, ended: 0, title: "Now", messages: 3 }] }),
    };
    const handle = { current: null as ChatHandle | null };
    const r = render(<MainChat specialist={SPEC} handle={handle} onOpenSettings={() => {}} mount={Spy as never} api={api} />);
    return { r, mounts, cleared, handle };
  }

  test("mounts the task chat on the home with the main start param and the current session", async () => {
    const { r, mounts } = chatRig();
    await flush();
    expect(mounts.at(-1)).toMatchObject({
      file: "/w/specialists/canvas",
      sessionId: "s-now",
      extraParams: { specialist: "c1:main" },
      specialist: { id: "c1", kind: "main" },
    });
    expect(r.root.find((n) => n.type === "h1").children).toEqual(["Canvas", " specialist"]);
    // Header: Clear, Search, Settings.
    for (const b of ["Clear", "Search", "Settings"]) expect(() => button(r, b)).not.toThrow();
  });

  test("Clear calls chat/clear and remounts with no session; the old one folds", async () => {
    const { r, mounts, cleared } = chatRig();
    await flush();
    await act(async () => {
      button(r, "Clear").props.onClick();
    });
    expect(cleared).toEqual(["c1"]);
    const last = mounts.at(-1)!;
    expect("sessionId" in last).toBe(false);
    expect(hasText(r, "Cleared · a new chat starts here")).toBe(true);
    expect(hasText(r, "Now · 3 messages")).toBe(true);
  });

  test("/clear in the composer goes through the same Clear", async () => {
    const { mounts, cleared } = chatRig();
    await flush();
    await act(async () => {
      (mounts.at(-1)!.onClear as () => void)();
    });
    expect(cleared).toEqual(["c1"]);
  });

  test("the page can send into the chat once it has reported", async () => {
    const { mounts, handle } = chatRig();
    await flush();
    expect(handle.current!.send("hi")).toBe(false);
    const sent: string[] = [];
    act(() => (mounts.at(-1)!.onBreakoutState as (s: unknown) => void)({ sessionId: "s-now", turns: [], send: (t: string) => sent.push(t) }));
    expect(handle.current!.send("Revert last night's change")).toBe(true);
    expect(sent).toEqual(["Revert last night's change"]);
  });

  test("a folded session: marker line, Show reads it read-only", async () => {
    const r = render(
      <FoldedSession
        folded={{ session_id: "s-old", started: 0, ended: 0, title: "Why did the embed lose its filters?", messages: 14 }}
        home="/h"
        load={async (file, sid) => {
          expect([file, sid]).toEqual(["/h", "s-old"]);
          return { turns: [{ role: "user", text: "why?" }, { role: "assistant", text: "Because." }] };
        }}
      />,
    );
    expect(hasText(r, "Why did the embed lose its filters? · 14 messages")).toBe(true);
    await act(async () => {
      button(r, "Show").props.onClick();
    });
    expect(hasText(r, "Because.")).toBe(true);
    expect(() => button(r, "Hide")).not.toThrow();
  });

  test("day labels", () => {
    const now = new Date(2026, 10, 18, 12);
    expect(dayLabel(now.getTime() / 1000, now)).toBe("Today");
    expect(dayLabel(new Date(2026, 10, 17, 9).getTime() / 1000, now)).toBe("Yesterday");
    expect(foldedLabel({ session_id: "x", started: 0, ended: 0, title: "", messages: 1 }, now)).toBe("Earlier · 1 message");
  });
});

function panelApi(over: Partial<PanelApi> = {}): PanelApi & { log: unknown[] } {
  const log: unknown[] = [];
  return {
    log,
    getPanel: async () => ({
      learned: {
        run: "r1",
        at: 0,
        summary: "Read 9 notes.",
        total: 4,
        items: [
          { id: "i1", kind: "Learned", text: "When a sync stalls, reopen the canvas.", state: "active" },
          { id: "i2", kind: "Corrected", text: "Filters are re-linked after a layout change.", state: "reverted" },
        ],
      },
      waiting: [
        { id: "d-1", at: 0, to: { name: "Ana", service: "Slack", handle: "@ana" }, subject: "Sync stalls", text: "t", why: "", status: "waiting" },
      ],
      inbox: { count: 3 },
      tasks: { helping_now: [{ session_id: "s9", title: "Publish Q4 canvas", file: "/w/q4", breakout_id: "b-1" }], guided_week: 5 },
    }),
    revertChange: async (id, run, item) => (log.push(["revert", id, run, item]), { message: "Revert last night's change: …", session_id: "s-now" }),
    sendDraft: async (id, did) => (log.push(["send", id, did]), { message: "Review and send this draft to Ana…", session_id: "s-now" }),
    discardDraft: async (id, did) => (log.push(["discard", id, did]), { ok: true as const }),
    ...over,
  };
}

describe("the side panel (U, V)", () => {
  test("Learned last night with Revert, Reverted; drafts; inbox; In your tasks; no breakouts listed", async () => {
    const api = panelApi();
    const r = render(<SidePanel specialistId="c1" sendToChat={() => true} onOpenSettings={() => {}} api={api} />);
    await flush();
    expect(hasText(r, "Learned last night")).toBe(true);
    expect(() => button(r, "See all 4")).not.toThrow();
    expect(hasText(r, "Reverted")).toBe(true);
    expect(hasText(r, "3 notes waiting for tonight's update")).toBe(true);
    expect(hasText(r, "Helping in 1 task now")).toBe(true);
    expect(hasText(r, "Gave guidance to 5 tasks this week")).toBe(true);
    expect(r.root.find((n) => n.type === "a").props.href).toContain("session_id=s9");
    expect(hasText(r, "Breakout")).toBe(false);
  });

  test("Revert sends the server's message into the main chat", async () => {
    const api = panelApi();
    const sent: string[] = [];
    const r = render(<SidePanel specialistId="c1" sendToChat={(t) => (sent.push(t), true)} onOpenSettings={() => {}} api={api} />);
    await flush();
    await act(async () => {
      r.root.find((n) => n.props["aria-label"] === "Revert: When a sync stalls, reopen the canvas.").props.onClick();
    });
    expect(api.log).toContainEqual(["revert", "c1", "r1", "i1"]);
    expect(sent).toEqual(["Revert last night's change: …"]);
    expect(hasText(r, "Revert asked in the chat")).toBe(true);
  });

  test("Review and send goes through the main chat; Discard removes", async () => {
    const api = panelApi();
    const sent: string[] = [];
    const r = render(<SidePanel specialistId="c1" sendToChat={(t) => (sent.push(t), true)} onOpenSettings={() => {}} api={api} />);
    await flush();
    await act(async () => {
      button(r, "Review and send").props.onClick();
    });
    await act(async () => {
      button(r, "Discard").props.onClick();
    });
    expect(api.log).toEqual([
      ["send", "c1", "d-1"],
      ["discard", "c1", "d-1"],
    ]);
    expect(sent).toEqual(["Review and send this draft to Ana…"]);
  });
});

describe("settings (one dialog, six tabs)", () => {
  test("the tabs, in order", () => {
    expect(TABS.map((t) => t.label)).toEqual(["Learned", "Memory", "Skills", "People", "Connections", "General"]);
  });

  test("the dialog draws the chosen tab and switches by click", () => {
    const tabs: string[] = [];
    const r = render(
      <SpecialistSettings
        specialist={SPEC}
        tab="people"
        onTab={(t) => tabs.push(t)}
        onClose={() => {}}
        sendToChat={() => true}
        onSaved={() => {}}
        onDeleted={() => {}}
      />,
    );
    const tabBtns = r.root.findAll((n) => n.props.role === "tab" && n.type === "button");
    expect(tabBtns.map(textOf)).toEqual(TABS.map((t) => t.label));
    expect(tabBtns.find((b) => b.props["aria-selected"])!.children).toEqual(["People"]);
    act(() => tabBtns[0]!.props.onClick());
    expect(tabs).toEqual(["learned"]);
  });

  test("Learned: items with Revert, drafts with Review and send / Discard, Run now (409 says busy)", async () => {
    const log: unknown[] = [];
    const api: LearnedApi = {
      getLearned: async () => ({ runs: [{ run: "r1", at: 0, summary: "Read 9 notes.", items: [{ id: "i1", kind: "New skill", text: "Publish checklist", from: ["n-1", "n-2", "b-1"] }] }] }),
      listDrafts: async () => ({ drafts: [{ id: "d-1", at: 0, to: { name: "Ana", service: "Slack", handle: "" }, subject: "Bug", text: "", why: "Seen 3 times", status: "waiting" }] }),
      getNightly: async () => ({ last_ok: null, running: false, last_row: null }),
      runNightly: async () => {
        log.push("run");
        throw Object.assign(new Error("busy"), { status: 409 });
      },
      revertChange: async (...a) => (log.push(["revert", ...a]), { message: "REVERT", session_id: null }),
      sendDraft: async (...a) => (log.push(["send", ...a]), { message: "SEND", session_id: null }),
      discardDraft: async (...a) => (log.push(["discard", ...a]), { ok: true as const }),
    };
    const sent: string[] = [];
    const r = render(<LearnedTab specialistId="c1" sendToChat={(t) => (sent.push(t), true)} api={api} />);
    await flush();
    expect(hasText(r, "From 2 notes and a breakout")).toBe(true);
    await act(async () => {
      button(r, "Revert").props.onClick();
    });
    await act(async () => {
      button(r, "Review and send").props.onClick();
    });
    await act(async () => {
      button(r, "Run now").props.onClick();
    });
    expect(sent).toEqual(["REVERT", "SEND"]);
    expect(log).toContainEqual(["revert", "c1", "r1", "i1"]);
    expect(hasText(r, "It cannot run right now")).toBe(true);
  });

  test("General: saves the fields and deletes after a confirm; shows the counts", async () => {
    const log: unknown[] = [];
    let deleted = 0;
    const r = render(
      <GeneralTab
        specialist={SPEC}
        onSaved={() => {}}
        onDeleted={() => deleted++}
        confirm={() => true}
        api={{
          patchSpecialist: async (id, b) => (log.push(["patch", id, b]), SPEC),
          deleteSpecialist: async (id) => (log.push(["delete", id]), { ok: true as const }),
          getCounts: async () => ({ breakouts_opened: 2, repeat_breakouts: 0, pulls: 7, pulls_confirmed: 5, corrections_received: 1, notes_by_source: { task: 3 } }),
          getCharter: async () => ({ text: "# The Canvas specialist", path: "/h/CLAUDE.md" }),
          putCharter: async (id, text) => (log.push(["charter", id, text]), { text, path: "" }),
        }}
      />,
    );
    await flush();
    expect(hasText(r, "Pulls confirmed")).toBe(true);
    await act(async () => {
      r.root.findAll((n) => n.type === "form")[0]!.props.onSubmit({ preventDefault() {} });
    });
    expect(log[0]).toEqual(["patch", "c1", { name: "Canvas", subject: "Fused canvases", triggers: { folders: ["/w/q4"], keywords: ["sync"] }, model: "", effort: "" }]);
    await act(async () => {
      button(r, "Delete this specialist").props.onClick();
    });
    expect(log).toContainEqual(["delete", "c1"]);
    expect(deleted).toBe(1);
  });

  test("count rows and from-lines", () => {
    expect(countRows({ breakouts_opened: 1, repeat_breakouts: 0, pulls: 0, pulls_confirmed: 0, corrections_received: 0, notes_by_source: {} })[0]).toEqual(["Breakouts opened", 1]);
    expect(fromLine(["r-1"])).toBe("From a review of a task");
    expect(fromLine([])).toBe("");
  });
});

describe("the flag gates in the shell (A)", () => {
  const SRC = join(import.meta.dir, "..", "..");
  const read = (p: string) => readFileSync(join(SRC, p), "utf8");

  test("the nav row and the rail item exist only with the flag on", () => {
    const s = read("shell/GlobalSidebar.tsx");
    expect(s).toContain("const specialistsOn = useSpecialistsEnabled();");
    expect(s).toContain("{specialistsOn && (");
    expect(s).toContain("...(specialistsOn");
  });

  test("the route renders only when the flag is known on, and redirects home when it is off", () => {
    const s = read("shell/App.tsx");
    expect(s).toContain('if (specialistsAnswer === false && isSpecialistsPath(location.pathname)) {');
    expect(s).toContain("const isSpecialists = isSpecialistsPath(pathname) && specialistsAnswer !== false;");
    expect(s).toContain("{specialistsAnswer ? (");
  });

  test("the toggle saves `specialists_enabled` and publishes it", () => {
    const s = read("shell/settings/toggles.tsx");
    expect(s).toContain("put={putSpecialistsEnabled}");
    expect(s).toContain("publishSpecialistsEnabled(next.specialists?.enabled === true)");
    expect(read("shell/settings/registry.ts")).toContain('id: "specialists",');
  });
});
