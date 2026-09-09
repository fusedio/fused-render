// WHAT A FAILED TURN LOOKS LIKE. The reported bug was not that the app told
// the reader nothing — it told them everything, in one shape:
//
//   * a red row holding `_account_error`'s whole rewrite as one run-on
//     sentence, help URL and the CLI's parenthetical included, with "run
//     /login" buried in the middle of it;
//   * the URL as plain text, so the one place to go was not clickable;
//   * and under it the trouble card DOUBLE-WRAPPED — `.turn.trouble`'s plate
//     around `platform/ui/TroubleCard`'s own — two borders, two paddings.
//
// So these pin the three: one card plate, the instruction on its own line, and
// the help URL as an `<a>`. Plus the two properties that must SURVIVE the
// change — an error shape we do not recognise stays exactly one text node, and
// nothing here became a second innerHTML site.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";

import { splitTroubleMessage } from "../protocol/trouble";
import { TroubleView } from "./TroubleView";
import { Turn } from "./Turn";

const HERE = dirname(new URL(import.meta.url).pathname);

/** agent.py's `_account_error` login branch, verbatim (agent.py:3197 — the one
 *  string this whole file is about; the backend text is NOT ours to change,
 *  the legacy template shares it). */
const LOGIN =
  "Claude Code isn't logged in. Open a terminal, run `claude`, type /login " +
  "and finish the sign-in, then start a new chat here. " +
  "Help: https://render.fused.io/#troubleshooting-login (Invalid API key · Please run /login)";

/** Its limit sibling, same shape. */
const LIMIT =
  "Your Claude plan's usage limit was reached. Wait for it to reset, or " +
  "upgrade the plan, then try again. " +
  "Help: https://render.fused.io/#troubleshooting-limit (Usage limit reached|1751200000)";

const mounted: Array<ReturnType<typeof create>> = [];
function mount(el: React.ReactElement): ReturnType<typeof create> {
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
function walk(node: Json | string | null, hit: (n: Json) => void): void {
  if (!node || typeof node === "string") return;
  hit(node);
  for (const k of node.children ?? []) walk(k as Json, hit);
}
const cls = (n: Json) => String((n.props as { className?: string }).className ?? "").split(/\s+/);
function all(r: ReturnType<typeof create>, want: string): Json[] {
  const out: Json[] = [];
  walk(r.toJSON() as Json, (n) => {
    if (cls(n).includes(want)) out.push(n);
  });
  return out;
}
function textOf(node: Json | string | null): string {
  if (!node) return "";
  if (typeof node === "string") return node;
  return (node.children ?? []).map((k) => textOf(k as Json)).join("");
}
function links(r: ReturnType<typeof create>): Json[] {
  const out: Json[] = [];
  walk(r.toJSON() as Json, (n) => {
    if (n.type === "a") out.push(n);
  });
  return out;
}

const errorTurn = (text: string) =>
  mount(<Turn turn={{ role: "error", key: "e1", text, kind: "login" }} />);

test("the rewrite comes apart into the parts it was assembled from", () => {
  expect(splitTroubleMessage(LOGIN)).toEqual({
    lead: "Claude Code isn't logged in.",
    action:
      "Open a terminal, run `claude`, type /login and finish the sign-in, " +
      "then start a new chat here.",
    help: "https://render.fused.io/#troubleshooting-login",
    raw: "Invalid API key · Please run /login",
  });
  const limit = splitTroubleMessage(LIMIT);
  expect(limit.lead).toBe("Your Claude plan's usage limit was reached.");
  expect(limit.action).toBe("Wait for it to reset, or upgrade the plan, then try again.");
  expect(limit.raw).toBe("Usage limit reached|1751200000");
});

test("the error row puts the instruction on its own line, with the raw text demoted", () => {
  const r = errorTurn(LOGIN);
  const rows = all(r, "err-line").map(textOf);
  // Four statements, four lines — the complaint was that they were one.
  expect(rows.length).toBe(4);
  expect(rows[0]).toBe("Claude Code isn't logged in.");
  const action = all(r, "err-action").map(textOf);
  expect(action.length).toBe(1);
  expect(action[0]).toContain("Open a terminal");
  expect(action[0]).toContain("/login");
  // The CLI's own words are still on screen, and still hoverable.
  const raw = all(r, "err-raw");
  expect(textOf(raw[0]!)).toBe("Invalid API key · Please run /login");
  expect((raw[0]!.props as { title?: string }).title).toBe("Invalid API key · Please run /login");
});

test("the help URL is a real link, opened safely", () => {
  const a = links(errorTurn(LOGIN));
  expect(a.length).toBe(1);
  const props = a[0]!.props as { href: string; target?: string; rel?: string };
  expect(props.href).toBe("https://render.fused.io/#troubleshooting-login");
  expect(props.target).toBe("_blank");
  expect(props.rel).toContain("noopener");
  expect(textOf(a[0]!)).toBe("https://render.fused.io/#troubleshooting-login");
});

test("an error shape we do not recognise stays one text node", () => {
  const raw = "API Error: 500 {\"type\":\"error\"}";
  const r = errorTurn(raw);
  const row = r.toJSON() as Json;
  expect(cls(row)).toContain("error");
  expect(row.children).toEqual([raw]);
  // No lines, no links, nothing invented.
  expect(all(r, "err-line").length).toBe(0);
  expect(links(r).length).toBe(0);
});

test("a bare URL in an unrecognised message is still clickable", () => {
  const a = links(errorTurn("could not reach https://render.fused.io/health, retrying"));
  expect(a.length).toBe(1);
  // The comma after it belongs to the sentence, not to the href.
  expect((a[0]!.props as { href: string }).href).toBe("https://render.fused.io/health");
});

test("ONE card plate: the row is the box, the platform card is flattened in it", () => {
  const r = mount(<TroubleView trouble={{ kind: "login", message: LOGIN }} what="using the chat" />);
  expect(all(r, "trouble-card").length).toBe(1);
  expect(all(r, "trouble").length).toBe(1);
  // …and the CSS is what makes that one plate rather than two nested ones.
  const sheet = readFileSync(join(HERE, "../styles/transcript.css"), "utf8");
  const rule = /\.chat-root \.turn\.trouble \.trouble-card \{([^}]*)\}/.exec(sheet);
  expect(rule).not.toBeNull();
  expect(rule![1]).toContain("border: 0");
  expect(rule![1]).toContain("padding: 0");
  expect(rule![1]).toContain("background: none");
});

test("the card's verbatim block holds the CLI's error, not our own sentence back again", () => {
  const r = mount(<TroubleView trouble={{ kind: "login", message: LOGIN }} />);
  const pre: Json[] = [];
  walk(r.toJSON() as Json, (n) => {
    if (n.type === "pre") pre.push(n);
  });
  expect(pre.length).toBe(1);
  expect(textOf(pre[0]!)).toBe("Invalid API key · Please run /login");
  // The instruction is the card's description instead — said once, not twice.
  expect(textOf(all(r, "trouble-explain")[0]!)).toContain("Open a terminal");
  expect(textOf(all(r, "trouble-explain")[0]!)).not.toContain("`");
});

test("no new innerHTML site: the error row and the card are text nodes", () => {
  for (const name of ["TroubleView.tsx", "Turn.tsx"]) {
    expect(readFileSync(join(HERE, name), "utf8")).not.toContain("dangerouslySetInnerHTML");
  }
});
