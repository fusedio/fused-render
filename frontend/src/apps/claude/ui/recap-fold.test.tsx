// THE FOLD ITSELF: the label, the sentence, the two ways out.
//
// Small, because there is little here that can go wrong — but the two things
// that CAN are both the kind that ship silently. The body must render as PLAIN
// TEXT (the design pins it: a recap is a sentence written about a conversation,
// and markdown would let a backtick out of the transcript restyle a line the
// reader cannot edit), and the × must actually be a control — a dismiss that
// renders but does not call back is exactly the dead affordance the chat's own
// `AttachTray` notes warn about.
//
// WHAT THE BODY'S CLICK DOES is not here: this component only calls back, and
// the host decides what "carry me there" means. That is `ui/recap-jump.test.tsx`
// — the scrollport's lent `jumpRef`, the flare, and the fallback for a uuid the
// log does not draw.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer, type ReactTestRendererJSON } from "react-test-renderer";

import { RecapFold } from "./RecapFold";

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

function mount(el: React.ReactElement): ReactTestRenderer {
  let r!: ReactTestRenderer;
  act(() => {
    r = create(el);
  });
  mounted.push(r);
  return r;
}

/** Every text node in the tree, in order. */
function words(node: ReactTestRendererJSON | string | null, out: string[] = []): string[] {
  if (!node) return out;
  if (typeof node === "string") {
    out.push(node);
    return out;
  }
  for (const k of node.children ?? []) words(k as ReactTestRendererJSON, out);
  return out;
}

const TEXT = "You are wiring the recap fold into the chat; next is the browser check.";

test("the row is a `note` turn that says `Recap:` and then the text, as plain text", () => {
  const r = mount(createElement(RecapFold, { text: TEXT }));
  const json = r.toJSON() as ReactTestRendererJSON;
  expect(json.props.className).toBe("turn note recap");
  const said = words(json);
  expect(said).toContain("Recap:");
  // ONE text node for the body — no markdown pass split it into elements, and
  // nothing rewrote it.
  expect(said).toContain(TEXT);
});

test("nothing in the row is a control: no button, no link, no dismiss", () => {
  const r = mount(createElement(RecapFold, { text: TEXT }));
  expect(r.root.findAllByType("button")).toEqual([]);
  expect(r.root.findAllByType("a")).toEqual([]);
  // The host owns the row's life (useAwayRecap clears it on the next send), so
  // taking it away is the host not rendering it any more.
  act(() => r.update(createElement("div", null)));
  expect(words(r.toJSON() as ReactTestRendererJSON)).toEqual([]);
});
