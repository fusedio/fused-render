import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";
import { useState } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const await_import = await import("./WorktreeMenu");
const { WorktreeMenuBody } = await_import;
const { worktreeRows } = await import("../protocol/worktrees");
import type { WorktreeList } from "../protocol/worktrees";

const LIST = (current: string, dirty = false): WorktreeList => ({
  root: "/r",
  current,
  branch_current: "main",
  worktrees: [
    { path: "/r", branch: "main", head: "a", name: "r", managed: false, is_main: true, dirty: false },
    { path: "/r/.claude/worktrees/feat", branch: "feat", head: "b", name: "feat", managed: true, is_main: false, dirty },
  ],
});

function mount(list: WorktreeList) {
  const switched: string[] = [];
  const created: string[] = [];
  let removes = 0;
  function Harness() {
    const [creating, setCreating] = useState<string | null>(null);
    const [confirming, setConfirming] = useState(false);
    return (
      <WorktreeMenuBody
        rows={worktreeRows(list)}
        onSwitch={(p) => switched.push(p)}
        creating={creating}
        onCreating={setCreating}
        onCreate={(n) => created.push(n)}
        confirming={confirming}
        onRemove={() => {
          if (confirming) removes += 1;
          else setConfirming(true);
        }}
      />
    );
  }
  let r: ReactTestRenderer | undefined;
  act(() => {
    r = create(<Harness />);
  });
  const root = r!.root;
  const buttons = () => root.findAll((n) => n.type === "button");
  const button = (text: string) => buttons().find((b) => label(b) === text)!;
  return { r: r!, root, buttons, button, switched, created, removes: () => removes };
}

describe("WorktreeMenuBody", () => {
  test("one row per worktree, the current one marked and inert", () => {
    const m = mount(LIST("/r"));
    const rows = m.root.findAll((n) => n.type === "button" && String(n.props.className).startsWith("chat-worktree-row") && n.props.title);
    expect(rows.map((b) => b.props.title)).toEqual(["/r", "/r/.claude/worktrees/feat"]);
    expect(rows.map((b) => b.props["aria-current"])).toEqual(["true", undefined]);
    expect(rows[0]!.props.onClick).toBeUndefined();
    act(() => rows[1]!.props.onClick());
    expect(m.switched).toEqual(["/r/.claude/worktrees/feat"]);
    // The main checkout is never removable.
    expect(m.buttons().some((b) => label(b) === "Remove this worktree")).toBe(false);
    act(() => m.r.unmount());
  });

  test("New worktree opens a field with the default name; Enter creates, Esc cancels", () => {
    const m = mount(LIST("/r"));
    act(() => m.button("New worktree…").props.onClick());
    const field = () => m.root.findByType("input");
    expect(field().props.value).toMatch(/^claude-\d{6}-\d{4}$/);
    act(() => field().props.onChange({ currentTarget: { value: "my-tree" } }));
    act(() => field().props.onKeyDown({ key: "Enter", preventDefault() {}, stopPropagation() {} }));
    expect(m.created).toEqual(["my-tree"]);
    // A bad name does not go.
    act(() => field().props.onChange({ currentTarget: { value: "a b" } }));
    act(() => field().props.onKeyDown({ key: "Enter", preventDefault() {}, stopPropagation() {} }));
    expect(m.created).toEqual(["my-tree"]);
    act(() => field().props.onKeyDown({ key: "Escape", preventDefault() {}, stopPropagation() {} }));
    expect(m.root.findAllByType("input")).toHaveLength(0);
    act(() => m.r.unmount());
  });

  test("remove asks first, and says so when the worktree is dirty", () => {
    const clean = mount(LIST("/r/.claude/worktrees/feat"));
    act(() => clean.button("Remove this worktree").props.onClick());
    expect(clean.removes()).toBe(0);
    act(() => clean.button("Confirm remove").props.onClick());
    expect(clean.removes()).toBe(1);
    act(() => clean.r.unmount());

    const dirty = mount(LIST("/r/.claude/worktrees/feat", true));
    act(() => dirty.button("Remove this worktree").props.onClick());
    expect(dirty.buttons().some((b) => label(b) === "Has uncommitted changes. Remove anyway")).toBe(true);
    act(() => dirty.r.unmount());
  });
});

function label(b: { props: { children?: unknown } }): string {
  return ([] as unknown[]).concat(b.props.children as unknown).filter((x) => typeof x === "string").join("");
}

