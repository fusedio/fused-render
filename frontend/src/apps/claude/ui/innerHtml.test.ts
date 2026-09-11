// D246, ported: the template pins the list of functions allowed to put
// `renderMd` output into an innerHTML with a test, because that list is the
// whole of the chat's XSS surface — model-authored markdown is the one thing on
// the page that becomes markup, and a second render site is a second place for
// the funnel (marked → DOMPurify) to be skipped.
//
// Native has ONE such site by construction: `MarkdownView`. Everything else —
// a tool's input, its output, an option label, a question, a notice — is a text
// node. This test is what keeps that true as the directory grows: a new
// component that needs markdown routes through MarkdownView, and a component
// that genuinely must be its own site gets added to INNER_HTML_SITES with a
// reason.
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { expect, test } from "bun:test";

import { INNER_HTML_SITES } from "./MarkdownView";

const HERE = dirname(new URL(import.meta.url).pathname);
/** The app's root — `ui/`'s parent. The scan is the WHOLE subtree from here. */
const APP = join(HERE, "..");

/** Every source file under `dir`, RECURSIVELY. A non-recursive scan of `ui/`
 *  plus `protocol/` left `pane/`, `params/`, `ClaudeChat.tsx` and
 *  `ChatMount.tsx` unguarded today — and `shots/` and `ann/` land in PR2/PR3. */
function sources(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) {
      out.push(...sources(path));
    } else if (
      /\.tsx?$/.test(entry.name) &&
      !entry.name.endsWith(".test.ts") &&
      !entry.name.endsWith(".test.tsx")
    ) {
      out.push(path);
    }
  }
  return out;
}

test("only the enumerated components put html into the DOM, anywhere in the app", () => {
  const offenders = sources(APP)
    .filter((path) => readFileSync(path, "utf8").includes("dangerouslySetInnerHTML"))
    .map((path) => path.split("/").pop()!.replace(/\.tsx?$/, ""));
  expect(offenders.sort()).toEqual([...INNER_HTML_SITES].sort());
});

test("the protocol layer never renders — it hands back strings", () => {
  for (const path of sources(join(APP, "protocol"))) {
    expect(readFileSync(path, "utf8")).not.toContain("dangerouslySetInnerHTML");
  }
});

test("raw tool text never reaches innerHTML: the chip and the cards are text nodes", () => {
  // The two files that render model-authored BYTES (a command, a file's
  // contents, an option label) must not even mention the escape hatch.
  for (const name of ["ToolChip", "PermCard", "QuestionCard", "NoticeView", "Turn"]) {
    expect(readFileSync(join(HERE, name + ".tsx"), "utf8")).not.toContain(
      "dangerouslySetInnerHTML",
    );
  }
});
