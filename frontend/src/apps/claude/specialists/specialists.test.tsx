// The task side of specialists (docs/specialists PLAN C.4 tests): chips placed
// by time, the offer card's decide bodies, the Breakout control, Send to a
// specialist with Undo, the services card's extra button, the CardStack seam,
// and nothing at all with the flag off.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";

import { publishSpecialistsEnabled, resetSpecialistsFlagForTests } from "@platform/lib/specialists-flag";
import { getSpecialistBreakouts, resetSpecialistBreakouts } from "@platform/lib/specialist-breakouts";
import type { TaskEvent } from "@platform/lib/specialists-api";
import type { Turn } from "../protocol/controller-api";
import type { PermissionRow } from "../protocol/types";
import { CardStack } from "../ui/CardStack";
import { BreakoutMenu } from "./BreakoutMenu";
import { OFFER_TOOL, SpecialistCardContext } from "./context";
import { OfferCard, briefAnswers } from "./OfferCard";
import { SendToSpecialist, claimOf } from "./SendToSpecialist";
import { ServiceCardExtra } from "./ServiceCardExtra";
import { chipText, placeEvents, TaskEventChip } from "./TaskEventChips";
import { useSpecialistTaskSide, type TaskSide } from "./TaskSide";
import { setSpecialistListForTests } from "./useTaskEvents";

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  resetSpecialistBreakouts();
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
const flush = () => act(async () => {
  await new Promise((res) => setTimeout(res, 0));
});

const SPECS = [
  { id: "c1", name: "Canvas", subject: "canvases", face: "", status: "", working: false },
  { id: "d2", name: "Data", subject: "data", face: "", status: "", working: false },
];
const ev = (over: Partial<TaskEvent>): TaskEvent => ({ kind: "pull", at: 0, specialist: { id: "c1", name: "Canvas" }, ...over });
const user = (key: string, ts?: number) => ({ role: "user", key, text: key, ...(ts ? { ts } : {}) }) as unknown as Turn;
const reply = (key: string, endedAt?: number) =>
  ({ role: "assistant", key, text: key, ...(endedAt ? { endedAt } : {}) }) as unknown as Turn;

describe("placeEvents", () => {
  test("an event goes after the last turn whose time is at or before it", () => {
    const turns = [user("u1", 100), reply("a1", 110), user("u2", 200), reply("a2")];
    const out = placeEvents(turns, [ev({ at: 101 }), ev({ kind: "breakout_closed", at: 150 }), ev({ kind: "note", at: 201 })]);
    expect(out.after.get("u1")!.map((e) => e.kind)).toEqual(["pull"]);
    expect(out.after.get("a1")!.map((e) => e.kind)).toEqual(["breakout_closed"]);
    // a2 has no clock yet: the note made after u2 sits under u2.
    expect(out.after.get("u2")!.map((e) => e.kind)).toEqual(["note"]);
    expect(out.tail).toEqual([]);
  });

  test("older than every timed turn, or no timed turn at all: the tail", () => {
    expect(placeEvents([user("u1", 100)], [ev({ at: 50 })]).tail).toHaveLength(1);
    expect(placeEvents([user("u1")], [ev({ at: 50 })]).tail).toHaveLength(1);
  });

  test("events keep their time order within one place", () => {
    const out = placeEvents([user("u1", 100)], [ev({ kind: "note", at: 300 }), ev({ at: 200 })]);
    expect(out.after.get("u1")!.map((e) => e.kind)).toEqual(["pull", "note"]);
  });
});

