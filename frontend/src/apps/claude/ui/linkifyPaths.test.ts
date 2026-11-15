import { expect, test } from "bun:test";

import { linkifyEnabled, linkifyPaths, type LinkTarget, type ProseText } from "./linkifyPaths";

function code(text: string, inPre = false) {
  const classes = new Set<string>();
  const attrs: Record<string, string> = {};
  const el: LinkTarget & { classes: Set<string>; attrs: Record<string, string> } = {
    textContent: text,
    closest: (sel: string) => (sel === "pre" && inPre ? {} : null),
    classList: { add: (c: string) => void classes.add(c) },
    setAttribute: (k: string, v: string) => void (attrs[k] = v),
    classes,
    attrs,
  };
  return el;
}

const rootOf = (els: LinkTarget[]) => ({ querySelectorAll: () => els });

test("an existing path gets the link class, role, tabindex and resolved data-path", async () => {
  const el = code("src/a.ts:12");
  const asked: string[] = [];
  const n = await linkifyPaths(rootOf([el]), {
    cwd: "/w",
    stat: async (p) => (asked.push(p), true),
  });
  expect(n).toBe(1);
  expect(asked).toEqual(["/w/src/a.ts"]);
  expect(el.classes.has("c-path-link")).toBe(true);
  expect(el.attrs.role).toBe("button");
  expect(el.attrs.tabindex).toBe("0");
  expect(el.attrs["data-path"]).toBe("/w/src/a.ts");
});

test("a path that does not exist, plain words, and code inside pre are left alone", async () => {
  const gone = code("src/missing.ts");
  const word = code("foo");
  const fenced = code("src/a.ts", true);
  const stat = async (p: string) => p !== "/w/src/missing.ts";
  await linkifyPaths(rootOf([gone, word, fenced]), { cwd: "/w", stat });
  expect(gone.classes.size).toBe(0);
  expect(word.classes.size).toBe(0);
  expect(fenced.classes.size).toBe(0);
});

test("dedupes stats and caps the number of distinct paths", async () => {
  const els = [code("a.ts"), code("a.ts"), ...Array.from({ length: 40 }, (_, i) => code(`f${i}.ts`))];
  const asked: string[] = [];
  await linkifyPaths(rootOf(els), { stat: async (p) => (asked.push(p), true) });
  expect(asked.filter((p) => p === "a.ts").length).toBe(1);
  expect(asked.length).toBe(30);
});

test("a stale tree is not marked", async () => {
  const el = code("a.ts");
  await linkifyPaths(rootOf([el]), { stat: async () => true, live: () => false });
  expect(el.classes.size).toBe(0);
});

test("the inert variant, a streaming tail, and a chat with no opener never linkify", () => {
  expect(linkifyEnabled({ links: true, enhance: true, hasOpener: true })).toBe(true);
  expect(linkifyEnabled({ links: false, enhance: true, hasOpener: true })).toBe(false);
  expect(linkifyEnabled({ links: true, enhance: false, hasOpener: true })).toBe(false);
  expect(linkifyEnabled({ links: true, enhance: true, hasOpener: false })).toBe(false);
});

test("~/ paths stat and link at the absolute home path; without a home they are left alone", async () => {
  const el = code("~/Documents/r.html");
  const asked: string[] = [];
  const n = await linkifyPaths(rootOf([el]), {
    cwd: "/w",
    home: "/home/u",
    stat: async (p) => (asked.push(p), true),
  });
  expect(n).toBe(1);
  expect(asked).toEqual(["/home/u/Documents/r.html"]);
  expect(el.attrs["data-path"]).toBe("/home/u/Documents/r.html");

  const lone = code("~/Documents/r.html");
  const none: string[] = [];
  expect(await linkifyPaths(rootOf([lone]), { cwd: "/w", stat: async (p) => (none.push(p), true) })).toBe(0);
  expect(none).toEqual([]);
});

test("a ~ that is not a leading ~/ is never linked", async () => {
  const els = [code("~"), code("a~b.ts"), code("~user/x.ts"), code("echo ~/x.ts")];
  const asked: string[] = [];
  const n = await linkifyPaths(rootOf(els), { cwd: "/w", home: "/h", stat: async (p) => (asked.push(p), true) });
  expect(n).toBe(0);
  expect(asked).toEqual([]);
});

