// The Skills page: one search over the skills on this machine and skills.sh.
// A stubbed `globalThis.fetch` (the api helpers are thin fetch wrappers and
// `mock.module` leaks across files). No test here may find a popularity count.
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";
import { installDomShim } from "@platform/lib/testDomShim";
import { freeName, matchSkills, readParams, skillKey, writeParams } from "./skills/model";

installDomShim();
const { default: Skills } = await import("./Skills");

function textOf(node: ReactTestInstance | string): string {
  if (typeof node === "string") return node;
  return node.children.map(textOf).join("");
}

const realFetch = globalThis.fetch;
const realLocation = globalThis.location;
const realHistory = globalThis.history;

const MINE = {
  slug: "ui-design-review", name: "ui-design-review", description: "Review a page against the design tokens.",
  source: "user", linked: false, path: "/h/.agents/skills/ui-design-review", location: "/h/.agents/skills",
  paths: ["/h/.agents/skills/ui-design-review"], readers: ["claude"],
};
const BUNDLED = {
  slug: "frontend-design", name: "frontend-design", description: "Create distinctive frontend interfaces.",
  source: "plugin", plugin: "frontend-design", marketplace: "mk", linked: false,
  path: "/p/frontend-design", location: "/p", paths: ["/p/frontend-design"], readers: ["claude"],
};
const OTHER = { ...MINE, slug: "release", name: "release", description: "Cuts a release.", path: "/h/.agents/skills/release", location: "/h/.agents/skills" };
const HIT = { id: "vercel-labs/agent-skills/web-design-guidelines", source: "vercel-labs/agent-skills", skillId: "web-design-guidelines", name: "web-design-guidelines", installs: 424242, added: false };

let calls: { action: string; body: Record<string, unknown> }[] = [];
let searchReply: () => unknown = () => ({ ok: true, skills: [HIT] });
let searchQueries: string[] = [];
let listFails = false;

function fakeFetch(url: unknown, init?: RequestInit): Promise<Response> {
  let body: Record<string, unknown> = {};
  try { body = JSON.parse(String(init?.body ?? "{}")); } catch { /* GET */ }
  const action = String(body.action ?? "");
  const u = String(url);
  if (u.endsWith("/claude-config/skills")) calls.push({ action, body });
  let data: unknown = {};
  if (u.endsWith("/claude-config/skills")) {
    if (action === "list" && listFails) return Promise.reject(new Error("offline"));
    if (action === "list") data = { skills: [MINE, OTHER, BUNDLED] };
    else if (action === "search") { searchQueries.push(String(body.query)); data = searchReply(); }
    else if (action === "remote_preview") data = { ok: true, text: "---\nname: web-design-guidelines\n---\n\n## Rules\nBe tidy.\n", files: ["SKILL.md", "rules.md"] };
    else if (action === "remote_add") data = { ok: true, slug: "web-design-guidelines" };
  }
  const text = JSON.stringify(data);
  return Promise.resolve({ ok: true, status: 200, json: async () => data, text: async () => text } as unknown as Response);
}

function setLocation(search: string) {
  const loc = { pathname: "/skills", search, href: "http://localhost/skills" + search, origin: "http://localhost", assign() {}, reload() {} };
  globalThis.location = loc as unknown as Location;
  globalThis.history = {
    state: null,
    pushState() {},
    replaceState(_s: unknown, _t: string, url: string) {
      const q = String(url).split("?")[1];
      loc.search = q ? "?" + q : "";
    },
  } as unknown as History;
}

beforeEach(() => {
  calls = [];
  searchQueries = [];
  listFails = false;
  searchReply = () => ({ ok: true, skills: [HIT] });
  setLocation("");
  globalThis.fetch = fakeFetch as unknown as typeof fetch;
});
afterEach(() => {
  globalThis.fetch = realFetch;
  globalThis.location = realLocation;
  globalThis.history = realHistory;
});

