import { expect, test } from "bun:test";
import { create, type ReactTestRendererJSON } from "react-test-renderer";
import StatusChip from "./StatusChip";

function render(el: React.ReactElement): ReactTestRendererJSON {
  return create(el).toJSON() as ReactTestRendererJSON;
}
function find(node: ReactTestRendererJSON, cls: string): ReactTestRendererJSON | undefined {
  const kids = (node.children ?? []).filter(
    (c): c is ReactTestRendererJSON => typeof c !== "string",
  );
  for (const k of kids) {
    if (String(k.props.className ?? "").split(" ").includes(cls)) return k;
    const deeper = find(k, cls);
    if (deeper) return deeper;
  }
  return undefined;
}

const NOOP = () => {};

test("idle: muted label, no numeral, no progress line", () => {
  const b = render(<StatusChip label="Models" open={false} title="Show models" onClick={NOOP} />);
  expect(b.type).toBe("button");
  expect(b.props.className).toContain("is-idle");
  expect(find(b, "sc-num")).toBeUndefined();
  expect(find(b, "sc-progress")).toBeUndefined();
  expect(find(b, "dl-summary")?.children).toEqual(["Models"]);
});

test("a count renders as one numeral beside the label and lifts the muting", () => {
  const b = render(
    <StatusChip label="Models" count={2} open={false} title="Show models" onClick={NOOP} />,
  );
  expect(b.props.className).not.toContain("is-idle");
  expect(find(b, "sc-num")?.children).toEqual(["2"]);
});

test("failure tone keeps the red rule; a plain count carries no tone class of its own", () => {
  const plain = render(
    <StatusChip label="Notifications" count={3} open={false} title="t" onClick={NOOP} />,
  );
  expect(plain.props.className).not.toContain("is-idle");
  expect(plain.props.className).not.toContain("is-failure");
  const fail = render(
    <StatusChip label="Notifications" count={1} tone="failure" open={false} title="t" onClick={NOOP} />,
  );
  expect(fail.props.className).toContain("is-failure");
});

test("a fraction draws the line at that width; null sweeps; undefined draws nothing", () => {
  const half = render(
    <StatusChip label="Erasing" progress={0.5} open={false} title="t" onClick={NOOP} />,
  );
  expect(find(half, "sc-progress-fill")?.props.style).toEqual({ width: "50%" });
  const sweep = render(
    <StatusChip label="Erasing" progress={null} open={false} title="t" onClick={NOOP} />,
  );
  expect(find(sweep, "sc-progress-fill")?.props.className).toContain("is-indeterminate");
  const none = render(<StatusChip label="Activity" open={false} title="t" onClick={NOOP} />);
  expect(find(none, "sc-progress")).toBeUndefined();
});

test("open and pinned are exposed for styling and assistive tech", () => {
  const b = render(
    <StatusChip label="Models" open pinned title="Hide models" onClick={NOOP} ariaLabel="Models, none loaded" />,
  );
  expect(b.props["aria-expanded"]).toBe(true);
  expect(b.props.className).toContain("is-pinned");
  expect(b.props["aria-label"]).toBe("Models, none loaded");
});
