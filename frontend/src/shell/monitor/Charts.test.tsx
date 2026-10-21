// AreaChart's path: smooth by default, step-after with `step`.
import { expect, test } from "bun:test";
import { act, create } from "react-test-renderer";

import { AreaChart } from "@shell/monitor/Charts";

const pts = [
  { t: 1, v: 0 },
  { t: 2, v: 4 },
  { t: 3, v: 2 },
];
const lineOf = (step?: boolean): string => {
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(<AreaChart points={pts} max={4} ticks={[]} windowS={3} label="x" step={step} />);
  });
  return r.root.find((n) => n.type === "path" && n.props.className === "monitor-line").props.d as string;
};

test("default line is point to point", () => {
  expect((lineOf().match(/L/g) ?? []).length).toBe(2);
});

test("step draws a horizontal then a vertical segment per reading", () => {
  const d = lineOf(true);
  // M(200,100) L(400,100) L(400,0) L(600,0) L(600,50)
  expect(d).toBe("M200.0,100.0L400.0,100.0L400.0,0.0L600.0,0.0L600.0,50.0");
});