describe("review fixes", () => {
  const { removeFlow, useWorktreeList } = await_import;

  test("remove asks the leave guard FIRST, and a 'stay' removes nothing", async () => {
    const order: string[] = [];
    const stay = await removeFlow("clean", {
      leave: async () => (order.push("leave"), false),
      remove: async () => (order.push("remove"), { removed: "/x" }),
    });
    expect(stay).toEqual({ stayed: true });
    expect(order).toEqual(["leave"]);
    const go = await removeFlow("clean", {
      leave: async () => (order.push("leave"), true),
      remove: async (force) => (order.push("remove:" + force), { removed: "/x" }),
    });
    expect(go).toEqual({ removed: "/x" });
    expect(order).toEqual(["leave", "leave", "remove:false"]);
  });

  test("unknown or dirty forces; a dirty refusal is flagged for the forced retry", async () => {
    const forces: boolean[] = [];
    const remove = async (force: boolean) => {
      forces.push(force);
      return force ? { removed: "/x" } : { error: "this worktree has uncommitted changes" };
    };
    expect(await removeFlow("unknown", { leave: async () => true, remove })).toEqual({ removed: "/x" });
    expect(await removeFlow("clean", { leave: async () => true, remove })).toEqual({
      error: "this worktree has uncommitted changes",
      dirty: true,
    });
    expect(forces).toEqual([true, false]);
  });

  test("a running turn refuses Remove, with a note", () => {
    let r: ReactTestRenderer | undefined;
    act(() => {
      r = create(
        <WorktreeMenuBody
          rows={worktreeRows(LIST("/r/.claude/worktrees/feat"))}
          onSwitch={() => {}}
          creating={null}
          onCreating={() => {}}
          onCreate={() => {}}
          confirming={false}
          onRemove={() => {
            throw new Error("must not run");
          }}
          running
        />,
      );
    });
    const btn = r!.root.findAll((n) => n.type === "button").find((b) => label(b) === "Remove this worktree")!;
    expect(btn.props.disabled).toBe(true);
    expect(btn.props.onClick).toBeUndefined();
    expect(r!.root.findAll((n) => n.props.className === "chat-worktree-note")).toHaveLength(1);
    act(() => r!.unmount());
  });

  test("the list hook drops an older answer and another folder's answer", async () => {
    const pending: { file: string; res(v: WorktreeList | { error: string }): void }[] = [];
    const lister = (file: string) =>
      new Promise<WorktreeList | { error: string }>((res) => pending.push({ file, res }));
    const ready: boolean[] = [];
    let seen: { list: WorktreeList | null; refresh(): Promise<void> } | null = null;
    function Probe({ file }: { file: string }) {
      seen = useWorktreeList(file, lister, (v) => ready.push(v));
      return null;
    }
    let r: ReactTestRenderer | undefined;
    await act(async () => {
      r = create(<Probe file="/r" />);
    });
    // A second call goes out; the FIRST answer lands last and must lose.
    await act(async () => void seen!.refresh());
    await act(async () => pending[1]!.res(LIST("/r/.claude/worktrees/feat")));
    await act(async () => pending[0]!.res(LIST("/r")));
    expect(seen!.list?.current).toBe("/r/.claude/worktrees/feat");
    // Another folder: the old folder's late answer does not land.
    await act(async () => r!.update(<Probe file="/other" />));
    await act(async () => void seen!.refresh());
    // pending[2] is the remount's own call, superseded by the refresh above.
    await act(async () => pending[3]!.res({ error: "not a git repository" }));
    await act(async () => pending[2]!.res(LIST("/r")));
    expect(seen!.list).toBeNull();
    expect(ready).toEqual([true, false]);
    act(() => r!.unmount());
    expect(ready[ready.length - 1]).toBe(false);
  });
});

describe("a chat on a file", () => {
  const FILE_LIST: WorktreeList = {
    root: "/r",
    current: "/r",
    branch_current: "main",
    relative: "src/app.py",
    worktrees: [
      { path: "/r", branch: "main", head: "a", name: "r", managed: false, is_main: true, dirty: false, mapped: { path: "/r/src/app.py", exists: true } },
      { path: "/r/.claude/worktrees/lacks", branch: "lacks", head: "c", name: "lacks", managed: true, is_main: false, dirty: false, mapped: { path: "/r/.claude/worktrees/lacks/src/app.py", exists: false } },
      { path: "/r/.claude/worktrees/has", branch: "has", head: "b", name: "has", managed: true, is_main: false, dirty: false, mapped: { path: "/r/.claude/worktrees/has/src/app.py", exists: true } },
    ],
  };

  test("rows switch to the mapped file, and say when it is not on the branch", () => {
    const switched: string[] = [];
    let r: ReactTestRenderer | undefined;
    act(() => {
      r = create(
        <WorktreeMenuBody
          rows={worktreeRows(FILE_LIST)}
          onSwitch={(p) => switched.push(p)}
          creating={null}
          onCreating={() => {}}
          onCreate={() => {}}
          confirming={false}
          onRemove={() => {}}
        />,
      );
    });
    const notes = r!.root.findAll((n) => n.props.className === "chat-worktree-miss");
    expect(notes).toHaveLength(1);
    expect(notes[0]!.props.children).toBe("opens the folder: file not on this branch");
    const rows = r!.root.findAll((n) => n.type === "button" && n.props.title && n.props.onClick);
    for (const b of rows) act(() => b.props.onClick());
    expect(switched).toEqual(["/r/.claude/worktrees/has/src/app.py", "/r/.claude/worktrees/lacks"]);
    act(() => r!.unmount());
  });

  test("the mount gate takes a file target; the list says repo for a file path", async () => {
    const { worktreeMenuTarget } = await import("../ClaudeChat");
    expect(worktreeMenuTarget("/r/src/app.py", "file")).toBe(true);
    expect(worktreeMenuTarget("/r", "folder")).toBe(true);
    expect(worktreeMenuTarget("/r/src/app.py", "")).toBe(false);
    expect(worktreeMenuTarget(null, "file")).toBe(false);

    const ready: boolean[] = [];
    let seen: { list: WorktreeList | null } | null = null;
    const asked: string[] = [];
    function Probe() {
      seen = await_import.useWorktreeList(
        "/r/src/app.py",
        async (f) => (asked.push(f), FILE_LIST),
        (v) => ready.push(v),
      );
      return null;
    }
    let r: ReactTestRenderer | undefined;
    await act(async () => {
      r = create(<Probe />);
    });
    expect(asked).toEqual(["/r/src/app.py"]);
    expect(seen!.list?.relative).toBe("src/app.py");
    expect(ready).toEqual([true]);
    act(() => r!.unmount());
  });
});