const tick = async (ms = 5, n = 3) => {
  for (let i = 0; i < n; i++) await act(async () => { await new Promise((r) => setTimeout(r, ms)); });
};
async function mount() {
  let box!: ReactTestRenderer;
  await act(async () => { box = create(createElement(Skills)); });
  await tick();
  return box;
}
const searchInput = (box: ReactTestRenderer) => box.root.findAllByType("input").find((i) => i.props.type === "search")!;
async function type(box: ReactTestRenderer, value: string) {
  await act(async () => {
    searchInput(box).props.onChange({ currentTarget: { value }, target: { value }, nativeEvent: { defaultPrevented: false } });
  });
  await tick(300, 1); // past the 250ms debounce
  await tick();
}
const button = (box: ReactTestRenderer, label: string) =>
  box.root.findAllByType("button").find((b) => textOf(b).trim() === label)!;

describe("model helpers", () => {
  test("matchSkills filters on name, description and plugin", () => {
    const all = [MINE, BUNDLED] as never[];
    expect(matchSkills(all, "design tokens").map((s) => s.slug)).toEqual(["ui-design-review"]);
    expect(matchSkills(all, "frontend").map((s) => s.slug)).toEqual(["frontend-design"]);
    expect(matchSkills(all, "  ")).toHaveLength(2);
  });
  test("a plugin skill's key names its marketplace, so two marketplaces' plugins do not collide", () => {
    expect(skillKey(BUNDLED as never)).toBe("plugin:frontend-design@mk/frontend-design");
    expect(skillKey({ ...BUNDLED, marketplace: "other" } as never)).not.toBe(skillKey(BUNDLED as never));
  });

  test("freeName stays within 64 characters", () => {
    const long = "a".repeat(63);
    expect(freeName(long, () => false).length).toBeLessThanOrEqual(64);
    expect(freeName(long, () => false)).toBe("a".repeat(62) + "-2");
    expect(freeName(long, () => true).length).toBeLessThanOrEqual(64);
    expect(freeName("a", (n) => n === "a-2")).toBe("a-3");
  });

  test("page params round-trip and keep unrelated params", () => {
    expect(readParams("?new=1&desc=hi")).toEqual({ skill: null, newSkill: true, desc: "hi" });
    expect(writeParams("?x=1&skill=a", { skill: null, newSkill: true, desc: "a b" })).toBe("?x=1&new=1&desc=a+b");
    expect(writeParams("?new=1&desc=a", { newSkill: false, desc: "" })).toBe("");
  });
});

