// A tool auto mode's classifier refused: the chip says why and offers the one
// button that lets that call run. react-test-renderer, the chip-copy pattern.
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";

import { PERMISSION_HINTS } from "@platform/lib/permission-modes";

import { AllowDeniedContext, type AllowDenied } from "./allowDeniedContext";
import { createCardPolicy, CardPolicyProvider } from "./cardPolicy";
import { ToolChip } from "./ToolChip";
import type { ToolSegment } from "../protocol/types";

type Json = ReactTestRendererJSON;

const mounted: Array<ReturnType<typeof create>> = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

function mount(seg: Partial<ToolSegment>, allow: AllowDenied | null) {
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(
      <CardPolicyProvider value={createCardPolicy()}>
        <AllowDeniedContext.Provider value={allow}>
          <ToolChip
            seg={
              {
                kind: "tool",
                id: "t1",
                name: "Bash",
                status: "error",
                input: { command: "git push --force" },
                output: "refused",
                images: [],
                denied: { reason: "Git Destructive" },
                ...seg,
              } as ToolSegment
            }
            cardKey="k"
          />
        </AllowDeniedContext.Provider>
      </CardPolicyProvider>,
    );
  });
  mounted.push(r);
  return r;
}

function find(node: Json | Json[] | null, pred: (n: Json) => boolean): Json[] {
  const out: Json[] = [];
  const walk = (n: Json | string | null) => {
    if (!n || typeof n === "string") return;
    if (pred(n)) out.push(n);
    for (const k of n.children ?? []) walk(k as Json | string);
  };
  for (const n of Array.isArray(node) ? node : [node]) walk(n);
  return out;
}
const cls = (c: string) => (n: Json) => (n.props as { className?: string }).className === c;
const text = (n: Json | Json[] | null): string =>
  (Array.isArray(n) ? n : [n]).map((x) => flat(x)).join("");
function flat(n: Json | string | null): string {
  if (!n) return "";
  if (typeof n === "string") return n;
  return (n.children ?? []).map((k) => flat(k as Json | string)).join("");
}
const button = (r: ReturnType<typeof create>) => find(r.toJSON(), cls("chip-denied-btn"))[0];

test("a denied chip says why and offers Allow", () => {
  const r = mount({}, async () => {});
  expect(text(find(r.toJSON(), cls("chip-denied-why")))).toBe("Blocked by auto mode · Git Destructive");
  const b = button(r)!;
  expect(text(b)).toBe("Allow");
  expect((b.props as { disabled?: boolean }).disabled).toBe(false);
});

test("a refusal that gave no reason still reads as one", () => {
  const r = mount({ denied: { reason: "" } }, null);
  expect(text(find(r.toJSON(), cls("chip-denied-why")))).toBe("Blocked by auto mode");
});

test("no button where nothing can be sent (history only, another agent)", () => {
  const r = mount({}, null);
  expect(find(r.toJSON(), cls("chip-denied-why"))).toHaveLength(1);
  expect(button(r)).toBeUndefined();
});

test("a chip that was not refused has no denied line", () => {
  const r = mount({ denied: undefined }, async () => {});
  expect(find(r.toJSON(), cls("chip-denied"))).toHaveLength(0);
});

test("clicking Allow asks for THIS call by its tool_use id", async () => {
  const calls: string[] = [];
  const r = mount({}, async (id) => {
    calls.push(id);
  });
  await act(async () => {
    (button(r)!.props as { onClick: () => void }).onClick();
  });
  expect(calls).toEqual(["t1"]);
});

test("an approved call reads Allowed and cannot be clicked again", () => {
  const r = mount({ denied: { reason: "Git Destructive", allowed: true } }, async () => {});
  const b = button(r)!;
  expect(text(b)).toBe("Allowed");
  expect((b.props as { disabled?: boolean }).disabled).toBe(true);
});

test("an approved call reads Allowed even where nothing can be sent", () => {
  const r = mount({ denied: { reason: "X", allowed: true } }, null);
  expect(text(button(r)!)).toBe("Allowed");
  expect((button(r)!.props as { disabled?: boolean }).disabled).toBe(true);
});

test("a failed allow says so and keeps the button", async () => {
  const r = mount({}, async () => {
    throw new Error("no live session");
  });
  await act(async () => {
    (button(r)!.props as { onClick: () => void }).onClick();
  });
  expect(text(find(r.toJSON(), cls("chip-denied-err")))).toBe("no live session");
  expect(text(button(r)!)).toBe("Allow");
});

test("the Auto hint says risky actions are refused and can be allowed from the chat", () => {
  expect(PERMISSION_HINTS.auto).toContain("refuses risky ones");
  expect(PERMISSION_HINTS.auto).toContain("allow a refused action from the chat");
  expect(PERMISSION_HINTS.auto).not.toContain("holds the rest");
});
