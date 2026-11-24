import { afterEach, describe, expect, test } from "bun:test";
import {
  closeAgentPeek,
  findAgentSegment,
  getAgentPeeks,
  getPeekTurns,
  hasAgentPeeks,
  openAgentPeek,
  publishPeekTurns,
  resetAgentPeeks,
} from "./agent-peeks";

afterEach(resetAgentPeeks);

const req = (over: Record<string, string> = {}) => ({
  sessionId: "s1",
  file: "/w",
  toolUseId: "tu1",
  title: "Explore",
  subtitle: "Explore · running",
  ...over,
});
const turns = [
  {
    role: "assistant",
    key: "a1",
    text: "",
    segments: [
      { kind: "text", text: "hi" },
      { kind: "tool", id: "tu1", name: "Agent", status: "running", children: [{ kind: "text", text: "inner" }] },
      { kind: "tool", id: "tu2", name: "Agent", orphan: true, status: "ok" },
    ],
  },
] as never;

describe("agent peeks", () => {
  test("open dedupes on session + tool use", () => {
    const a = openAgentPeek(req());
    expect(openAgentPeek(req())).toBe(a);
    expect(openAgentPeek(req({ toolUseId: "tu2" }))).not.toBe(a);
    expect(openAgentPeek(req({ sessionId: "s2" }))).not.toBe(a);
    expect(getAgentPeeks().length).toBe(3);
  });

  test("close drops the session's turns only with its last peek", () => {
    const a = openAgentPeek(req());
    const b = openAgentPeek(req({ toolUseId: "tu2" }));
    publishPeekTurns("s1", turns);
    expect(getPeekTurns("s1")).toBe(turns);
    closeAgentPeek(a);
    expect(getPeekTurns("s1")).toBe(turns);
    closeAgentPeek(b);
    expect(getPeekTurns("s1")).toBeUndefined();
    expect(hasAgentPeeks("s1")).toBe(false);
  });

  test("publish without an open peek stores nothing", () => {
    publishPeekTurns("s1", turns);
    expect(getPeekTurns("s1")).toBeUndefined();
  });

  test("findAgentSegment finds the call, including an orphan parent", () => {
    expect(findAgentSegment(turns, "tu1")?.children?.length).toBe(1);
    expect(findAgentSegment(turns, "tu2")?.orphan).toBe(true);
    expect(findAgentSegment(turns, "nope")).toBeNull();
    expect(findAgentSegment(undefined, "tu1")).toBeNull();
  });
});