describe("chips", () => {
  test("a note the task says the user said: Use now confirms it, then it reads In effect now", async () => {
    const confirmed: string[] = [];
    const r = render(
      <TaskEventChip
        ev={ev({ kind: "note", note_id: "n-3", basis: "user-said", in_effect: false, undoable: true })}
        host={{ parentSessionId: "s1", parentFile: "/w/proj", refresh: () => {} }}
        useNow={async (id, nid) => (confirmed.push(`${id}/${nid}`), { id: nid } as never)}
      />,
    );
    expect(() => button(r, "Undo")).not.toThrow();
    await act(async () => {
      button(r, "Use now").props.onClick();
    });
    expect(confirmed).toEqual(["c1/n-3"]);
    expect(hasText(r, "In effect now")).toBe(true);
    expect(r.root.findAll((n) => n.type === "button" && textOf(n) === "Use now")).toHaveLength(0);
  });

  test("a note already in effect has no Use now", () => {
    const r = render(
      <TaskEventChip ev={ev({ kind: "note", note_id: "n-4", basis: "user-said", in_effect: true })} host={{ parentSessionId: "s1", parentFile: "/w", refresh: () => {} }} />,
    );
    expect(hasText(r, "In effect now")).toBe(true);
    expect(r.root.findAll((n) => n.type === "button" && textOf(n) === "Use now")).toHaveLength(0);
  });

  test("the words", () => {
    expect(chipText(ev({ entries: 2 }))).toBe("Guidance from the Canvas specialist was added · 2 entries");
    expect(chipText(ev({ kind: "breakout_closed" }))).toBe("Breakout closed · its full note is saved with the Canvas specialist");
    expect(chipText(ev({ kind: "note" }))).toBe("Sent to the Canvas specialist's inbox");
  });

  test("Reopen asks the server and opens the card on the breakout's session", async () => {
    const asked: string[] = [];
    const r = render(
      <TaskEventChip
        ev={ev({ kind: "breakout_closed", breakout_id: "b-1" })}
        host={{ parentSessionId: "s1", parentFile: "/w/proj", parentTitle: "Publish", refresh: () => {} }}
        reopen={async (id, bid) => {
          asked.push(`${id}/${bid}`);
          return { session_id: "bs1", start_params: { specialist: "c1:breakout:b-1" }, home: "/h" };
        }}
      />,
    );
    await act(async () => {
      button(r, "Reopen").props.onClick();
    });
    expect(asked).toEqual(["c1/b-1"]);
    expect(getSpecialistBreakouts()[0]).toMatchObject({ breakoutId: "b-1", sessionId: "bs1", parentSessionId: "s1", home: "/h" });
  });

  test("a note chip's Undo deletes the note", async () => {
    const deleted: string[] = [];
    let refreshed = 0;
    const r = render(
      <TaskEventChip
        ev={ev({ kind: "note", note_id: "n-1", undoable: true })}
        host={{ parentSessionId: "s1", parentFile: "/w/proj", refresh: () => refreshed++ }}
        undoNote={async (id, nid) => (deleted.push(`${id}/${nid}`), { ok: true as const })}
      />,
    );
    await act(async () => {
      button(r, "Undo").props.onClick();
    });
    expect(deleted).toEqual(["c1/n-1"]);
    expect(refreshed).toBe(1);
    expect(hasText(r, "Note removed")).toBe(true);
  });
});

const offerRow = (over: Partial<PermissionRow> = {}): PermissionRow =>
  ({
    id: "req-1",
    tool: OFFER_TOOL,
    input: { specialist: "c1", name: "Canvas", problem: "Sync stops at step 3", tried: "Retry", look_at: "~/q4" },
    created_at: 0,
    decision: "",
    scope: "",
    mode: "",
    answers: {},
    ...over,
  }) as PermissionRow;

function fakeActions() {
  const calls: unknown[][] = [];
  return {
    calls,
    actions: {
      decidePermission: async (...a: unknown[]) => void calls.push(["decide", ...a]),
      answerQuestion: async (...a: unknown[]) => void calls.push(["answer", ...a]),
      decidePlan: async () => {},
      dismissCard: () => {},
    } as never,
  };
}

