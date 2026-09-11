// NO EMOJI IN THE CHAT'S UI (Akshil, 2026-09-09, P2-7).
//
// The port shipped 📄/🖼 on the attachment chips and receipts, 📌 on a comment
// row, and 📌/🖼/📄 in the markers a wordless send's bubble shows. Every icon in
// this app comes from `lucide-react`, and an emoji is the one glyph an app
// cannot draw: it arrives at a weight and a hue the platform font picked and is
// a different picture on each OS.
//
// A GREP, not a render assertion, because the failure mode is a new one being
// typed somewhere else next week — the icon vocabulary is `ui/AttachIcon` and a
// literal in any other file is the bug. Comments are stripped first: a line
// EXPLAINING which emoji was replaced is exactly the note that must survive.
import { expect, test } from "bun:test";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

/** Pictographs and the variation selector that dresses a symbol up as one.
 *  NOT the plain symbol ranges: `✕`, `✓`, `✗`, `←`, `→` and `⋮` are T's own
 *  typographic marks, they take the surrounding ink and metrics, and every one
 *  of them is in the template verbatim. */
const EMOJI = /[\u{1F000}-\u{1FAFF}\u{FE0F}]|[\u{2600}-\u{27BF}]\u{FE0F}/u;

/** Line and block comments out, so a note about the emoji that was removed can
 *  stay where the reader will need it. Strings are deliberately kept. */
function code(src: string): string {
  return src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
}

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else if (/\.(ts|tsx|css)$/.test(name) && !/\.test\.tsx?$/.test(name)) out.push(p);
  }
  return out;
}

test("no emoji literal anywhere in the chat's own source", () => {
  const root = join(import.meta.dir, "..");
  const bad: string[] = [];
  for (const file of walk(root)) {
    const lines = code(readFileSync(file, "utf8")).split("\n");
    lines.forEach((line, i) => {
      if (EMOJI.test(line)) bad.push(file.slice(root.length + 1) + ":" + (i + 1) + " " + line.trim());
    });
  }
  expect(bad).toEqual([]);
});
