// A reply's link to a LOCAL file — `/Users/…/SKILL.md`, `~/x`, `file:///…` —
// opens in the preview overlay like a tool chip's path does, and in the viewer
// in a new tab where no overlay exists. Before this the href either vanished
// (DOMPurify drops `file:`) or 404'd under the shell's origin: nothing opened,
// nothing said why.
//
// `react-test-renderer`: no DOM, so the click arrives as a synthetic target
// whose `closest` answers the way the rendered anchor would.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { act, create } from "react-test-renderer";

import { FilePreviewContext } from "./filePreview";
import { MarkdownView, PATH_ACTIVATE_SELECTOR } from "./MarkdownView";

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

/** The event a click on `<a data-path=…>` (or on text inside it) delivers. */
function clickOn(attrs: Record<string, string>, matches = true, mods: Record<string, unknown> = {}) {
  const el = { getAttribute: (k: string) => attrs[k] ?? null };
  let prevented = 0;
  const ev = {
    target: { closest: (sel: string) => (matches && sel === PATH_ACTIVATE_SELECTOR ? el : null) },
    preventDefault: () => void (prevented += 1),
    ...mods,
  };
  return { ev, prevented: () => prevented };
}

function root(r: ReturnType<typeof create>) {
  return r.root.findAll((n) => n.type === "div" && !!n.props.dangerouslySetInnerHTML)[0]!;
}

test("inside a chat, a local-file link opens the preview overlay with `~` resolved", () => {
  const opened: string[] = [];
  const r = mount(
    <FilePreviewContext.Provider value={(p) => void opened.push(p)}>
      <MarkdownView text="see [spec](~/.claude/skills/x/SKILL.md)" enhance={false} cwd="/canvas" />
    </FilePreviewContext.Provider>,
  );
  const { ev, prevented } = clickOn({ "data-path": "~/.claude/skills/x/SKILL.md" });
  act(() => root(r).props.onClick(ev));
  // `useHome` has not answered in this suite: `~` stays, as resolveAgainst documents.
  expect(opened).toEqual(["~/.claude/skills/x/SKILL.md"]);
  expect(prevented()).toBe(1);
});

test("an absolute path outside the chat's folder is opened as it is, never joined to cwd", () => {
  const opened: string[] = [];
  const r = mount(
    <FilePreviewContext.Provider value={(p) => void opened.push(p)}>
      <MarkdownView text="x" enhance={false} cwd="/Users/u/Fused/canvas/custom/T" />
    </FilePreviewContext.Provider>,
  );
  const { ev } = clickOn({ "data-path": "/Users/u/.claude/skills/x/SKILL.md" });
  act(() => root(r).props.onClick(ev));
  expect(opened).toEqual(["/Users/u/.claude/skills/x/SKILL.md"]);
});

test("without an opener the viewer opens in a new tab instead of a dead click", () => {
  const opens: string[] = [];
  const w = globalThis as unknown as { window?: { open?: (u: string) => void } };
  const before = w.window;
  w.window = { ...(before as object), open: (u: string) => void opens.push(u) } as typeof before;
  try {
    const r = mount(<MarkdownView text="x" enhance={false} cwd="/canvas" />);
    const { ev, prevented } = clickOn({ "data-path": "/Users/u/a b.md" });
    act(() => root(r).props.onClick(ev));
    expect(opens).toEqual(["/render?path=" + encodeURIComponent("/Users/u/a b.md")]);
    expect(prevented()).toBe(1);
  } finally {
    w.window = before;
  }
});

test("a click anywhere else is left to the browser", () => {
  const opened: string[] = [];
  const r = mount(
    <FilePreviewContext.Provider value={(p) => void opened.push(p)}>
      <MarkdownView text="[web](https://x.test)" enhance={false} />
    </FilePreviewContext.Provider>,
  );
  const { ev, prevented } = clickOn({}, false);
  act(() => root(r).props.onClick(ev));
  expect(opened).toEqual([]);
  expect(prevented()).toBe(0);
});

/** Swap `window.open` for a recorder for the length of `fn`. */
function withWindowOpen(fn: (opens: string[]) => void) {
  const opens: string[] = [];
  const w = globalThis as unknown as { window?: { open?: (u: string) => void } };
  const before = w.window;
  w.window = { ...(before as object), open: (u: string) => void opens.push(u) } as typeof before;
  try {
    fn(opens);
  } finally {
    w.window = before;
  }
}

test("Cmd, Ctrl or Shift-click opens the viewer in a tab, not the overlay (Bugbot)", () => {
  for (const mod of ["metaKey", "ctrlKey", "shiftKey"]) {
    withWindowOpen((opens) => {
      const opened: string[] = [];
      const r = mount(
        <FilePreviewContext.Provider value={(p) => void opened.push(p)}>
          <MarkdownView text="x" enhance={false} cwd="/canvas" />
        </FilePreviewContext.Provider>,
      );
      const { ev, prevented } = clickOn({ "data-path": "/Users/u/a.md" }, true, { [mod]: true });
      act(() => root(r).props.onClick(ev));
      expect(opened).toEqual([]);
      expect(opens).toEqual(["/render?path=" + encodeURIComponent("/Users/u/a.md")]);
      expect(prevented()).toBe(1);
    });
  }
});

test("a middle click opens the tab too; other aux buttons are left alone", () => {
  withWindowOpen((opens) => {
    const opened: string[] = [];
    const r = mount(
      <FilePreviewContext.Provider value={(p) => void opened.push(p)}>
        <MarkdownView text="x" enhance={false} cwd="/canvas" />
      </FilePreviewContext.Provider>,
    );
    const right = clickOn({ "data-path": "/Users/u/a.md" }, true, { button: 2 });
    act(() => root(r).props.onAuxClick(right.ev));
    expect(opens).toEqual([]);
    expect(right.prevented()).toBe(0);
    const middle = clickOn({ "data-path": "/Users/u/a.md" }, true, { button: 1 });
    act(() => root(r).props.onAuxClick(middle.ev));
    expect(opened).toEqual([]);
    expect(opens).toEqual(["/render?path=" + encodeURIComponent("/Users/u/a.md")]);
    expect(middle.prevented()).toBe(1);
  });
});

