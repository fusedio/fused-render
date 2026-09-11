// THE WORDLESS SEND'S BUBBLE, and the reader who happens to type its words.
//
// A send with no typed text still gets a bubble, and what it says is what the
// message CARRIED — "files", "images", "annotations", "pane screenshot" — each
// with the lucide glyph the chip and the receipt wear (P2-7).
//
// T could tell those apart from a prompt because it wrote them with an emoji in
// front. The port dropped the emoji and kept matching on the WORD, so a message
// that was literally "files" — an ordinary thing to say to an agent — was drawn
// as a wordless attachment send, icon and all (Bugbot, PR #1064). Marker-ness
// is a private sigil now, and this suite pins both directions of that: the
// marker still draws its icons and still SAYS only the word, and the reader's
// own "files" is a plain bubble.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";

// Loaded AFTER the shim: `Turn` reaches the platform router through the receipt
// row, and that module reads `location` at import time.
const { MARKER_FILE, MARKER_JOIN, MARKER_VIEW } = await import("../protocol/wire");
const { Turn } = await import("./Turn");
type UserTurn = import("../protocol/controller-api").UserTurn;

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

type Json = ReactTestRendererJSON;
function walk(node: Json | string | null, hit: (n: Json | string) => void): void {
  if (!node) return;
  hit(node);
  if (typeof node === "string") return;
  for (const k of node.children ?? []) walk(k as Json, hit);
}

/** Every text node under the bubble, joined: what the reader actually reads. */
function bubbleText(r: ReturnType<typeof create>): string {
  let out = "";
  walk(r.toJSON() as Json, (n) => {
    if (typeof n === "string") out += n;
  });
  return out;
}

function count(r: ReturnType<typeof create>, type: string): number {
  let n = 0;
  walk(r.toJSON() as Json, (node) => {
    if (typeof node !== "string" && node.type === type) n += 1;
  });
  return n;
}

function markers(r: ReturnType<typeof create>): number {
  let n = 0;
  walk(r.toJSON() as Json, (node) => {
    if (typeof node === "string") return;
    const c = (node.props as { className?: string } | undefined)?.className;
    if (typeof c === "string" && c.split(/\s+/).includes("c-marker")) n += 1;
  });
  return n;
}

const user = (text: string): UserTurn => ({ role: "user", key: "u:1", text });

test("a wordless send's bubble draws one icon per marker, and only the words", () => {
  const r = mount(<Turn turn={user(MARKER_FILE + MARKER_JOIN + MARKER_VIEW)} />);
  expect(markers(r)).toBe(2);
  // Two lucide glyphs, and no emoji anywhere near them (P2-7).
  expect(count(r, "svg")).toBe(2);
  // The SIGIL IS MACHINERY: it is invisible, and it does not reach the page.
  expect(bubbleText(r)).toBe("files" + MARKER_JOIN + "pane screenshot");
  expect(bubbleText(r)).not.toContain("⁣");
});

test("a reader who types a marker's word gets a plain bubble (Bugbot, PR #1064)", () => {
  for (const typed of ["files", "images", "annotations", "pane screenshot", "files + images"]) {
    const r = mount(<Turn turn={user(typed)} />);
    expect(markers(r)).toBe(0);
    expect(count(r, "svg")).toBe(0);
    expect(bubbleText(r)).toBe(typed);
  }
});
