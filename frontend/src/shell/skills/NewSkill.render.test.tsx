// New skill: the Draft with Claude flow, end to end against a stubbed fetch.
// The Claude task is a fake row the test moves from running to done.
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";
import { installDomShim } from "@platform/lib/testDomShim";
import { draftTitle, freeName, renameSkill, splitFrontmatter } from "./model";

installDomShim();
const { NewSkill } = await import("./NewSkill");

function textOf(node: ReactTestInstance | string): string {
  if (typeof node === "string") return node;
  return node.children.map(textOf).join("");
}

const GOOD = "---\nname: design-review\ndescription: Use when reviewing a page.\n---\n\n## Steps\n1. Look.\n";
const realFetch = globalThis.fetch;
let skillCalls: { action: string; body: Record<string, unknown> }[] = [];
let taskPosts: { url: string; body: Record<string, unknown> }[] = [];
let taskStatus = "in_progress";
let draftStatus: Record<string, unknown> = {};
let checkReply: Record<string, unknown> = {};
let gen = 1;
let saveReply: Record<string, unknown> = { ok: true, slug: "design-review", key: "/h/.agents/skills/design-review" };

function reply(data: unknown): Promise<Response> {
  const text = JSON.stringify(data);
  return Promise.resolve({ ok: true, status: 200, json: async () => data, text: async () => text } as unknown as Response);
}
function fakeFetch(url: unknown, init?: RequestInit): Promise<Response> {
  const u = String(url);
  let body: Record<string, unknown> = {};
  try { body = JSON.parse(String(init?.body ?? "{}")); } catch { /* GET */ }
  if (u.endsWith("/claude-config/skills")) {
    const action = String(body.action);
    skillCalls.push({ action, body });
    switch (action) {
      case "draft_check": return reply(checkReply);
      case "draft_start": return reply({ ok: true, staging: "/h/.fused-render/skill-drafts/s1" });
      case "draft_prompt": return reply({ ok: true, prompt: "PROMPT:" + body.text + (body.adapting ? ":adapt" : "") });
      case "draft_status": return reply({ ok: true, ...draftStatus });
      case "draft_save": return reply(saveReply);
      default: return reply({ ok: true });
    }
  }
  if (u.endsWith("/api/tasks/create")) {
    taskPosts.push({ url: u, body });
    return reply({ key: "pending:e1", entry_id: "e1" });
  }
  if (u.includes("/api/tasks/") && (u.endsWith("/send") || u.endsWith("/cancel"))) {
    taskPosts.push({ url: u, body });
    return reply({ ok: true });
  }
  const taskRow = () => ({ key: "s1", entry_id: "e1", status: taskStatus, session_id: "s1", target: "/h/.fused-render/skill-drafts/s1" });
  if (u.startsWith("/api/tasks/changes")) return new Promise((r) => setTimeout(() => r(reply({ generation: ++gen, full: false, rows: [taskRow()], gone: [] })), 100));
  if (u.startsWith("/api/tasks")) return reply({ tasks: [taskRow()], generation: ++gen });
  return reply({});
}

beforeEach(() => {
  skillCalls = [];
  taskPosts = [];
  taskStatus = "in_progress";
  draftStatus = { files: [], text: null, name: "", description: "", problems: [] };
  checkReply = { ok: true, root: "/s/ui", name: "ui-design-review", files: ["SKILL.md", "references/t.md"] };
  saveReply = { ok: true, slug: "design-review", key: "/h/.agents/skills/design-review" };
  globalThis.fetch = fakeFetch as unknown as typeof fetch;
});
const mounted: ReactTestRenderer[] = [];
afterEach(async () => {
  // Unmounting lets go of the shared tasks feed, so no test sees another's rows.
  await act(async () => { for (const b of mounted.splice(0)) b.unmount(); });
  globalThis.fetch = realFetch;
});

const tick = async (ms = 5, n = 3) => {
  for (let i = 0; i < n; i++) await act(async () => { await new Promise((r) => setTimeout(r, ms)); });
};
async function mount(props: Partial<Parameters<typeof NewSkill>[0]> = {}) {
  let box!: ReactTestRenderer;
  const calls = { closed: 0, saved: [] as string[] };
  await act(async () => {
    box = create(createElement(NewSkill, { initialDesc: "", onClose: () => calls.closed++, onSaved: (k: string) => calls.saved.push(k), ...props }));
  });
  mounted.push(box);
  await tick();
  return { box, calls };
}
const change = (n: ReactTestInstance, value: string) =>
  act(async () => { n.props.onChange({ currentTarget: { value }, target: { value }, nativeEvent: { defaultPrevented: false } }); });
