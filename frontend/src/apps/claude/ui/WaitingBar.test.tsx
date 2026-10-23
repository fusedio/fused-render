import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";

import type { PermissionRow } from "../protocol/types";
import { ComposerGate, WaitingBar, waitingKind } from "./WaitingBar";

const mounted: Array<ReturnType<typeof create>> = [];
function mount(el: React.ReactElement) {
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(el);
  });
  mounted.push(r);
  return r;
}
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

const row = (tool: string, decision: unknown = ""): PermissionRow =>
  ({ id: tool + decision, tool, input: {}, created_at: 0, decision, scope: "", mode: "" }) as unknown as PermissionRow;

function Composer() {
  return <textarea data-testid="composer" />;
}

test("kind is derived from the newest open row", () => {
  expect(waitingKind([])).toBeNull();
  expect(waitingKind([row("AskUserQuestion", "allow")])).toBeNull();
  expect(waitingKind([row("AskUserQuestion")])).toBe("question");
  expect(waitingKind([row("ExitPlanMode")])).toBe("plan");
  expect(waitingKind([row("AskUserQuestion"), row("Bash")])).toBe("permission");
});

test("open card: bar present, composer hidden but still mounted", () => {
  const r = mount(
    <ComposerGate kind="question" agentName="Claude" showJump={false} onJump={() => {}}>
      <Composer />
    </ComposerGate>,
  );
  const json = r.toJSON() as ReactTestRendererJSON[];
  expect(JSON.stringify(json)).toContain("Claude is waiting for your answer");
  expect(r.root.findAllByType(Composer).length).toBe(1);
  expect(r.root.findByProps({ className: "composer-gate" }).props.hidden).toBe(true);
});

test("no open card: composer visible, no bar", () => {
  const r = mount(
    <ComposerGate kind={null}>
      <Composer />
    </ComposerGate>,
  );
  expect(r.root.findAllByProps({ role: "status" }).length).toBe(0);
  expect(r.root.findByProps({ className: "composer-gate" }).props.hidden).toBe(false);
});

test("jump button only with showJump, and it calls onJump", () => {
  let n = 0;
  const hidden = mount(<WaitingBar kind="plan" agentName="Claude" showJump={false} onJump={() => n++} />);
  expect(hidden.root.findAllByType("button").length).toBe(0);
  const shown = mount(<WaitingBar kind="plan" agentName="Claude" showJump onJump={() => n++} />);
  const btn = shown.root.findByType("button");
  expect(JSON.stringify(shown.toJSON())).toContain("Jump to plan");
  act(() => btn.props.onClick());
  expect(n).toBe(1);
});
