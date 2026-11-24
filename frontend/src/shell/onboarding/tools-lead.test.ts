import { expect, test } from "bun:test";
import { toolsLead } from "./tools-lead";

const base = { showGit: true, chromeApplies: true, appName: "Render" };

test("names Chrome for bots when Chrome applies", () => {
  expect(toolsLead(base)).toContain("drives Google Chrome for bots");
  expect(toolsLead({ ...base, showGit: false })).toContain("drives Google Chrome for bots");
});

test("does not claim Chrome when it is n/a", () => {
  for (const showGit of [true, false]) {
    expect(toolsLead({ ...base, showGit, chromeApplies: false })).not.toContain("Chrome");
  }
  expect(toolsLead({ ...base, chromeApplies: false })).toContain("installs Community apps with git");
});