describe("the offer card", () => {
  beforeEach(() => setSpecialistListForTests(SPECS));
  const host = { parentSessionId: "s1", parentFile: "/w/proj", parentTitle: "Publish Q4", runId: "r1" };
  const made = { breakout_id: "b-9", home: "/h", seed: "Brief from the task", start_params: { specialist: "c1:breakout:b-9" }, name: "Canvas" };

  test("titles itself and shows the brief", () => {
    const { actions } = fakeActions();
    const r = render(<OfferCard row={offerRow()} actions={actions} host={host} />);
    expect(hasText(r, "Hand this to the Canvas specialist?")).toBe(true);
    expect(hasText(r, "Sync stops at step 3")).toBe(true);
  });

  test("Open breakout: decide allow with the brief as answers, then the breakout with origin offer", async () => {
    const { actions, calls } = fakeActions();
    const created: unknown[] = [];
    const r = render(
      <OfferCard row={offerRow()} actions={actions} host={host} create={async (id, body) => (created.push([id, body]), made)} />,
    );
    await act(async () => {
      button(r, "Open breakout").props.onClick();
    });
    expect(calls).toEqual([["answer", "req-1", briefAnswers({ problem: "Sync stops at step 3", tried: "Retry", look_at: "~/q4" })]]);
    expect(created[0]).toEqual([
      "c1",
      {
        parent: { session_id: "s1", run_id: "r1", file: "/w/proj", title: "Publish Q4" },
        brief: { problem: "Sync stops at step 3", tried: "Retry", look_at: "~/q4" },
        origin: "offer",
        request_id: "req-1",
      },
    ]);
    expect(getSpecialistBreakouts()[0]).toMatchObject({ breakoutId: "b-9", seed: "Brief from the task", specialistName: "Canvas" });
  });

  test("Edit brief: the edited brief is what is decided and sent", async () => {
    const { actions, calls } = fakeActions();
    const created: unknown[] = [];
    const r = render(<OfferCard row={offerRow()} actions={actions} host={host} create={async (id, b) => (created.push(b), made)} />);
    act(() => button(r, "Edit brief").props.onClick());
    const areas = r.root.findAll((n) => n.type === "textarea");
    expect(areas).toHaveLength(3);
    act(() => areas[0]!.props.onChange({ target: { value: "Sync stalls after the update" } }));
    await act(async () => {
      button(r, "Open breakout").props.onClick();
    });
    expect((calls[0]![2] as Record<string, string[]>).problem).toEqual(["Sync stalls after the update"]);
    expect((created[0] as { brief: { problem: string } }).brief.problem).toBe("Sync stalls after the update");
  });

  test("Keep trying: decide deny, no breakout", async () => {
    const { actions, calls } = fakeActions();
    const r = render(<OfferCard row={offerRow()} actions={actions} host={host} create={async () => made} />);
    await act(async () => {
      button(r, "Keep trying").props.onClick();
    });
    expect(calls).toEqual([["decide", "req-1", "deny", "once"]]);
    expect(getSpecialistBreakouts()).toHaveLength(0);
  });

  test("the brief JSON the decide carries is exactly the three fields", () => {
    // answerQuestion joins each list with ", ": one entry each is the text itself.
    const wire = Object.fromEntries(Object.entries(briefAnswers({ problem: "a, b", tried: "", look_at: "x" })).map(([k, v]) => [k, v.join(", ")]));
    expect(JSON.parse(JSON.stringify(wire))).toEqual({ problem: "a, b", tried: "", look_at: "x" });
  });

  test("answered: no buttons, says what happened", () => {
    const { actions } = fakeActions();
    const r = render(<OfferCard row={offerRow({ decision: "deny" } as never)} actions={actions} host={host} />);
    expect(r.root.findAll((n) => n.type === "button")).toHaveLength(0);
    expect(hasText(r, "You chose to keep trying")).toBe(true);
  });
});

describe("the CardStack seam", () => {
  test("without the context the offer row is an ordinary card; with it, the offer card", () => {
    const { actions } = fakeActions();
    const plain = render(<CardStack rows={[offerRow()]} placement="open" actions={actions} />);
    expect(hasText(plain, "Hand this to")).toBe(false);
    const ctx = {
      renderCard: (row: PermissionRow) => (row.tool === OFFER_TOOL ? <div key={row.id}>offer here</div> : null),
    };
    const ours = render(
      <SpecialistCardContext.Provider value={ctx}>
        <CardStack rows={[offerRow()]} placement="open" actions={actions} />
      </SpecialistCardContext.Provider>,
    );
    expect(hasText(ours, "offer here")).toBe(true);
  });
});

