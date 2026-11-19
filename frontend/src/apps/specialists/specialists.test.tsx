// The Specialists page (docs/specialists.md §9): routes, the list and
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
import { faceColor } from "./components/Face";
import { MainChat, type ChatHandle } from "./components/MainChat";
import { FoldedSession, dayLabel, foldedLabel } from "./components/FoldedSession";
import { SidePanel, type PanelApi } from "./components/SidePanel";
import { CreateSpecialist, parseKeywords, triggersFrom } from "./dialogs/CreateSpecialist";
import { LearnedTab, fromLine, type LearnedApi } from "./dialogs/LearnedTab";
import { GeneralTab, countRows } from "./dialogs/GeneralTab";
import { SpecialistSettings, TABS } from "./dialogs/SpecialistSettings";
import { GeneralInbox } from "./dialogs/GeneralInbox";
import { MemoryTab } from "./dialogs/MemoryTab";

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
    const made: { s: SpecialistDetail | null } = { s: null };
    const r = render(
      <CreateSpecialist onClose={() => {}} onCreated={(s) => (made.s = s)} create={async (b) => (posted.push(b), SPEC)} />,
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
    expect(made.s).toBe(SPEC);
  });

  test("the face is the shipped seed's object or a plain colour string (live: Canvas seed crashed the list)", () => {
    expect(faceColor({ shape: "hexagon", color: "#2a9a86", icon: "" }, "x")).toBe("#2a9a86");
    expect(faceColor("#123456", "x")).toBe("#123456");
    expect(faceColor(null, "x")).toMatch(/^#/);
    expect(faceColor({ shape: "hexagon" }, "x")).toMatch(/^#/);
    const r = render(
      <SpecialistList
        rows={[{ id: "c1", name: "Canvas", subject: "c", face: { shape: "hexagon", color: "#2a9a86", icon: "" }, status: "Ready", working: false }]}
        selected="c1"
        loading={false}
        error=""
        onSelect={() => {}}
        onCreate={() => {}}
      />,
    );
    expect(textOf(r.root.find((n) => n.type === "a"))).toContain("Canvas");
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
    expect(mounts[mounts.length - 1]).toMatchObject({
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
    const last = mounts[mounts.length - 1]!;
    expect("sessionId" in last).toBe(false);
    expect(hasText(r, "Cleared · a new chat starts here")).toBe(true);
    expect(hasText(r, "Now · 3 messages")).toBe(true);
  });

  test("/clear in the composer goes through the same Clear", async () => {
    const { mounts, cleared } = chatRig();
    await flush();
    await act(async () => {
      (mounts[mounts.length - 1]!.onClear as () => void)();
    });
    expect(cleared).toEqual(["c1"]);
  });

  test("the page can send into the chat once it has reported", async () => {
    const { mounts, handle } = chatRig();
    await flush();
    expect(handle.current!.send("hi")).toBe(false);
    const sent: string[] = [];
    act(() => (mounts[mounts.length - 1]!.onBreakoutState as (s: unknown) => void)({ sessionId: "s-now", turns: [], send: (t: string) => sent.push(t) }));
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
    // The reply goes through the chat's markdown (lazy), or its plain-text fallback while that loads.
    expect(hasText(r, "Because.") || r.root.findAll((n) => n.props.text === "Because.").length > 0).toBe(true);
    expect(hasText(r, "why?")).toBe(true);
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
        new: 4,
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
  test("refreshKey re-reads without blanking the panel (live: stale after Run now in Settings)", async () => {
    let reads = 0;
    const api = panelApi();
    const counted: PanelApi = { ...api, getPanel: async (id) => (reads++, api.getPanel(id)) };
    const el = (k: number) => <SidePanel specialistId="c1" sendToChat={() => true} onOpenSettings={() => {}} refreshKey={k} api={counted} />;
    const r = render(el(0));
    await act(async () => {});
    expect(reads).toBe(1);
    expect(textOf(r.root)).toContain("When a sync stalls");
    act(() => r.update(el(1)));
    expect(textOf(r.root)).toContain("When a sync stalls");
    await act(async () => {});
    expect(reads).toBe(2);
  });

  test("Learned last night with Revert, Reverted; drafts; inbox; In your tasks; no breakouts listed", async () => {
    const api = panelApi();
    const r = render(<SidePanel specialistId="c1" sendToChat={() => true} onOpenSettings={() => {}} api={api} />);
    await flush();
    expect(hasText(r, "Latest update")).toBe(true);
    expect(hasText(r, "last night")).toBe(false);
    expect(() => button(r, "See all 4")).not.toThrow();
    expect(hasText(r, "Reverted")).toBe(true);
    expect(hasText(r, "3 notes waiting for the next update")).toBe(true);
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
    // One line each; where it came from is behind More.
    expect(hasText(r, "From 2 notes and a breakout")).toBe(false);
    act(() => button(r, "More").props.onClick());
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
    // The app's own confirm, not the browser's: nothing is deleted until its Delete.
    expect(log).not.toContainEqual(["delete", "c1"]);
    await act(async () => {
      await button(r, "Delete").props.onClick();
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

describe("the general inbox", () => {
  const note = { id: "n-9", kind: "gap", claim: "Nobody covers the billing export.", basis: "verified", source: { type: "task", title: "Billing" }, at: 0 };
  test("the quiet line under the list opens it", () => {
    let opened = 0;
    const r = render(
      <SpecialistList rows={[]} selected={null} loading={false} error="" onSelect={() => {}} onCreate={() => {}} generalCount={2} onOpenGeneral={() => opened++} />,
    );
    const row = r.root.find((n) => n.type === "button" && String(n.props.className).includes("spx-general-row"));
    expect(textOf(row)).toContain("2 notes to give to one");
    act(() => row.props.onClick());
    expect(opened).toBe(1);
  });

  test("Give to a specialist, or Delete", async () => {
    const log: unknown[] = [];
    let changed = 0;
    const r = render(
      <GeneralInbox
        notes={[note]}
        specialists={[{ id: "c1", name: "Canvas", subject: "", face: "", status: "", working: false }]}
        onClose={() => {}}
        onChanged={() => changed++}
        api={{
          moveGeneralNote: async (nid, spec) => (log.push(["move", nid, spec]), note as never),
          deleteNote: async (id, nid) => (log.push(["delete", id, nid]), { ok: true as const }),
        }}
      />,
    );
    expect(hasText(r, "Nobody covers the billing export.")).toBe(true);
    await act(async () => {
      button(r, "Give").props.onClick();
    });
    await act(async () => {
      button(r, "Delete").props.onClick();
    });
    expect(log).toEqual([
      ["move", "n-9", "c1"],
      ["delete", "general", "n-9"],
    ]);
    expect(changed).toBe(2);
  });
});

describe("Settings › Memory inbox", () => {
  test("In effect now only for a note that is in effect", async () => {
    const notes = [
      { id: "n-1", kind: "convention", claim: "Publish to staging first.", basis: "user-said", source: { type: "user" }, at: 0, in_effect: true },
      { id: "n-2", kind: "convention", claim: "The task says you said so.", basis: "user-said", source: { type: "task" }, at: 0, in_effect: false },
    ];
    const r = render(
      <MemoryTab
        specialistId="c1"
        sendToChat={() => true}
        api={{
          getMemory: async () => ({ entries: [], raw: "", lines: 0, bytes: 0, limit_lines: 120, limit_bytes: 12000, knowledge: [] }),
          getInbox: async () => ({ notes }),
        }}
      />,
    );
    await flush();
    const rows = r.root.findAll((n) => n.type === "div" && /^spx-note( is-said)?$/.test(String(n.props.className)));
    expect(rows.map((x) => textOf(x).includes("In effect now"))).toEqual([true, false]);
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

// ── fix wave (FE-A): what the UX review asked for ────────────────────────
import { Face, faceSvg, initialsFor } from "./components/Face";
import { findOffsets, greeting } from "./components/MainChat";
import { secondPerson, clip, agoText, revertable } from "./lib/api-extra";
import { buildSkill, parseSkill, SkillsTab } from "./dialogs/SkillsTab";
import { PeopleTab, serviceChoices } from "./dialogs/PeopleTab";
import { ConnectionsTab, serviceTitle } from "./dialogs/ConnectionsTab";
import { fromLabel } from "./dialogs/MemoryTab";

describe("faces", () => {
  test("a seeded face draws its shape in its colour; a letter only as fallback", () => {
    const r = render(<Face id="c1" name="Canvas" face={{ shape: "hexagon", color: "#2a9a86", icon: "" }} />);
    const svg = r.root.find((n) => n.type === "svg");
    expect(svg.findAll((n) => n.type === "path")[0]!.props.fill).toBe("#2a9a86");
    expect(textOf(r.root)).toBe("");
    const r2 = render(<Face id="c1" name="Canvas" face="" letters="Ca" />);
    expect(textOf(r2.root)).toBe("Ca");
    expect(faceSvg({ shape: "hexagon", color: "#2a9a86" }, "c1", "C")).toContain('fill="#2a9a86"');
  });
  test("two letters when names share a first letter", () => {
    const names = ["QA1-Alpha", "QA1-Beta", "QA3-Stats", "Canvas", "Hexagons"];
    expect(initialsFor("QA3-Stats", names)).toBe("QS");
    expect(initialsFor("QA1-Beta", names)).toBe("QB");
    expect(initialsFor("Canvas", names)).toBe("C");
    expect(initialsFor("Charts", ["Canvas", "Charts"])).toBe("Ch");
  });
});

describe("the one chat: working, greeting, model, search", () => {
  const rig = (chat: { current: string | null; folded: never[] }) => {
    const mounts: Record<string, unknown>[] = [];
    const Spy = (p: Record<string, unknown>) => (mounts.push(p), null);
    const running: boolean[] = [];
    const r = render(
      <MainChat
        specialist={{ ...SPEC, model: "opus", status: "Ready" }}
        onOpenSettings={() => {}}
        onRunning={(b) => running.push(b)}
        mount={Spy as never}
        api={{ getChat: async () => chat, clearChat: async () => chat }}
      />,
    );
    return { r, mounts, running };
  };
  test("an empty chat greets; the composer names the specialist; the model starts as its own (R7)", async () => {
    const { r, mounts } = rig({ current: null, folded: [] });
    await flush();
    expect(hasText(r, "I'm the Canvas specialist. Tell me something to remember, or ask me anything about Fused canvases.")).toBe(true);
    expect(mounts[mounts.length - 1]).toMatchObject({ model: "opus", placeholder: "Message the Canvas specialist" });
  });
  test("the header says Working… while the chat runs, and tells the page", async () => {
    const { r, mounts, running } = rig({ current: "s1", folded: [] });
    await flush();
    expect(hasText(r, "Ready")).toBe(true);
    act(() => (mounts[mounts.length - 1]!.onBreakoutState as (s: unknown) => void)({ sessionId: "s1", turns: [{}], running: true }));
    expect(hasText(r, "Working…")).toBe(true);
    expect(hasText(r, "I'm the Canvas specialist")).toBe(false);
    expect(running[running.length - 1]).toBe(true);
  });
  test("search offsets and greeting text", () => {
    expect(findOffsets("None and none, NONE", "none")).toEqual([0, 9, 15]);
    expect(findOffsets("abc", "")).toEqual([]);
    expect(greeting({ name: "Stats", subject: "Python statistics." })).toBe(
      "I'm the Stats specialist. Tell me something to remember, or ask me anything about Python statistics.",
    );
  });
  test("the list says Working… from the chat's own run state before its poll", () => {
    const r = render(
      <SpecialistList
        rows={[{ id: "c1", name: "Canvas", subject: "c", face: "", status: "Ready", working: false }]}
        selected="c1"
        loading={false}
        error=""
        onSelect={() => {}}
        onCreate={() => {}}
        runningHere
      />,
    );
    expect(textOf(r.root.find((n) => n.type === "a"))).toContain("Working…");
  });
});

describe("plain words (R1)", () => {
  test("second person, clipped titles, ages", () => {
    expect(secondPerson("In the user's projects, mean() must skip None.")).toBe("In your projects, mean() must skip None.");
    expect(secondPerson("The user's canvases are left-aligned.")).toBe("Your canvases are left-aligned.");
    expect(secondPerson("Asked by the user.")).toBe("Asked by you.");
    expect(secondPerson("The user prefers tabs.")).toBe("The user prefers tabs.");
    expect(clip("QA3T2: I keep getting wrong averages from stats.py and I'm stuck. Hand this problem", 40)).toBe(
      "QA3T2: I keep getting wrong averages…",
    );
    expect(agoText(100, 112_000)).toBe("12 s ago");
  });
  test("an entry's source in words, never its raw form", () => {
    expect(fromLabel("user main chat")).toBe("You told it in its chat");
    expect(fromLabel("breakout b-12ab34cd")).toBe("A breakout from one of your tasks");
    expect(fromLabel("")).toBe("");
  });
  test("C4: only an active item can be reverted", () => {
    expect(["active", undefined, "reverting", "reverted", "changed"].map((s) => revertable(s))).toEqual([true, true, false, false, false]);
  });
});

describe("Learned while an update runs (C5)", () => {
  const base = (over: Partial<LearnedApi>): LearnedApi => ({
    getLearned: async () => ({ runs: [] }),
    listDrafts: async () => ({ drafts: [] }),
    getNightly: async () => ({ last_ok: null, running: false, last_row: null, started: null }),
    runNightly: async () => ({ row: "r9" }),
    revertChange: async () => ({ message: "", session_id: null }),
    sendDraft: async () => ({ message: "", session_id: null }),
    discardDraft: async () => ({ ok: true as const }),
    ...over,
  });
  test("Updating… started N s ago, never beside No updates yet; refreshes when it ends", async () => {
    let running = true;
    let reads = 0;
    const api = base({
      getNightly: async () => ({ last_ok: null, running, last_row: null, started: 100 }),
      getLearned: async () => (reads++, { runs: running ? [] : [{ run: "r9", at: 0, summary: "Read 2 notes.", items: [] }] }),
    });
    const r = render(<LearnedTab specialistId="c1" sendToChat={() => true} api={api} now={() => 112_000} />);
    await flush();
    expect(hasText(r, "Updating… started 12 s ago")).toBe(true);
    expect(hasText(r, "No updates yet")).toBe(false);
    running = false;
    await act(async () => {
      await new Promise((res) => setTimeout(res, 3_100));
    });
    expect(reads).toBe(2);
    expect(hasText(r, "Latest update")).toBe(true);
    expect(hasText(r, "Last night")).toBe(false);
  }, 6_000);
  test("changed and reverted items offer no Revert; Revert keeps the tab and says it was asked", async () => {
    const sent: string[] = [];
    const api = base({
      getLearned: async () => ({
        runs: [
          {
            run: "r1",
            at: 0,
            summary: "",
            items: [
              { id: "i1", kind: "Learned", text: "A", state: "active" },
              { id: "i2", kind: "Learned", text: "B", state: "changed" },
              { id: "i3", kind: "Learned", text: "C", state: "reverted" },
            ],
          },
        ],
      }),
      revertChange: async () => ({ message: "Undo this change from the latest update: A", session_id: null }),
    });
    const r = render(<LearnedTab specialistId="c1" sendToChat={(t) => (sent.push(t), true)} api={api} />);
    await flush();
    expect(r.root.findAll((n) => n.type === "button" && textOf(n) === "Revert").length).toBe(1);
    expect(hasText(r, "Changed since")).toBe(true);
    await act(async () => {
      button(r, "Revert").props.onClick();
    });
    expect(sent).toEqual(["Undo this change from the latest update: A"]);
    expect(hasText(r, "Revert asked in the chat")).toBe(true);
  });
});

describe("skills, people, connections", () => {
  test("SKILL.md front matter round-trips through fields; other keys are kept", () => {
    const text = "---\nname: publish\ndescription: When publishing a canvas.\nallowed-tools: Read\n---\n\n# Publish\n\n1. Sign in\n";
    const p = parseSkill(text);
    expect(p).toMatchObject({ name: "publish", description: "When publishing a canvas.", extra: ["allowed-tools: Read"] });
    expect(p.body.startsWith("# Publish")).toBe(true);
    expect(parseSkill(buildSkill({ ...p, description: "Before: a publish" }))).toMatchObject({ description: "Before: a publish" });
  });
  test("Skills: an empty state, and no raw front matter in the editor", async () => {
    const r = render(
      <SkillsTab
        specialistId="c1"
        api={{
          listSkills: async () => ({ skills: [] }),
          getSkill: async () => ({ name: "", text: "" }),
          putSkill: async () => ({ name: "", text: "" }),
          deleteSkill: async () => ({ ok: true as const }),
        }}
      />,
    );
    await flush();
    expect(hasText(r, "No skills yet")).toBe(true);
    expect(hasText(r, "Pick a skill")).toBe(false);
  });
  test("People: Service is a choice of connected services; Remove on every row; an empty state", async () => {
    expect(
      serviceChoices(
        [
          { key: "slack", title: "Slack", kind: "connect", connected: true, allowed: false },
          { key: "notion", title: "Notion", kind: "connect", connected: false, allowed: false },
          { key: "app", title: "Svc", kind: "app", connected: true, allowed: true },
        ],
        "Email",
      ),
    ).toEqual(["Email", "Slack"]);
    const r = render(
      <PeopleTab
        specialistId="c1"
        api={{
          getPeople: async () => ({ rows: [{ area: "Sync", owner: "Ana", service: "Slack", handle: "@ana", notes: "" }], path: "" }),
          putPeople: async (_id, rows) => ({ rows, path: "" }),
          getConnections: async () => ({ services: [{ key: "slack", title: "Slack", kind: "connect", connected: true, allowed: true }] }),
        }}
      />,
    );
    await flush();
    const sel = r.root.find((n) => n.type === "select");
    expect(sel.props.value).toBe("Slack");
    expect(() => button(r, "Remove")).not.toThrow();
    const empty = render(
      <PeopleTab specialistId="c1" api={{ getPeople: async () => ({ rows: [], path: "" }), putPeople: async (_i, rows) => ({ rows, path: "" }) }} />,
    );
    await flush();
    expect(hasText(empty, "No people yet")).toBe(true);
  });
  test("Connections: a row not signed in is off, with Sign in; the title, not the id", async () => {
    expect(serviceTitle({ key: "qa2-svc", title: "qa2-svc" })).toBe("Qa2 svc");
    expect(serviceTitle({ key: "gh", title: "GitHub" })).toBe("GitHub");
    const r = render(
      <ConnectionsTab
        specialistId="c1"
        api={{
          getConnections: async () => ({ services: [{ key: "notion", title: "Notion", kind: "connect", connected: false, allowed: false }] }),
          putConnections: async () => ({ services: [] }),
        }}
      />,
    );
    await flush();
    const row = r.root.find((n) => n.type === "li");
    expect(String(row.props.className)).toContain("is-off");
    expect(row.find((n) => n.type === "input").props.disabled).toBe(true);
    expect(row.find((n) => n.type === "a").props.href).toBe("/preferences?s=connections");
  });
});

describe("dialogs", () => {
  test("Settings: Done, Learned · N new, never wider than the window", () => {
    let closed = 0;
    const r = render(
      <SpecialistSettings
        specialist={SPEC}
        tab="general"
        onTab={() => {}}
        onClose={() => closed++}
        sendToChat={() => true}
        onSaved={() => {}}
        onDeleted={() => {}}
        learnedCount={4}
      />,
    );
    expect(textOf(r.root.findAll((n) => n.props.role === "tab")[0]!)).toBe("Learned · 4 new");
    act(() => button(r, "Done").props.onClick());
    expect(closed).toBe(1);
    expect(r.root.findAll((n) => typeof n.props.className === "string" && n.props.className.includes("modal-close")).length).toBe(0);
  });
  test("Create: examples as placeholders, errors under the field, busy on Create", async () => {
    let resolve!: (s: SpecialistDetail) => void;
    const r = render(<CreateSpecialist onClose={() => {}} onCreated={() => {}} create={() => new Promise((res) => (resolve = res))} />);
    const inputs = r.root.findAll((n) => n.type === "input");
    expect(inputs[0]!.props.placeholder).toBe("e.g. Canvas");
    await act(async () => {
      r.root.find((n) => n.type === "form").props.onSubmit({ preventDefault() {} });
    });
    expect(hasText(r, "Give it a name.")).toBe(true);
    expect(r.root.findAll((n) => n.type === "input")[0]!.props["aria-invalid"]).toBe(true);
    act(() => r.root.findAll((n) => n.type === "input")[0]!.props.onChange({ target: { value: "Canvas" } }));
    act(() => r.root.findAll((n) => n.type === "input")[1]!.props.onChange({ target: { value: "Canvases" } }));
    await act(async () => {
      r.root.find((n) => n.type === "form").props.onSubmit({ preventDefault() {} });
    });
    expect(hasText(r, "Creating…")).toBe(true);
    await act(async () => resolve(SPEC));
  });
});

describe("polish: Learned list and its counts", () => {
  test("See all follows the unseen count; a bookkeeping-only update says so", async () => {
    const api = panelApi({
      getPanel: async () => ({
        learned: { run: "r2", at: 0, summary: "Nothing new was learned; 5 entries were confirmed again.", total: 0, new: 0, items: [] },
        waiting: [],
        inbox: { count: 0 },
        tasks: { helping_now: [], guided_week: 0 },
      }),
    });
    const r = render(<SidePanel specialistId="c1" sendToChat={() => true} onOpenSettings={() => {}} api={api} />);
    await flush();
    expect(hasText(r, "Nothing new was learned; 5 entries were confirmed again.")).toBe(true);
    expect(hasText(r, "See all")).toBe(false);
    const seen = panelApi({ getPanel: async () => ({ ...(await panelApi().getPanel("c1")), learned: { ...(await panelApi().getPanel("c1")).learned!, new: 0 } }) });
    const r2 = render(<SidePanel specialistId="c1" sendToChat={() => true} onOpenSettings={() => {}} api={seen} />);
    await flush();
    expect(() => button(r2, "See all")).not.toThrow();
    expect(hasText(r2, "See all 4")).toBe(false);
  });

  function learnedApi(over: Partial<LearnedApi> = {}): LearnedApi & { seen: string[] } {
    const seen: string[] = [];
    return {
      seen,
      getLearned: async () => ({
        runs: [
          { run: "r2", at: 0, summary: "Nothing new was learned; 3 entries were confirmed again.", items: [], confirmed: 3 },
          { run: "r1", at: 0, summary: "Read 2 notes.", items: [{ id: "i1", kind: "Learned", text: "Publish to staging first." }] },
        ],
      }),
      listDrafts: async () => ({ drafts: [] }),
      getNightly: async () => ({ last_ok: null, running: false, last_row: null }),
      runNightly: async () => ({ row: "x" }) as never,
      revertChange: async () => ({ message: "", session_id: null }),
      sendDraft: async () => ({ message: "", session_id: null }),
      discardDraft: async () => ({ ok: true as const }),
      markSeen: async (id, run) => (seen.push(`${id}/${run}`), { seen: run, new: 0 }),
      ...over,
    };
  }

  test("opening Learned marks the newest update seen, once", async () => {
    const api = learnedApi();
    let told = 0;
    const r = render(<LearnedTab specialistId="c1" sendToChat={() => true} api={api} onSeen={() => told++} />);
    await flush();
    expect(api.seen).toEqual(["c1/r2"]);
    expect(told).toBe(1);
    expect(hasText(r, "Nothing new was learned; 3 entries were confirmed again.")).toBe(true);
    expect(hasText(r, "No changes.")).toBe(true);
  });

  test("closed reports are listed apart from the drafts waiting (polish 9)", async () => {
    const api = learnedApi({
      listDrafts: async () => ({
        drafts: [
          { id: "d-1", at: 0, to: { name: "Ana", service: "Slack", handle: "" }, subject: "Sync bug", text: "", why: "", status: "waiting" },
          { id: "d-2", at: 0, to: { name: "Bo", service: "Slack", handle: "" }, subject: "Old bug", text: "", why: "Fixed upstream", status: "closed" },
        ],
      }),
    });
    const r = render(<LearnedTab specialistId="c1" sendToChat={() => true} api={api} />);
    await flush();
    const closed = r.root.find((n) => n.type === "section" && n.props["aria-label"] === "Closed reports");
    expect(textOf(closed)).toContain("Old bug");
    expect(textOf(closed)).toContain("Fixed upstream");
    expect(textOf(closed)).not.toContain("Sync bug");
  });

  test("an interrupted update that changed nothing gets one quiet line", async () => {
    const api = learnedApi({
      getLearned: async () => ({ runs: [] }),
      getNightly: async () => ({ last_ok: null, running: false, last_row: null, stopped_empty: "interrupted" }),
    });
    const r = render(<LearnedTab specialistId="c1" sendToChat={() => true} api={api} />);
    await flush();
    expect(hasText(r, "The last update was interrupted and changed nothing. Run now to try again.")).toBe(true);
    expect(hasText(r, "No updates yet")).toBe(false);
  });

  test("Learned · N new stays while the tab is up, and is gone once the user moves on", async () => {
    // The tabs read the server through the default API: hold every read so nothing lands in this test.
    const realFetch = globalThis.fetch;
    globalThis.fetch = (() => new Promise(() => {})) as unknown as typeof fetch;
    const el = (tab: "learned" | "memory") => (
      <SpecialistSettings specialist={SPEC} tab={tab} onTab={() => {}} onClose={() => {}} sendToChat={() => true} onSaved={() => {}} onDeleted={() => {}} learnedCount={6} />
    );
    const r = render(el("learned"));
    const label = () => textOf(r.root.findAll((n) => n.props.role === "tab")[0]!);
    expect(label()).toBe("Learned · 6 new");
    // the tab marks it seen
    const tab = r.root.find((n) => n.type === LearnedTab);
    act(() => tab.props.onSeen());
    expect(label()).toBe("Learned · 6 new");
    act(() => r.update(el("memory")));
    expect(label()).toBe("Learned");
    globalThis.fetch = realFetch;
  });

  test("the list's foot never calls a specialist an expert", () => {
    const r = render(
      <SpecialistList rows={[]} selected={null} loading={false} error="" onSelect={() => {}} onCreate={() => {}} generalCount={0} onOpenGeneral={() => {}} />,
    );
    expect(hasText(r, "Each specialist knows one subject in depth. Tasks can ask any of them for help.")).toBe(true);
    expect(textOf(r.root).toLowerCase()).not.toContain("expert");
  });
});

import { noteText } from "./lib/api-extra";

describe("review fixes: entry ids in notes, the panel before any update", () => {
  const ENTRY = { id: "m-0001", text: "Our tile service serves 512-pixel tiles, so tile_size must be passed through." };

  test("noteText: a known entry reads as its first words, an unknown id is dropped", () => {
    expect(noteText("tile_bounds hardcodes 256; this contradicts m-0001.", [ENTRY])).toBe(
      "tile_bounds hardcodes 256; this contradicts the entry “Our tile service serves 512-pixel tiles…”.",
    );
    expect(noteText("Wrong per [m-0001].", [ENTRY])).toBe("Wrong per the entry “Our tile service serves 512-pixel tiles…”.");
    expect(noteText("This contradicts m-0001.")).toBe("This contradicts.");
    expect(noteText("Fonts load late (see [m-ab12]), so wait.")).toBe("Fonts load late, so wait.");
    expect(noteText("No ids here; atom-abcd stays.")).toBe("No ids here; atom-abcd stays.");
  });

  test("Settings › Memory inbox and the general inbox never show a bare entry id", async () => {
    const notes = [
      { id: "n-1", kind: "case", claim: "Latitudes break at 512px; this contradicts m-0001.", basis: "verified", source: { type: "task" }, at: 0 },
    ];
    const r = render(
      <MemoryTab
        specialistId="c1"
        sendToChat={() => true}
        api={{
          getMemory: async () => ({
            entries: [{ ...ENTRY, basis: "user-said", seen: 0, confirmed: "", from: "", retires: "", raw: "" }],
            raw: "", lines: 1, bytes: 0, limit_lines: 120, limit_bytes: 12000, knowledge: [],
          }),
          getInbox: async () => ({ notes }),
        }}
      />,
    );
    await flush();
    const row = r.root.find((n) => n.type === "div" && n.props.className === "spx-note");
    expect(textOf(row)).toContain("this contradicts the entry “Our tile service serves 512-pixel tiles…”.");
    expect(textOf(row)).not.toContain("m-0001");
    const g = render(
      <GeneralInbox
        notes={notes as never}
        specialists={[]}
        onClose={() => {}}
        onChanged={() => {}}
        api={{ moveGeneralNote: async () => ({}) as never, deleteNote: async () => ({ ok: true as const }) }}
      />,
    );
    expect(hasText(g, "Latitudes break at 512px; this contradicts.")).toBe(true);
    expect(textOf(g.root)).not.toContain("m-0001");
  });

  test("before any update the side panel says when updates run, and nothing else there", async () => {
    const api = panelApi({
      getPanel: async () => ({ learned: null, updated: false, waiting: [], inbox: { count: 0 }, tasks: { helping_now: [], guided_week: 0 } }),
    });
    const r = render(<SidePanel specialistId="c1" sendToChat={() => true} onOpenSettings={() => {}} api={api} />);
    await flush();
    expect(hasText(r, "No update has run yet. Updates run once a day while the app is open and idle.")).toBe(true);
    expect(hasText(r, "Nothing changed in the last update.")).toBe(false);
    expect(hasText(r, "See all")).toBe(false);
    const after = panelApi({
      getPanel: async () => ({ learned: null, updated: true, waiting: [], inbox: { count: 0 }, tasks: { helping_now: [], guided_week: 0 } }),
    });
    const r2 = render(<SidePanel specialistId="c1" sendToChat={() => true} onOpenSettings={() => {}} api={after} />);
    await flush();
    expect(hasText(r2, "Nothing changed in the last update.")).toBe(true);
  });
});
