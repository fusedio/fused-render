import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const { ComposerCard } = await import("./Composer");
const { acceptText, menuKey, menuQuery, CommandMenu } = await import("./CommandMenu");
const { UserWords } = await import("./Turn");
const { DEFAULT_EFFORT, DEFAULT_MODEL, DEFAULT_PERMISSION } = await import("./composer-defaults");
const { resetDraftSyncers } = await import("@platform/lib/drafts");
const { LOCAL_COMMANDS } = await import("../protocol/commands");
import type { ClassifiedCommand } from "../protocol/commands";

describe("the menu's keyboard model", () => {
  test("open only on a bare first token", () => {
    expect(menuQuery("/")).toBe("");
    expect(menuQuery("/com")).toBe("com");
    expect(menuQuery("/plugin:x")).toBe("plugin:x");
    expect(menuQuery("/com ")).toBeNull();
    expect(menuQuery("/compact now")).toBeNull();
    expect(menuQuery(" /com")).toBeNull();
    expect(menuQuery("com")).toBeNull();
    expect(menuQuery("")).toBeNull();
  });

  test("arrows wrap, Enter/Tab accept, Esc closes, Shift+Enter is the box's", () => {
    expect(menuKey("ArrowDown", 0, 3)).toEqual({ type: "move", index: 1 });
    expect(menuKey("ArrowDown", 2, 3)).toEqual({ type: "move", index: 0 });
    expect(menuKey("ArrowUp", 0, 3)).toEqual({ type: "move", index: 2 });
    expect(menuKey("Enter", 1, 3)).toEqual({ type: "accept" });
    expect(menuKey("Tab", 1, 3)).toEqual({ type: "accept" });
    expect(menuKey("Escape", 1, 3)).toEqual({ type: "close" });
    expect(menuKey("Enter", 1, 3, true)).toBeNull();
    expect(menuKey("a", 1, 3)).toBeNull();
    expect(menuKey("Enter", 0, 0)).toBeNull();
  });

  test("accepting leaves `/name ` with the space", () => {
    expect(acceptText("compact")).toBe("/compact ");
  });

  test("rows: bold name, muted description, a local tag on local ones", () => {
    let r: ReactTestRenderer | undefined;
    const picked: string[] = [];
    act(() => {
      r = create(
        <CommandMenu
          id="m"
          active={1}
          rows={[
            { name: "clear", description: "New", hint: "", local: true },
            { name: "compact", description: "Squash", hint: "", local: false },
          ]}
          onPick={(n) => picked.push(n)}
        />,
      );
    });
    const opts = r!.root.findAll((n) => n.props.role === "option" && typeof n.type === "string");
    expect(opts.map((o) => o.props["aria-selected"])).toEqual([false, true]);
    expect(r!.root.findAll((n) => n.props.className === "chat-cmd-tag")).toHaveLength(1);
    act(() => opts[1]!.props.onClick());
    expect(picked).toEqual(["compact"]);
    act(() => r!.unmount());
  });
});

describe("the transcript chip", () => {
  test("the first token is a chip and the text joins back exactly", () => {
    let r: ReactTestRenderer | undefined;
    act(() => {
      r = create(<UserWords text={"/review the diff"} />);
    });
    const chip = r!.root.findByProps({ className: "chat-cmd-chip" });
    expect(chip.props.children).toBe("/review");
    expect((r!.toJSON() as unknown as unknown[]).flat().map((n) =>
      typeof n === "string" ? n : (n as { children: string[] }).children.join(""),
    ).join("")).toBe("/review the diff");
    act(() => r!.unmount());
  });
});

// ---- the composer --------------------------------------------------------

const realFetch = globalThis.fetch;
beforeEach(() => {
  (globalThis as { fetch: unknown }).fetch = () => Promise.resolve(new Response("{}", { status: 200 }));
});
const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  (globalThis as { fetch: unknown }).fetch = realFetch;
  resetDraftSyncers();
});

function mount(answer: (cmd: ClassifiedCommand) => boolean | void = () => true) {
  const sent: string[] = [];
  const locals: ClassifiedCommand[] = [];
  let renderer: ReactTestRenderer | undefined;
  act(() => {
    renderer = create(
      <ComposerCard
        variant="chat"
        file="/p"
        sessionId=""
        controls={{
          model: DEFAULT_MODEL,
          effort: DEFAULT_EFFORT,
          permission: DEFAULT_PERMISSION,
          setModel() {},
          setEffort() {},
          setPermission() {},
        }}
        status="idle"
        back="/explorer/view/p"
        onSend={(text) => sent.push(text)}
        onStop={() => {}}
        slashCommands={[{ name: "compact", description: "Compact the conversation", source: "builtin" }]}
        onLocalCommand={(cmd) => {
          locals.push(cmd);
          return answer(cmd);
        }}
      />,
    );
  });
  mounted.push(renderer!);
  const root = renderer!.root;
  const box = () => root.findByType("textarea");
  const type = (value: string) => act(() => box().props.onChange({ currentTarget: { value } }));
  const press = (key: string, shiftKey = false, isComposing = false) => {
    let prevented = false;
    act(() => {
      box().props.onKeyDown({
        key,
        shiftKey,
        nativeEvent: { isComposing },
        preventDefault() {
          prevented = true;
        },
        stopPropagation() {},
      });
    });
    return prevented;
  };
  const rows = () =>
    root.findAll((n) => n.props.role === "option" && typeof n.type === "string").map((n) => n.props["data-index"] as number);
  const names = () =>
    root.findAll((n) => n.props.className === "chat-cmd-name").map((n) => (n.props.children as string[]).join(""));
  const active = () =>
    root.findAll((n) => n.props.role === "option" && typeof n.type === "string").findIndex((n) => n.props["aria-selected"]);
  return { box, type, press, rows, names, active, sent, locals };
}

