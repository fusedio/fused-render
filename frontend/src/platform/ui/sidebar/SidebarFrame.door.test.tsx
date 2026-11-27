// The collapsed rail's icons and the hover card hooks an owner may hang on
// them (`SidebarRailItem.door`, the shell's "Left sidebar preview"): with none an
// icon is exactly what it was; with one its handlers and attributes ride on
// the icon, the frame's own still run, and `quietTip` keeps the tooltip away.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";

const { SidebarFrame, NavItem } = await import("./SidebarFrame");
const { SidebarDoorContext } = await import("./sidebar-doors");
const { setSidebarState } = await import("@platform/lib/sidebarstate");
type Item = import("./SidebarFrame").SidebarRailItem;

const rect = { top: 10, height: 28, left: 10, width: 28, right: 38, bottom: 38 };
const target = { getBoundingClientRect: () => rect };

let r: ReactTestRenderer | null = null;
beforeEach(() => {
  setSidebarState({ width: 240, collapsed: true }, false);
});
afterEach(() => {
  act(() => r?.unmount());
  r = null;
  setSidebarState({ width: 240, collapsed: false }, false);
});

function draw(rail: Item[], onRailEl?: (el: HTMLElement | null) => void): ReactTestRenderer {
  act(() => {
    r = create(
      <SidebarFrame title="Render" rail={rail} {...(onRailEl ? { onRailEl } : {})}>
        <div />
      </SidebarFrame>,
      { createNodeMock: () => ({ closest: () => null }) },
    );
  });
  return r!;
}

const icon = (root: ReactTestRenderer, label: string): ReactTestInstance =>
  root.root.find((n) => (n.type === "a" || n.type === "button") && n.props["aria-label"] === label);
const tips = (root: ReactTestRenderer) => root.root.findAll((n) => n.type === "div" && n.props.className === "sidebar-rail-tip");

describe("SidebarFrame rail icons", () => {
  test("no door: the icon's props are the ones it always had", () => {
    const root = draw([{ key: "home", label: "Home", icon: null, href: "/home" }]);
    const a = icon(root, "Home");
    expect(Object.keys(a.props).sort()).toEqual(
      ["aria-label", "children", "className", "href", "onBlur", "onClick", "onFocus", "onMouseEnter", "onMouseLeave"].sort(),
    );
    act(() => a.props.onMouseEnter({ currentTarget: target }));
    expect(tips(root)).toHaveLength(1);
  });

  test("a door: its attributes and handlers ride on the icon; the click runs it first; quietTip keeps the tooltip away", () => {
    const seen: string[] = [];
    const root = draw([
      {
        key: "home",
        label: "Home",
        icon: null,
        href: "/home",
        onClick: () => seen.push("click"),
        door: {
          attrs: { "data-left-door": "home", "aria-controls": "card-1" },
          onPointerEnter: () => seen.push("enter"),
          onPointerLeave: () => seen.push("leave"),
          onKeyDown: () => seen.push("key"),
          onFocus: () => seen.push("focus"),
          onActivate: () => seen.push("activate"),
          quietTip: true,
        },
      },
    ]);
    const a = icon(root, "Home");
    expect(a.props["data-left-door"]).toBe("home");
    expect(a.props["aria-controls"]).toBe("card-1");
    a.props.onPointerEnter({});
    a.props.onPointerLeave({});
    a.props.onKeyDown({});
    act(() => a.props.onFocus({ currentTarget: target }));
    act(() => a.props.onMouseEnter({ currentTarget: target }));
    expect(tips(root)).toHaveLength(0);
    act(() => a.props.onClick({ preventDefault() {} }));
    expect(seen).toEqual(["enter", "leave", "key", "focus", "activate", "click"]);
  });

  test("an action icon (no href) wears a door too", () => {
    const seen: string[] = [];
    const root = draw([{ key: "search", label: "Search", icon: null, onClick: () => seen.push("click"), door: { onActivate: () => seen.push("activate") } }]);
    const b = icon(root, "Search");
    expect(b.type).toBe("button");
    act(() => b.props.onClick({}));
    expect(seen).toEqual(["activate", "click"]);
  });

  test("onRailEl hears the sidebar once, and keeps it across a collapse and an expand (one node)", () => {
    const els: (HTMLElement | null)[] = [];
    const fn = (el: HTMLElement | null) => els.push(el);
    draw([{ key: "home", label: "Home", icon: null, href: "/home" }], fn);
    expect(els).toHaveLength(1);
    expect(els[0]).not.toBeNull();
    act(() => setSidebarState({ width: 240, collapsed: false }, false));
    act(() => setSidebarState({ width: 240, collapsed: true }, false));
    expect(els).toHaveLength(1);
  });
});

describe("expanded rows (NavItem doorKey, SidebarDoorContext)", () => {
  beforeEach(() => setSidebarState({ width: 240, collapsed: false }, false));
  const row = (root: ReactTestRenderer): ReactTestInstance => root.root.find((n) => n.type === "a" && n.props.className?.startsWith("sidebar-item"));

  test("no provider, or a key it does not answer: the row's props are the ones it always had", () => {
    let root: ReactTestRenderer | null = null;
    act(() => {
      root = create(<NavItem href="/home" label="Home" icon={null} title="Go home" doorKey="home" />);
    });
    const bare = row(root!);
    expect(Object.keys(bare.props).sort()).toEqual(["children", "className", "href", "id", "onClick", "title"].sort());
    act(() => root!.unmount());
    act(() => {
      root = create(
        <SidebarDoorContext.Provider value={() => undefined}>
          <NavItem href="/home" label="Home" icon={null} title="Go home" doorKey="home" />
        </SidebarDoorContext.Provider>,
      );
    });
    expect(Object.keys(row(root!).props).sort()).toEqual(["children", "className", "href", "id", "onClick", "title"].sort());
    act(() => root!.unmount());
  });

  test("a door: its hooks ride on the row, its title goes quiet, the click runs it first", () => {
    const seen: string[] = [];
    let root: ReactTestRenderer | null = null;
    act(() => {
      root = create(
        <SidebarDoorContext.Provider
          value={(key) =>
            key === "home"
              ? {
                  attrs: { "data-left-door": "home", "data-left-open": "open" },
                  onPointerEnter: () => seen.push("enter"),
                  onPointerDown: () => seen.push("down"),
                  onContextMenu: () => seen.push("menu"),
                  onActivate: () => seen.push("activate"),
                  quietTip: true,
                }
              : undefined
          }
        >
          <NavItem href="/home" label="Home" icon={null} title="Go home" doorKey="home" onClick={() => seen.push("click")} />
        </SidebarDoorContext.Provider>,
      );
    });
    const a = row(root!);
    expect(a.props["data-left-door"]).toBe("home");
    expect(a.props["data-left-open"]).toBe("open");
    expect(a.props.title).toBeUndefined();
    a.props.onPointerEnter({});
    a.props.onPointerDown({});
    a.props.onContextMenu({});
    act(() => a.props.onClick({ preventDefault() {} }));
    expect(seen).toEqual(["enter", "down", "menu", "activate", "click"]);
    act(() => root!.unmount());
  });

  test("onRailEl hears the expanded sidebar too (one node in both layouts)", () => {
    const els: (HTMLElement | null)[] = [];
    const fn = (el: HTMLElement | null) => els.push(el);
    draw([], fn);
    expect(els).toHaveLength(1);
    expect(els[0]).not.toBeNull();
  });
});
