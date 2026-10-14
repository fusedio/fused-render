// The file preview overlay and the doors into it: the modal's iframe URL and
// missing-file state, a tool chip's path (opens the overlay, never toggles the
// chip), and an artifact row (overlay inside a chat, new tab outside one).
// Esc is the chassis' own wiring and the shim's `document` cannot deliver a
// keydown (see PlanModal.test.tsx), so ✕ stands in for "closes".
import {
  installDomShim,
  installPortalContainer,
  removePortalContainer,
} from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestInstance } from "react-test-renderer";

import { ArtifactRow } from "./ArtifactRow";
import { FilePreviewContext, FilePreviewProvider, useFilePreview } from "./filePreview";
import { FilePreviewModal } from "./FilePreviewModal";
import { PathLabel } from "./ToolChip";

const mounted: Array<ReturnType<typeof create>> = [];
function mount(el: React.ReactElement) {
  installPortalContainer();
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(el);
  });
  mounted.push(r);
  return r;
}
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  removePortalContainer();
});

const tick = () => act(async () => void (await new Promise((res) => setTimeout(res, 0))));
const byType = (r: ReturnType<typeof create>, t: string) => r.root.findAll((n) => n.type === t);
const textOf = (n: ReactTestInstance | string): string =>
  typeof n === "string" ? n : n.children.map((c) => textOf(c as ReactTestInstance | string)).join("");

test("the modal frames /render for the path once it exists", async () => {
  const r = mount(<FilePreviewModal path="/p/my file.html" onClose={() => {}} stat={async () => ({})} />);
  await tick();
  const frames = byType(r, "iframe");
  expect(frames).toHaveLength(1);
  expect(frames[0].props.src).toBe("/render?path=" + encodeURIComponent("/p/my file.html"));
  expect(textOf(byType(r, "h2")[0])).toContain("my file.html");
});

test("a missing file says so instead of framing", async () => {
  const r = mount(
    <FilePreviewModal path="/p/gone.html" onClose={() => {}} stat={() => Promise.reject(new Error("404"))} />,
  );
  await tick();
  expect(byType(r, "iframe")).toHaveLength(0);
  expect(textOf(r.root.findAll((n) => n.props.className === "c-fp-missing")[0])).toBe(
    "File no longer exists",
  );
});

test("the ✕ calls onClose", async () => {
  let closed = 0;
  const r = mount(<FilePreviewModal path="/p/a.html" onClose={() => closed++} stat={async () => ({})} />);
  await tick();
  const x = byType(r, "button").find((b) => String(b.props.className).includes("modal-close"))!;
  act(() => x.props.onClick({ preventDefault() {}, stopPropagation() {} }));
  await act(async () => void (await new Promise((res) => setTimeout(res, 160))));
  expect(closed).toBe(1);
});

test("a path label opens the preview and does not toggle the chip", () => {
  const opened: string[] = [];
  let bubbled = 0;
  const r = mount(
    <FilePreviewContext.Provider value={(p) => opened.push(p)}>
      <PathLabel value="/p/a.ts" />
    </FilePreviewContext.Provider>,
  );
  const btn = byType(r, "button")[0];
  act(() =>
    btn.props.onClick({
      stopPropagation() {
        bubbled--;
      },
    }),
  );
  expect(opened).toEqual(["/p/a.ts"]);
  expect(bubbled).toBe(-1); // stopPropagation was called
});

test("a path label with no opener stays a plain label", () => {
  const r = mount(<PathLabel value="/p/a.ts" />);
  expect(byType(r, "button")).toHaveLength(0);
  expect(byType(r, "div")).toHaveLength(1);
});

const art = { remote_url: "https://x.test/a", file_path: "/p/a.html", exists: true as const };

test("an artifact row opens the overlay inside a chat", () => {
  const opened: string[] = [];
  const r = mount(
    <FilePreviewContext.Provider value={(p) => opened.push(p)}>
      <ArtifactRow artifact={art} />
    </FilePreviewContext.Provider>,
  );
  act(() => byType(r, "div")[0].props.onClick());
  expect(opened).toEqual(["/p/a.html"]);
});

test("an artifact row with no provider still window.opens", () => {
  const calls: unknown[][] = [];
  const w = globalThis as unknown as { window: { open?: unknown } };
  const prev = w.window.open;
  w.window.open = (...a: unknown[]) => calls.push(a);
  try {
    const r = mount(<ArtifactRow artifact={art} />);
    act(() => byType(r, "div")[0].props.onClick());
  } finally {
    w.window.open = prev;
  }
  expect(calls).toHaveLength(1);
  expect(String(calls[0][0])).toContain("/explorer/view/p/a.html");
});

test("a disabled provider (the landing) leaves artifact rows on window.open", () => {
  const calls: unknown[][] = [];
  const w = globalThis as unknown as { window: { open?: unknown } };
  const prev = w.window.open;
  w.window.open = (...a: unknown[]) => calls.push(a);
  try {
    const r = mount(
      <FilePreviewProvider cwd="/p" enabled={false}>
        <ArtifactRow artifact={art} />
      </FilePreviewProvider>,
    );
    act(() => byType(r, "div")[0].props.onClick());
  } finally {
    w.window.open = prev;
  }
  expect(calls).toHaveLength(1);
});

test("a gone file falls back to the hosted page even inside a chat", () => {
  const opened: string[] = [];
  const calls: unknown[][] = [];
  const w = globalThis as unknown as { window: { open?: unknown } };
  const prev = w.window.open;
  w.window.open = (...a: unknown[]) => calls.push(a);
  try {
    const r = mount(
      <FilePreviewContext.Provider value={(p) => opened.push(p)}>
        <ArtifactRow artifact={{ ...art, exists: false }} />
      </FilePreviewContext.Provider>,
    );
    act(() => byType(r, "div")[0].props.onClick());
  } finally {
    w.window.open = prev;
  }
  expect(opened).toEqual([]);
  expect(calls[0][0]).toBe("https://x.test/a");
});

function Opener({ p }: { p: string }) {
  const open = useFilePreview();
  return <button onClick={() => open?.(p)} />;
}
const modals = (r: ReturnType<typeof create>) => r.root.findAllByType(FilePreviewModal);

test("leaving the chat (enabled off) drops the open preview", () => {
  const tree = (enabled: boolean) => (
    <FilePreviewProvider cwd="/p" enabled={enabled} sessionKey="s1">
      <Opener p="/p/a.html" />
    </FilePreviewProvider>
  );
  const r = mount(tree(true));
  act(() => byType(r, "button")[0].props.onClick());
  expect(modals(r)).toHaveLength(1);
  act(() => r.update(tree(false)));
  act(() => r.update(tree(true)));
  expect(modals(r)).toHaveLength(0);
});

test("a different session drops the open preview", () => {
  const tree = (sessionKey: string) => (
    <FilePreviewProvider cwd="/p" sessionKey={sessionKey}>
      <Opener p="/p/a.html" />
    </FilePreviewProvider>
  );
  const r = mount(tree("s1"));
  act(() => byType(r, "button")[0].props.onClick());
  expect(modals(r)).toHaveLength(1);
  act(() => r.update(tree("s2")));
  expect(modals(r)).toHaveLength(0);
});
