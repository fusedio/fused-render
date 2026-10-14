// Where an answered card parks: after the chip it answered, in ANY turn.
//
// Bug: a long run split into several assistant turns by mid-run follow-ups
// stamped `parkedIn` from the newest turn, so an answered AskUserQuestion whose
// chip lived in an earlier turn went to the newest turn's tail.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const { parkPlan } = await import("./Transcript");
const { CardStack } = await import("./CardStack");

const tool = (id: string, name: string) => ({ kind: "tool", id, name, input: {}, status: "done", output: "", images: [] });
const A = { role: "assistant", key: "A", text: "", segments: [tool("toolu_q", "AskUserQuestion"), tool("toolu_r", "Read")] };
const U = { role: "user", key: "U", text: "follow-up" };
const B = { role: "assistant", key: "B", text: "", segments: [tool("toolu_s", "SendMessage"), tool("toolu_a", "Agent")] };
const turns = [A, U, B] as never;

const row = (over: object) =>
  ({ id: "q1", tool: "AskUserQuestion", input: {}, created_at: 1, decision: "allow", scope: "", mode: "", answers: {}, ...over }) as never;

describe("parkPlan", () => {
  test("a card whose chip is in an earlier turn sits after that chip, not in the stamped turn's tail", () => {
    const plan = parkPlan(turns, [row({ toolUseId: "toolu_q", parkedIn: "B" })]);
    expect(plan.get("A")?.after.get(0)).toEqual(["q1"]);
    expect(plan.get("B")?.tail ?? []).toEqual([]);
    expect(plan.get("B")?.after.size ?? 0).toBe(0);
  });
  test("a decided card with no parkedIn still finds its chip", () => {
    const plan = parkPlan(turns, [row({ toolUseId: "toolu_s" })]);
    expect(plan.get("B")?.after.get(0)).toEqual(["q1"]);
  });
  test("no chip anywhere: the stamped turn's tail, as before; no stamp: not planned", () => {
    expect(parkPlan(turns, [row({ toolUseId: "nope", parkedIn: "B" })]).get("B")?.tail).toEqual(["q1"]);
    expect(parkPlan(turns, [row({ toolUseId: "nope" })]).size).toBe(0);
  });
  test("name-match fallback stays inside the stamped turn", () => {
    const plan = parkPlan(turns, [row({ id: "p1", tool: "Agent", parkedIn: "B" })]);
    expect(plan.get("B")?.after.get(1)).toEqual(["p1"]);
  });
});

describe("CardStack placements agree with parkPlan", () => {
  const mounted: ReactTestRenderer[] = [];
  afterEach(() => {
    for (const r of mounted.splice(0)) act(() => r.unmount());
  });
  const actions = { decidePermission: () => {}, answerQuestion: () => {}, decidePlan: () => {}, dismissCard: () => {} } as never;
  const cards = (el: React.ReactElement) => {
    let r!: ReactTestRenderer;
    act(() => {
      r = create(el);
    });
    mounted.push(r);
    return r.root.findAll((n) => typeof n.type === "function" && n.props.row?.id).map((n) => n.props.row.id as string);
  };
  const decided = row({ toolUseId: "toolu_q", parkedIn: "B" });
  test("parked placement draws exactly the ids it is given, whatever parkedIn says", () => {
    expect(cards(<CardStack rows={[decided]} ids={["q1"]} placement="parked" turnKey="A" actions={actions} />)).toEqual(["q1"]);
  });
  test("open placement leaves out a card placed inline", () => {
    const noStamp = row({ toolUseId: "toolu_q" });
    expect(cards(<CardStack rows={[noStamp]} placement="open" actions={actions} />)).toEqual(["q1"]);
    expect(cards(<CardStack rows={[noStamp]} placed={new Set(["q1"])} placement="open" actions={actions} />)).toEqual([]);
  });
  test("an unanswered card stays in the open stack", () => {
    const open = row({ decision: "", toolUseId: "toolu_q" });
    expect(cards(<CardStack rows={[open]} placed={new Set(["q1"])} placement="open" actions={actions} />)).toEqual(["q1"]);
  });
});