const area = (box: ReactTestRenderer) => box.root.findByProps({ id: "new-skill-desc" });
const sourceInput = (box: ReactTestRenderer) => box.root.findByProps({ id: "new-skill-source" });
const button = (box: ReactTestRenderer, label: string) =>
  box.root.findAllByType("button").find((b) => textOf(b).trim() === label)!;

// Claude finishes (the fake row goes done) and leaves this draft in the staging dir.
async function drafted(box: ReactTestRenderer, status: Record<string, unknown>) {
  taskStatus = "done";
  draftStatus = status;
  await act(async () => { button(box, "Draft it").props.onClick({ preventDefault() {} }); });
  await tick();
  await act(async () => { await new Promise((r) => setTimeout(r, 700)); });
  await tick();
}

describe("model helpers", () => {
  test("renameSkill swaps only the frontmatter name", () => {
    expect(renameSkill(GOOD, "design-review-2")).toBe(GOOD.replace("name: design-review", "name: design-review-2"));
    expect(renameSkill("---\ndescription: d\n---\nbody", "x")).toBe("---\nname: x\ndescription: d\n---\nbody");
    expect(renameSkill("body", "x")).toBe("---\nname: x\n---\n\nbody");
  });
  test("splitFrontmatter, freeName and draftTitle", () => {
    expect(splitFrontmatter(GOOD)[1]).toBe("\n## Steps\n1. Look.\n".replace(/^\n/, "\n"));
    expect(splitFrontmatter("no fm")).toEqual(["", "no fm"]);
    expect(freeName("a", (n) => n === "a-2")).toBe("a-3");
    expect(draftTitle("Review pages\nmore")).toBe("Draft skill: Review pages");
    expect(draftTitle("x".repeat(80)).length).toBeLessThan(70);
  });
});