// A tiny tree: bun has no DOM here, so this models just what the prose pass uses.
class FEl {
  className = "";
  attrs: Record<string, string> = {};
  kids: (FEl | FText)[] = [];
  parentNode: FEl | null = null;
  ownerDocument = { createElement: (tag: string) => new FEl(tag) };
  classList = { contains: (c: string) => this.className.split(" ").includes(c) };
  constructor(public tagName: string) {}
  setAttribute(k: string, v: string) {
    this.attrs[k] = v;
  }
  appendChild(c: FEl | FText) {
    c.parentNode = this;
    this.kids.push(c);
    return c;
  }
  replaceChild(next: FEl, old: FText) {
    this.kids[this.kids.indexOf(old)] = next;
    next.parentNode = this;
    old.parentNode = null;
  }
}
class FText {
  parentNode: FEl | null = null;
  constructor(public data: string) {}
  splitText(offset: number): FText {
    const tail = new FText(this.data.slice(offset));
    this.data = this.data.slice(0, offset);
    const p = this.parentNode!;
    tail.parentNode = p;
    p.kids.splice(p.kids.indexOf(this) + 1, 0, tail);
    return tail;
  }
}
function el(tag: string, ...kids: (FEl | string)[]): FEl {
  const e = new FEl(tag);
  for (const k of kids) e.appendChild(typeof k === "string" ? new FText(k) : k);
  return e;
}
function textsOf(e: FEl): FText[] {
  return e.kids.flatMap((k) => (k instanceof FText ? [k] : textsOf(k)));
}
function proseRoot(e: FEl, codes: LinkTarget[] = []) {
  return { querySelectorAll: () => codes, textNodes: () => textsOf(e) as unknown as ProseText[] };
}
const render = (e: FEl): string =>
  e.kids.map((k) => (k instanceof FText ? k.data : `[${k.tagName}${k.className ? "." + k.className : ""}:${render(k)}]`)).join("");

const REPORT = "/private/tmp/x/scratchpad/REPORT.md";

test("prose: a bare absolute path is wrapped in a span, trailing period stays outside", async () => {
  const p = el("p", `The full write-up is at ${REPORT}. The substance:`);
  const n = await linkifyPaths(proseRoot(p), { stat: async () => true });
  expect(n).toBe(1);
  expect(render(p)).toBe(`The full write-up is at [span.c-path-link:${REPORT}]. The substance:`);
  const span = p.kids[1] as FEl;
  expect(span.attrs["data-path"]).toBe(REPORT);
  expect(span.attrs.role).toBe("button");
  expect(span.attrs.tabindex).toBe("0");
});

test("prose: two paths in one text node are both wrapped", async () => {
  const p = el("p", "a /w/a.md and /w/b.md.");
  const n = await linkifyPaths(proseRoot(p), { stat: async () => true });
  expect(n).toBe(2);
  expect(render(p)).toBe("a [span.c-path-link:/w/a.md] and [span.c-path-link:/w/b.md].");
});

test("prose: a missing file leaves the text untouched", async () => {
  const p = el("p", `at ${REPORT}.`);
  expect(await linkifyPaths(proseRoot(p), { stat: async () => false })).toBe(0);
  expect(render(p)).toBe(`at ${REPORT}.`);
  expect(p.kids.length).toBe(1);
});

test("prose: text inside pre, code and a link is not wrapped", async () => {
  const root = el("div", el("pre", "/w/a.md"), el("code", "/w/b.md"), el("a", "/w/c.md"), el("p", "x /w/d.md"));
  const asked: string[] = [];
  const n = await linkifyPaths(proseRoot(root), { stat: async (p) => (asked.push(p), true) });
  expect(n).toBe(1);
  expect(asked).toEqual(["/w/d.md"]);
});

test("prose: the same path in a code and in prose costs one stat", async () => {
  const c = code("/w/a.md");
  const p = el("p", "also /w/a.md here");
  const asked: string[] = [];
  const n = await linkifyPaths(proseRoot(p, [c]), { stat: async (x) => (asked.push(x), true) });
  expect(asked).toEqual(["/w/a.md"]);
  expect(n).toBe(2);
  expect(c.classes.has("c-path-link")).toBe(true);
});

test("prose: ~/ needs a known home", async () => {
  const p = el("p", "see ~/n/a.md");
  expect(await linkifyPaths(proseRoot(p), { stat: async () => true })).toBe(0);
  expect(await linkifyPaths(proseRoot(p), { home: "/h", stat: async () => true })).toBe(1);
  expect((p.kids[1] as FEl).attrs["data-path"]).toBe("/h/n/a.md");
});