describe("the Breakout control", () => {
  const host = { parentSessionId: "s1", parentFile: "/w/proj" };

  test("lists A copy of this chat and each specialist; the copy is today's action", () => {
    let copies = 0;
    const r = render(<BreakoutMenu specialists={SPECS} host={host} onCopyOfChat={() => copies++} />);
    act(() => button(r, "Breakout").props.onClick());
    expect(r.root.findAll((n) => n.props.role === "menuitem").map(textOf)).toEqual([
      "A copy of this chat",
      "Canvas specialist",
      "Data specialist",
    ]);
    act(() => button(r, "A copy of this chat").props.onClick());
    expect(copies).toBe(1);
  });

  test("without the plain breakout: only the specialists", () => {
    const r = render(<BreakoutMenu specialists={SPECS} host={host} />);
    act(() => button(r, "Breakout").props.onClick());
    expect(r.root.findAll((n) => n.props.role === "menuitem").map(textOf)).toEqual(["Canvas specialist", "Data specialist"]);
  });

  test("a specialist: type the brief, Open breakout creates it by hand and opens the card", async () => {
    const created: unknown[] = [];
    const r = render(
      <BreakoutMenu
        specialists={SPECS}
        host={host}
        create={async (id, body) => (
          created.push([id, body]),
          { breakout_id: "b-2", home: "/h2", seed: "seed", start_params: { specialist: "d2:breakout:b-2" }, name: "Data" }
        )}
      />,
    );
    act(() => button(r, "Breakout").props.onClick());
    act(() => button(r, "Data specialist").props.onClick());
    const areas = r.root.findAll((n) => n.type === "textarea");
    act(() => areas[0]!.props.onChange({ target: { value: "The join is slow" } }));
    const form = r.root.find((n) => n.type === "form");
    await act(async () => {
      form.props.onSubmit({ preventDefault() {} });
    });
    expect(created[0]).toEqual([
      "d2",
      { parent: { session_id: "s1", file: "/w/proj" }, brief: { problem: "The join is slow", tried: "", look_at: "" }, origin: "hand" },
    ]);
    expect(getSpecialistBreakouts()[0]).toMatchObject({ breakoutId: "b-2", specialistId: "d2", startParams: { specialist: "d2:breakout:b-2" } });
  });
});

describe("Send to a specialist", () => {
  test("sends a user-said note, shows the sent chip, Undo removes it", async () => {
    const added: unknown[] = [];
    const removed: string[] = [];
    const r = render(
      <SendToSpecialist
        text="Staging was the right first target."
        uuid="u-7"
        sessionId="s1"
        title="Publish Q4"
        specialists={SPECS}
        add={async (id, body) => (added.push([id, body]), { id: "n-5" } as never)}
        undo={async (id, nid) => (removed.push(`${id}/${nid}`), { ok: true as const })}
      />,
    );
    act(() => button(r, "Send to a specialist").props.onClick());
    await act(async () => {
      button(r, "Canvas specialist").props.onClick();
    });
    expect(added[0]).toEqual([
      "c1",
      {
        kind: "case",
        claim: "Staging was the right first target.",
        evidence: "Staging was the right first target.",
        basis: "user-said",
        source: { session_id: "s1", uuid: "u-7", title: "Publish Q4" },
      },
    ]);
    expect(hasText(r, "Sent to the Canvas specialist's inbox")).toBe(true);
    await act(async () => {
      button(r, "Undo").props.onClick();
    });
    expect(removed).toEqual(["c1/n-5"]);
    expect(hasText(r, "Send to a specialist")).toBe(true);
  });

  test("none fits: the general inbox", async () => {
    const added: string[] = [];
    const r = render(
      <SendToSpecialist text="x" sessionId="s1" specialists={[]} add={async (id) => (added.push(id), { id: "n-1" } as never)} />,
    );
    act(() => button(r, "Send to a specialist").props.onClick());
    await act(async () => {
      button(r, "None fits: the general inbox").props.onClick();
    });
    expect(added).toEqual(["general"]);
    expect(hasText(r, "Sent to the general inbox")).toBe(true);
  });

  test("a long message's claim is cut to 300 characters", () => {
    expect(claimOf("a".repeat(1000)).length).toBeLessThanOrEqual(300);
  });
});

