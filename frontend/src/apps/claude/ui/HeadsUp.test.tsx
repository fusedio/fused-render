import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const { HeadsUp } = await import("./HeadsUp");

const NOTE = {
  learn: "x",
  tag: "Heads up" as const,
  title: "Tests are skipped",
  bullets: ["The agent set **SKIP_TESTS**.", "Run the suite yourself."],
};

function render(node: React.ReactElement): ReactTestRenderer {
  let out!: ReactTestRenderer;
  act(() => {
    out = create(node);
  });
  return out;
}
function texts(r: ReactTestRenderer): string {
  return JSON.stringify(r.toJSON());
}

test("renders the tag, the title and every bullet, and nothing when there is no note", () => {
  const r = render(<HeadsUp note={NOTE} onGotIt={() => {}} onAsk={() => {}} onDismiss={() => {}} />);
  const s = texts(r);
  expect(s).toContain("Heads up");
  expect(s).toContain("Tests are skipped");
  expect(s).toContain("SKIP_TESTS"); // bold survives through the inline renderer
  expect(s).toContain("Run the suite yourself");
  const empty = render(<HeadsUp note={null} onGotIt={() => {}} onAsk={() => {}} onDismiss={() => {}} />);
  expect(empty.toJSON()).toBeNull();
});

test("the three buttons call their own callbacks", () => {
  const calls: string[] = [];
  const r = render(
    <HeadsUp
      note={NOTE}
      onGotIt={() => calls.push("got")}
      onAsk={() => calls.push("ask")}
      onDismiss={() => calls.push("dismiss")}
    />,
  );
  const buttons = r.root.findAll((n) => n.type === "button" && typeof n.props.onClick === "function");
  const labels = buttons.map((b) => b.props["aria-label"] ?? [b.props.children].flat().join(""));
  expect(labels).toEqual(["Got it", "Ask in chat", "Dismiss"]);
  for (const b of buttons) act(() => b.props.onClick());
  expect(calls).toEqual(["got", "ask", "dismiss"]);
});
