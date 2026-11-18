// An expert's Settings: Save sends only its own keys (never the bot `memory`), and the Skills section lists and edits
// its skills/*.md like a bot's, without "Learn from last task".
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";

const { api } = await import("../lib/api");
const { saveSettings } = await import("./actions");
const { SkillsSection } = await import("./SkillsSection");

/** Static markup, without base-ui's "useLayoutEffect does nothing on the server" warnings. */
function render(el: Parameters<typeof renderToStaticMarkup>[0]): string {
  const err = console.error;
  console.error = () => {};
  try { return renderToStaticMarkup(el); } finally { console.error = err; }
}

const value = {
  name: "Canvas Reference", model: "sonnet", effort: "low", instructions: "Be brief.", memory: "bot memory", approval: "auto",
  buildAccess: "full", terminal: true, encrypt: true, profile: "Default", maxSteps: "50", face: null, imessage: "x", imessageEnabled: true,
  imessageTo: "y", preset: "", kind: "expert", askMeAbout: "Canvases", superAccess: "full", browserId: "b2", trustedApps: ["a"], trustedAppsWas: [],
  connections: ["gmail"], connectionsWas: [], folders: ["/w/maps"], allowRulesRemoved: [],
} as never;

describe("expert settings", () => {
  test("Save sends name, model, effort, instructions, ask_me_about and folders only", async () => {
    const sent: unknown[] = [];
    const orig = { settings: api.settings, flag: api.flag, profile: api.profile };
    api.settings = ((_id: string, body: unknown) => { sent.push(body); return Promise.resolve({ ok: true }); }) as typeof api.settings;
    api.flag = (() => Promise.resolve({ ok: true })) as typeof api.flag;
    api.profile = (() => { throw new Error("an expert has no browser profile"); }) as typeof api.profile;
    try { await saveSettings("x1", value); } finally { Object.assign(api, orig); }
    expect(sent).toEqual([{ name: "Canvas Reference", model: "sonnet", effort: "low", instructions: "Be brief.", ask_me_about: "Canvases", folders: ["/w/maps"] }]);
  });
  test("Skills: the expert's files listed with Edit and delete, write by hand, no Learn from last task", () => {
    const b = { id: "x1", name: "X", status: "idle", seq: 0, kind: "expert",
      skills: [{ name: "edges-and-folders", title: "Connecting nodes", trigger: "edge, folder", body: "## Edges\n- pairs" }] } as never;
    const out = render(createElement(SkillsSection, { b, confirm: async () => true, expert: true }));
    expect(out).toContain('data-name="edges-and-folders"');
    expect(out).toContain(">Connecting nodes<");
    expect(out).toContain('data-op="edit"');
    expect(out).toContain('data-op="delete"');
    expect(out).toContain('id="snew"');
    expect(out).not.toContain('id="slearn"');
    expect(out).not.toContain("Learn from last task");
    const bot = render(createElement(SkillsSection, { b: { ...(b as object), kind: "bot" } as never, confirm: async () => true }));
    expect(bot).toContain('id="slearn"');
  });
});