describe("New skill", () => {
  test("opens with the description it was given, Draft it needs one", async () => {
    const { box } = await mount({ initialDesc: "zzzz" });
    expect(area(box).props.value).toBe("zzzz");
    const { box: empty } = await mount();
    expect(button(empty, "Draft it").props.disabled).toBe(true);
    expect(textOf(empty.root)).toContain("Runs as a Claude task. You review the files before anything is saved.");
    expect(textOf(empty.root)).toContain("What Claude writes");
  });

  test("a source path is checked and reported, or its error shown", async () => {
    const { box } = await mount();
    await change(sourceInput(box), "~/s/ui");
    await tick(450, 1);
    await tick();
    expect(skillCalls.find((c) => c.action === "draft_check")?.body.path).toBe("~/s/ui");
    expect(textOf(box.root)).toContain("✓ Found SKILL.md + 1 other file. Claude adapts it rather than starting blank.");
    checkReply = { ok: false, error: "no SKILL.md in that folder" };
    await change(sourceInput(box), "~/s/nope");
    await tick(450, 1);
    await tick();
    expect(textOf(box.root)).toContain("no SKILL.md in that folder");
  });

  test("Draft it stages, builds the prompt in the backend and starts an unqueued task in the staging dir", async () => {
    const { box } = await mount();
    await change(area(box), "Review pages");
    await change(sourceInput(box), "~/s/ui");
    await tick(450, 1);
    await tick();
    await act(async () => { button(box, "Draft it").props.onClick({ preventDefault() {} }); });
    await tick();
    expect(skillCalls.find((c) => c.action === "draft_start")?.body.path).toBe("~/s/ui");
    expect(skillCalls.find((c) => c.action === "draft_prompt")?.body).toMatchObject({ text: "Review pages", adapting: true });
    expect(taskPosts[0].body).toMatchObject({
      prompt: "PROMPT:Review pages:adapt",
      target: "/h/.fused-render/skill-drafts/s1",
      title: "Draft skill: Review pages",
      queue: false,
    });
    const card = textOf(box.root.findByProps({ "data-testid": "draft-card" }));
    expect(card).toContain("Claude is adapting the skill");
    expect(card).toContain("Open task");
    expect(button(box, "Save skill").props.disabled).toBe(true); // still running
  });

  test("when Claude is done the draft shows live, validates, saves and opens the skill", async () => {
    const { box, calls } = await mount({ initialDesc: "Review pages" });
    await drafted(box, { files: ["SKILL.md", "references/a.md"], text: GOOD, name: "design-review", description: "Use when reviewing a page.", problems: [] });
    const aside = textOf(box.root.findByProps({ "data-testid": "draft-aside" }));
    expect(aside).toContain("design-review · 2 files");
    expect(aside).toContain("## Steps");
    expect(textOf(box.root.findByProps({ "data-testid": "draft-card" }))).toContain("Drafted design-review");
    const save = button(box, "Save skill");
    expect(save.props.disabled).toBe(false);
    await act(async () => { save.props.onClick({ preventDefault() {} }); });
    await tick();
    expect(skillCalls.find((c) => c.action === "draft_save")?.body).toMatchObject({ staging: "/h/.fused-render/skill-drafts/s1", placement: "all" });
    expect(calls.saved).toEqual(["/h/.agents/skills/design-review"]);
  }, 15000);

  test("validation problems show inline, disable Save and offer another name", async () => {
    const { box } = await mount({ initialDesc: "Review pages", existing: ["design-review"] });
    await drafted(box, { files: ["SKILL.md"], text: GOOD, name: "design-review", description: "d",
      problems: ["a skill named design-review already exists; pick a different name"] });
    expect(textOf(box.root.findByProps({ "data-testid": "draft-card" }))).toContain("already exists");
    expect(button(box, "Save skill").props.disabled).toBe(true);
    const name = box.root.findByProps({ "aria-label": "A different name" });
    expect(name.props.value).toBe("design-review-2");
    await act(async () => { button(box, "Use this name").props.onClick({ preventDefault() {} }); });
    await tick();
    const write = skillCalls.find((c) => c.action === "draft_write")!;
    expect(write.body).toMatchObject({ path: "SKILL.md", text: GOOD.replace("name: design-review", "name: design-review-2") });
  }, 15000);

  test("Ask for changes sends a follow-up to the same task", async () => {
    const { box } = await mount({ initialDesc: "Review pages" });
    await drafted(box, { files: ["SKILL.md"], text: GOOD, name: "design-review", description: "d", problems: [] });
    await change(box.root.findByProps({ "aria-label": "Ask for changes" }), "shorter please");
    await act(async () => { button(box, "Send").props.onClick({ preventDefault() {} }); });
    await tick();
    const send = taskPosts.find((p) => p.url.endsWith("/send"))!;
    expect(send.url).toBe("/api/tasks/s1/send");
    expect(send.body).toEqual({ text: "shorter please" });
  }, 15000);

  test("Edit before saving writes SKILL.md into the staging dir, not a skills dir", async () => {
    const { box } = await mount({ initialDesc: "Review pages" });
    await drafted(box, { files: ["SKILL.md"], text: GOOD, name: "design-review", description: "d", problems: [] });
    await act(async () => { button(box, "Edit before saving").props.onClick({ preventDefault() {} }); });
    const editor = box.root.findByProps({ "aria-label": "SKILL.md", rows: 16 });
    expect(editor.props.value).toBe(GOOD);
    await change(editor, GOOD + "extra\n");
    await act(async () => { button(box, "Apply edits").props.onClick({ preventDefault() {} }); });
    await tick();
    expect(skillCalls.find((c) => c.action === "draft_write")?.body).toEqual({
      action: "draft_write", staging: "/h/.fused-render/skill-drafts/s1", path: "SKILL.md", text: GOOD + "extra\n" });
  }, 15000);

  test("Discard draft removes the staging dir and closes", async () => {
    const { box, calls } = await mount({ initialDesc: "Review pages" });
    await act(async () => { button(box, "Draft it").props.onClick({ preventDefault() {} }); });
    await tick();
    await act(async () => { await new Promise((r) => setTimeout(r, 300)); });
    await act(async () => { button(box, "Discard draft").props.onClick({ preventDefault() {} }); });
    await tick();
    expect(skillCalls.find((c) => c.action === "draft_discard")?.body.staging).toBe("/h/.fused-render/skill-drafts/s1");
    expect(taskPosts.some((p) => p.url.endsWith("/cancel"))).toBe(true);
    expect(calls.closed).toBe(1);
  });

  test("a task that fails to start discards the staging dir", async () => {
    const { box } = await mount({ initialDesc: "Review pages" });
    const ok = globalThis.fetch;
    globalThis.fetch = ((u: unknown, i?: RequestInit) =>
      String(u).endsWith("/api/tasks/create")
        ? Promise.resolve({ ok: false, status: 500, json: async () => ({ error: "boom" }), text: async () => "" } as unknown as Response)
        : (ok as typeof fetch)(u as string, i)) as typeof fetch;
    await act(async () => { button(box, "Draft it").props.onClick({ preventDefault() {} }); });
    await tick();
    expect(skillCalls.some((c) => c.action === "draft_discard")).toBe(true);
    expect(button(box, "Draft it")).toBeTruthy();
  });
});