describe("composer: the slash menu", () => {
  test("`/` opens it with every command; typing filters; a space closes", () => {
    const c = mount();
    expect(c.rows()).toHaveLength(0);
    c.type("/");
    expect(c.names()).toEqual([...LOCAL_COMMANDS.map((l) => "/" + l.name), "/compact"]);
    c.type("/com");
    expect(c.names()).toEqual(["/compact"]);
    c.type("/com ");
    expect(c.rows()).toHaveLength(0);
    c.type("/zzz");
    expect(c.rows()).toHaveLength(0);
  });

  test("arrows move, Enter accepts into the box without sending", () => {
    const c = mount();
    c.type("/");
    expect(c.active()).toBe(0);
    c.press("ArrowDown");
    expect(c.active()).toBe(1);
    c.press("ArrowUp");
    c.press("ArrowUp");
    expect(c.active()).toBe(c.rows().length - 1);
    expect(c.press("Enter")).toBe(true);
    expect(c.box().props.value).toBe("/compact ");
    expect(c.sent).toEqual([]);
    expect(c.rows()).toHaveLength(0);
  });

  test("Esc closes it for that text; typing brings it back", () => {
    const c = mount();
    c.type("/c");
    expect(c.rows().length).toBeGreaterThan(0);
    expect(c.press("Escape")).toBe(true);
    expect(c.rows()).toHaveLength(0);
    // With the menu shut, Enter is a send again.
    c.type("/cl");
    expect(c.rows().length).toBeGreaterThan(0);
  });
});

describe("composer: local commands are answered, never sent", () => {
  test("/clear calls the handler, clears the box, sends nothing", () => {
    const c = mount();
    c.type("/clear ");
    c.press("Enter");
    expect(c.locals).toEqual([{ kind: "local", name: "clear", arg: "" }]);
    expect(c.sent).toEqual([]);
    expect(c.box().props.value).toBe("");
  });

  test("a refused one puts the words back", () => {
    const c = mount(() => false);
    c.type("/model nope");
    c.press("Enter");
    expect(c.locals.map((l) => [l.name, l.arg])).toEqual([["model", "nope"]]);
    expect(c.sent).toEqual([]);
    expect(c.box().props.value).toBe("/model nope");
  });

  test("/help opens the menu instead of reaching the host", () => {
    const c = mount();
    c.type("/help ");
    c.press("Enter");
    expect(c.locals).toEqual([]);
    expect(c.box().props.value).toBe("");
    expect(c.rows().length).toBe(LOCAL_COMMANDS.length + 1);
    c.press("Escape");
    expect(c.rows()).toHaveLength(0);
  });

  test("a CLI command and an unknown one go out verbatim", () => {
    const c = mount();
    c.type("/compact keep the plan");
    c.press("Enter");
    c.type("/nosuch thing");
    c.press("Enter");
    expect(c.sent).toEqual(["/compact keep the plan", "/nosuch thing"]);
    expect(c.locals).toEqual([]);
  });
});

describe("review fixes", () => {
  test("Enter on a fully typed name sends it; Tab still only accepts", () => {
    const c = mount();
    c.type("/compact");
    expect(c.rows().length).toBeGreaterThan(0);
    c.press("Tab");
    expect(c.box().props.value).toBe("/compact ");
    expect(c.sent).toEqual([]);
    c.type("/compact");
    c.press("Enter");
    expect(c.sent).toEqual(["/compact"]);
    // A local one typed in full runs at once too.
    c.type("/clear");
    c.press("Enter");
    expect(c.locals.map((l) => l.name)).toEqual(["clear"]);
  });

  test("an IME composition keeps its keys", () => {
    const c = mount();
    c.type("/c");
    const before = c.active();
    expect(c.press("ArrowDown", false, true)).toBe(false);
    expect(c.active()).toBe(before);
  });

  test("the box is a combobox; the list ids only while open", () => {
    const c = mount();
    expect(c.box().props.role).toBe("combobox");
    expect(c.box().props["aria-autocomplete"]).toBe("list");
    expect(c.box().props["aria-expanded"]).toBe(false);
    expect(c.box().props["aria-controls"]).toBeUndefined();
    c.type("/");
    expect(c.box().props["aria-expanded"]).toBe(true);
    expect(c.box().props["aria-controls"]).toBeTruthy();
    expect(String(c.box().props["aria-activedescendant"])).toEndWith("-0");
  });

  test("/help with words after it goes to the host, which may refuse and keep them", () => {
    const c = mount(() => false);
    c.type("/help me with this");
    c.press("Enter");
    expect(c.locals.map((l) => [l.name, l.arg])).toEqual([["help", "me with this"]]);
    expect(c.box().props.value).toBe("/help me with this");
    expect(c.sent).toEqual([]);
  });
});

test("Enter on a name typed in the wrong case accepts the row instead of sending", () => {
  const c = mount();
  c.type("/CLEAR");
  expect(c.names()).toEqual(["/clear"]);
  expect(c.press("Enter")).toBe(true);
  expect(c.box().props.value).toBe("/clear ");
  expect(c.sent).toEqual([]);
  expect(c.locals).toEqual([]);
});