describe("the Skills page", () => {
  test("an empty query lists your skills, plugin skills and the skills.sh hint, and asks skills.sh nothing", async () => {
    const box = await mount();
    const text = textOf(box.root);
    expect(text).toContain("Each skill teaches your coding CLIs how to do one job.");
    expect(text).toContain("Your skills");
    expect(text).toContain("ui-design-review");
    expect(text).toContain("From Claude Code plugins");
    expect(text).toContain("frontend-design");
    expect(text).toContain("Type 2+ letters to search skills.sh too.");
    expect(box.root.findAllByType("input").find((i) => i.props.type === "search")!.props.placeholder).toBe("Search your skills and skills.sh");
    expect(calls.filter((c) => c.action === "search")).toHaveLength(0);
    expect(calls.filter((c) => c.action === "available")).toHaveLength(0);
  });

  test("two letters search installed skills and skills.sh, debounced, with no counts", async () => {
    const box = await mount();
    await type(box, "design");
    expect(searchQueries).toEqual(["design"]);
    const text = textOf(box.root);
    expect(text).toContain("Installed");
    expect(text).toContain("ui-design-review");
    expect(text).not.toContain("release");
    expect(text).toContain("Add from skills.sh");
    expect(text).toContain("web-design-guidelines");
    expect(text).toContain("vercel-labs/agent-skills");
    expect(text).not.toContain("424");
    expect(text.toLowerCase()).not.toContain("install count");
    expect(text.toLowerCase()).not.toContain("popular");
    expect(calls.filter((c) => c.action === "available")).toHaveLength(0);
  });

  test("one letter filters installed skills and does not ask skills.sh", async () => {
    const box = await mount();
    await type(box, "r");
    expect(searchQueries).toEqual([]);
    expect(textOf(box.root)).not.toContain("Add from skills.sh");
  });

  test("a skills.sh failure says so, keeps installed matches, and Retry asks again", async () => {
    searchReply = () => ({ ok: false, error: "skills.sh search failed: boom" });
    const box = await mount();
    await type(box, "design");
    expect(textOf(box.root)).toContain("Couldn't reach skills.sh.");
    expect(textOf(box.root)).toContain("ui-design-review");
    searchReply = () => ({ ok: true, skills: [HIT] });
    await act(async () => { button(box, "Retry").props.onClick(); });
    await tick();
    expect(searchQueries).toEqual(["design", "design"]);
    expect(textOf(box.root)).toContain("web-design-guidelines");
  });

  test("a failed local list is shown in place, with Retry, and skills.sh still works", async () => {
    listFails = true;
    const box = await mount();
    expect(textOf(box.root)).toContain("Couldn't read your skills.");
    expect(textOf(box.root)).not.toContain("No skills of your own yet.");
    await type(box, "design");
    expect(searchQueries).toEqual(["design"]);
    expect(textOf(box.root)).toContain("web-design-guidelines");
    expect(textOf(box.root)).not.toContain("No installed skills match");
    listFails = false;
    await act(async () => { button(box, "Retry").props.onClick(); });
    await tick();
    expect(textOf(box.root)).not.toContain("Couldn't read your skills.");
    expect(textOf(box.root)).toContain("ui-design-review");
  });

  test("picking a skills.sh row previews it without frontmatter and Add places it", async () => {
    const box = await mount();
    await type(box, "design");
    const pick = box.root.findAll((n) => n.type === "button" && String(n.props.className ?? "").includes("cc-srow-pick"))[0];
    await act(async () => { pick.props.onClick(); });
    await tick();
    expect(calls.find((c) => c.action === "remote_preview")?.body).toMatchObject({ source: "vercel-labs/agent-skills", skill_id: "web-design-guidelines" });
    const aside = box.root.findByProps({ "data-testid": "remote-preview" });
    const text = textOf(aside);
    const md = aside.findAll((n) => typeof n.props.text === "string")[0];
    expect(md.props.text).toContain("Be tidy.");
    expect(md.props.text).not.toContain("name: web-design-guidelines");
    expect(text).toContain("rules.md");
    expect(text).toContain("View on GitHub");
    const gh = aside.findAllByType("a")[0];
    expect(gh.props.href).toBe("https://github.com/vercel-labs/agent-skills");
    const add = aside.findAllByType("button").find((b) => textOf(b).includes("Add skill"))!;
    await act(async () => { add.props.onClick(); });
    await tick();
    expect(calls.find((c) => c.action === "remote_add")?.body).toMatchObject({ source: "vercel-labs/agent-skills", skill_id: "web-design-guidelines" });
    expect(textOf(box.root.findByProps({ "data-testid": "remote-preview" }))).toContain("Added");
  });

  test("no match anywhere offers to write that skill", async () => {
    searchReply = () => ({ ok: true, skills: [] });
    const box = await mount();
    await type(box, "zzzz");
    expect(textOf(box.root)).toContain('Nothing installed and nothing on skills.sh matches "zzzz".');
    const chip = button(box, 'Write a "zzzz" skill');
    await act(async () => { chip.props.onClick(); });
    expect(location.search).toContain("new=1");
    expect(location.search).toContain("desc=zzzz");
  });

  test("New skill opens ?new=1 with no description", async () => {
    const box = await mount();
    await act(async () => { button(box, "New skill").props.onClick(); });
    expect(location.search).toBe("?new=1");
  });

  test("a deep link ?skill= opens that skill instead of the list", async () => {
    setLocation("?skill=" + encodeURIComponent("/h/.agents/skills/ui-design-review"));
    const box = await mount();
    expect(textOf(box.root)).not.toContain("Type 2+ letters");
  });
});
