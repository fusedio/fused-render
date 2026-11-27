// A door that TAKES the click (its `onActivate` answers true — a favourite's
// card in a workspace host): the entry's own click does not run, and the door
// hears how the click came (Enter, or a modifier down).
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";

const { SidebarFrame, NavItem } = await import("./SidebarFrame");
const { SidebarDoorContext } = await import("./sidebar-doors");
const { setSidebarState } = await import("@platform/lib/sidebarstate");
type Item = import("./SidebarFrame").SidebarRailItem;
type How = { keyboard: boolean; modified: boolean } | undefined;

let r: ReactTestRenderer | null = null;
beforeEach(() => setSidebarState({ width: 240, collapsed: true }, false));
afterEach(() => {
  act(() => r?.unmount());
  r = null;
  setSidebarState({ width: 240, collapsed: false }, false);
});

const click = { preventDefault() {}, detail: 1, shiftKey: false, metaKey: false, ctrlKey: false, altKey: false };
const icon = (root: ReactTestRenderer, label: string): ReactTestInstance =>
  root.root.find((n) => (n.type === "a" || n.type === "button") && n.props["aria-label"] === label);

describe("a door that takes the click", () => {
  test("a rail icon: true skips the icon's own click; anything else lets it run", () => {
    const seen: string[] = [];
    const hows: How[] = [];
    let take = true;
    const rail: Item[] = [
      { key: "pin:b1", label: "Fav", icon: null, href: "/x", onClick: () => seen.push("click"), door: { onActivate: (h) => (hows.push(h), take) } },
    ];
    act(() => {
      r = create(<SidebarFrame title="Render" rail={rail}><div /></SidebarFrame>, { createNodeMock: () => ({ closest: () => null }) });
    });
    act(() => icon(r!, "Fav").props.onClick({ ...click, detail: 0 }));
    expect(seen).toEqual([]);
    expect(hows[0]).toEqual({ keyboard: true, modified: false });
    take = false;
    act(() => icon(r!, "Fav").props.onClick({ ...click, shiftKey: true }));
    expect(seen).toEqual(["click"]);
    expect(hows[1]).toEqual({ keyboard: false, modified: true });
  });

  test("an expanded row (NavItem) does the same", () => {
    const seen: string[] = [];
    setSidebarState({ width: 240, collapsed: false }, false);
    act(() => {
      r = create(
        <SidebarDoorContext.Provider value={() => ({ onActivate: () => true })}>
          <SidebarFrame title="Render">
            <NavItem id="search-link" label="Search" icon={null} doorKey="search" onClick={() => seen.push("click")} />
          </SidebarFrame>
        </SidebarDoorContext.Provider>,
        { createNodeMock: () => ({ closest: () => null }) },
      );
    });
    act(() => r!.root.find((n) => n.type === "button" && n.props.id === "search-link").props.onClick(click));
    expect(seen).toEqual([]);
  });
});