describe("the services card's extra button", () => {
  test("Approve, don't ask again: allow once, then add the service", async () => {
    const calls: unknown[] = [];
    const r = render(
      <ServiceCardExtra
        specialistId="c1"
        requestId="req-2"
        tool="mcp__specialist_services__slack_post_message"
        actions={{ decidePermission: async (...a: unknown[]) => void calls.push(["decide", ...a]) } as never}
        load={async () => ({ services: [{ key: "slack", title: "Slack", kind: "connect", connected: true, allowed: false }] })}
        save={async (id, c) => (calls.push(["put", id, c]), { services: [] })}
      />,
    );
    await flush();
    await act(async () => {
      button(r, "Approve, don't ask again").props.onClick();
    });
    expect(calls).toEqual([
      ["decide", "req-2", "allow", "once"],
      ["put", "c1", { add: ["slack"] }],
    ]);
  });

  test("in a breakout: offered for a known read, never for a write", async () => {
    const rows = [
      {
        key: "slack",
        title: "Slack",
        kind: "connect",
        connected: true,
        allowed: false,
        tools: [
          { name: "mcp__specialist_services__slack_search", write: false },
          { name: "mcp__specialist_services__slack_post_message", write: true },
        ],
      },
    ];
    const card = (tool: string) =>
      render(
        <ServiceCardExtra
          specialistId="c1"
          requestId="req-3"
          tool={tool}
          kind="breakout"
          actions={{ decidePermission: async () => {} } as never}
          load={async () => ({ services: rows })}
        />,
      );
    const read = card("mcp__specialist_services__slack_search");
    const write = card("mcp__specialist_services__slack_post_message");
    await flush();
    expect(read.root.findAll((n) => n.type === "button")).toHaveLength(1);
    expect(write.root.findAll((n) => n.type === "button")).toHaveLength(0);
  });

  test("a tool of no known service draws nothing", async () => {
    const r = render(
      <ServiceCardExtra
        specialistId="c1"
        requestId="req-2"
        tool="mcp__specialist_services__zzz_x"
        actions={{ decidePermission: async () => {} } as never}
        load={async () => ({ services: [] })}
      />,
    );
    await flush();
    expect(r.root.findAll((n) => n.type === "button")).toHaveLength(0);
  });
});

describe("with the flag off nothing renders (A)", () => {
  let side: TaskSide | null = null;
  function Probe(props: { specialist?: { id: string; kind: "main" | "breakout" } }) {
    side = useSpecialistTaskSide({
      claude: true,
      sessionId: "s1",
      file: "/w/proj",
      turns: [user("u1", 100)],
      ...(props.specialist ? { specialist: props.specialist } : {}),
    });
    return null;
  }
  afterEach(() => resetSpecialistsFlagForTests());

  test("off: no cards seam, no chips, no dock", () => {
    publishSpecialistsEnabled(false);
    render(<Probe />);
    expect(side!.cards).toBeNull();
    expect(side!.afterTurn).toBeUndefined();
    expect(side!.dock).toBeNull();
    expect(side!.tail).toBeNull();
  });

  test("on, inside a specialist session: no task UI (a breakout cannot open a breakout)", () => {
    publishSpecialistsEnabled(true);
    setSpecialistListForTests(SPECS);
    render(<Probe specialist={{ id: "c1", kind: "breakout" }} />);
    expect(side!.afterTurn).toBeUndefined();
    expect(side!.dock).toBeNull();
    // The seam is there for the services card, but an offer row is not ours here.
    expect(side!.cards!.renderCard(offerRow(), fakeActions().actions)).toBeNull();
  });

  test("on, in a task: Send to a specialist after a message", () => {
    publishSpecialistsEnabled(true);
    setSpecialistListForTests(SPECS);
    render(<Probe />);
    expect(side!.afterTurn).toBeDefined();
    expect(side!.afterTurn!(user("u1", 100))).not.toBeNull();
  });
});
