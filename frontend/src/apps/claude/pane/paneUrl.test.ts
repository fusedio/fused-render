import { describe, expect, test } from "bun:test";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

const {
  curLeftEntry,
  decidePane,
  footnoteFor,
  homePlaceholderFor,
  paneModeLabel,
  paneModeLetter,
  paneModeIconUrl,
  paneNounFor,
  paneOfferable,
  paneSrcFor,
  annotateLabelFor,
  annIdleTitleFor,
  shotLabelFor,
} = await import("./paneUrl");
type StatResult = import("@platform/lib/api").StatResult;
type TemplateEntry = import("@platform/lib/api").TemplateEntry;

function stat(over: Partial<StatResult> = {}): StatResult {
  return {
    path: "/w/notes.md",
    name: "notes.md",
    is_dir: false,
    size: 12,
    mtime: 1,
    templates: [],
    ...over,
  };
}

const tpl = (mode: string, over: Partial<TemplateEntry> = {}): TemplateEntry => ({
  mode,
  path: "/t/" + mode + "/template.html",
  icon: "/t/" + mode + "/icon.svg",
  ...over,
});

describe("paneOfferable", () => {
  test("drops the chat mode (never frame this view inside its own pane)", () => {
    const out = paneOfferable([tpl("markdown"), tpl("claude"), tpl("code")]);
    expect(out.map((e) => e.mode)).toEqual(["markdown", "code"]);
  });

  test("drops conditional entries — an unresolved gate reads as not offered (CT-12)", () => {
    const out = paneOfferable([tpl("markdown"), tpl("duckdb", { conditional: true })]);
    expect(out.map((e) => e.mode)).toEqual(["markdown"]);
  });

  test("keeps stat's order, so the first offerable entry is the default", () => {
    const out = paneOfferable([tpl("claude"), tpl("code"), tpl("markdown")]);
    expect(out[0].mode).toBe("code");
  });

  test("undefined templates is an empty list, not a throw", () => {
    expect(paneOfferable(undefined)).toEqual([]);
  });
});

describe("curLeftEntry", () => {
  const entries = [tpl("markdown"), tpl("code")];
  test("leftmode names an offerable entry", () => {
    expect(curLeftEntry(entries, "code")?.mode).toBe("code");
  });
  test("unknown leftmode falls back SILENTLY to the default (SPEC PT-9)", () => {
    expect(curLeftEntry(entries, "duckdb")?.mode).toBe("markdown");
  });
  test("unset leftmode is the default", () => {
    expect(curLeftEntry(entries, undefined)?.mode).toBe("markdown");
    expect(curLeftEntry(entries, "")?.mode).toBe("markdown");
  });
  test("no entries at all is null, not a throw", () => {
    expect(curLeftEntry([], "code")).toBeNull();
  });
});

describe("paneSrcFor", () => {
  test("a template entry frames the template with the target as _file", () => {
    expect(paneSrcFor(tpl("markdown"), "/w/a b.md")).toBe(
      "/render?path=%2Ft%2Fmarkdown%2Ftemplate.html&_file=%2Fw%2Fa%20b.md",
    );
  });

  test("_remote=1 is forwarded exactly as the shell's own iframe does", () => {
    expect(paneSrcFor(tpl("code"), "/w/x.py", true)).toContain("&_remote=1");
    expect(paneSrcFor(tpl("code"), "/w/x.py", false)).not.toContain("_remote");
  });

  test("the _render sentinel (PT-12) is a bare /render on the FILE", () => {
    expect(paneSrcFor({ mode: "_render", path: null }, "/w/page.html")).toBe("/render?path=%2Fw%2Fpage.html");
  });

  test("an offerable non-sentinel entry with no path throws", () => {
    expect(() => paneSrcFor({ mode: "code", path: null }, "/w/x.py")).toThrow(/no template \(code\)/);
  });

  test("the shell-mounted flags ride on the end, and are idempotent", () => {
    const once = paneSrcFor(tpl("code"), "/w/x.py", false, { noFocus: true, preview: true });
    expect(once).toContain("_nofocus=1");
    expect(once).toContain("_preview=1");
    expect(once.match(/_nofocus/g)?.length).toBe(1);
  });
});

describe("decidePane — the four kinds", () => {
  test("APP FOLDER: ./app.py resolves an entry → project, /render on the entry", () => {
    const d = decidePane({
      file: "/w/app",
      chatOnly: false,
      stat: stat({ is_dir: true }),
      appEntry: { entry: "/w/app/main.html" },
    });
    expect(d.kind).toBe("project");
    expect(d.src).toBe("/render?path=%2Fw%2Fapp%2Fmain.html");
    expect(d.noun).toBe("project");
    expect(d.paneNoun).toBe("app");
    expect(d.entry).toBe("/w/app/main.html");
    // An app folder has exactly one thing to frame, so there is no picker.
    expect(d.leftModes).toEqual([]);
  });

  test("ORDINARY FOLDER: no entry → NO PANE (D239), and NOT a throw", () => {
    const d = decidePane({ file: "/w/dl", chatOnly: false, stat: stat({ is_dir: true }), appEntry: {} });
    expect(d.kind).toBe("none");
    expect(d.src).toBeNull();
    expect(d.noun).toBe("folder");
    // "the entry html the pane is rendering", and nothing of ours is.
    expect(d.entry).toBe("");
  });

  test("FILE: the offerable views, first is the default, target is the entry", () => {
    const d = decidePane({
      file: "/w/notes.md",
      chatOnly: false,
      stat: stat({ templates: [tpl("markdown"), tpl("claude"), tpl("code")], remote: true }),
    });
    expect(d.kind).toBe("file");
    expect(d.noun).toBe("file");
    expect(d.paneNoun).toBe("preview");
    expect(d.leftModes.map((e) => e.mode)).toEqual(["markdown", "code"]);
    expect(d.framedMode).toBe("markdown");
    expect(d.entry).toBe("/w/notes.md");
    expect(d.remote).toBe(true);
    expect(d.src).toContain("&_remote=1");
  });

  test("FILE with leftmode: the named view is what is framed", () => {
    const d = decidePane({
      file: "/w/notes.md",
      chatOnly: false,
      stat: stat({ templates: [tpl("markdown"), tpl("code")] }),
      leftMode: "code",
    });
    expect(d.framedMode).toBe("code");
    expect(d.src).toContain("code%2Ftemplate.html");
  });

  test("FILE with no offerable view throws (the pane WE have to fill)", () => {
    expect(() =>
      decidePane({ file: "/w/x.bin", chatOnly: false, stat: stat({ templates: [tpl("claude")] }) }),
    ).toThrow(/no preview view for this file/);
  });
});

describe("decidePane — chat_only always answers none", () => {
  test("an app folder: the noun is still reported, `entry` is NOT", () => {
    const d = decidePane({
      file: "/w/app",
      chatOnly: true,
      stat: stat({ is_dir: true }),
      appEntry: { entry: "/w/app/main.html" },
    });
    expect(d.kind).toBe("none");
    expect(d.src).toBeNull();
    expect(d.noun).toBe("project");
    expect(d.paneNoun).toBe("app");
    expect(d.entry).toBe("");
  });

  test("an ordinary folder", () => {
    const d = decidePane({ file: "/w/dl", chatOnly: true, stat: stat({ is_dir: true }), appEntry: {} });
    expect(d.kind).toBe("none");
    expect(d.noun).toBe("folder");
  });

  test("a file: checked BEFORE the entry lookup, so a file with no view does NOT throw", () => {
    const d = decidePane({ file: "/w/x.bin", chatOnly: true, stat: stat({ templates: [tpl("claude")] }) });
    expect(d.kind).toBe("none");
    expect(d.noun).toBe("file");
    expect(d.leftModes).toEqual([]);
  });
});

describe("the nouns: ONE writer for every piece of chrome", () => {
  test("paneNoun: a project's pane is the user's app, everything else is a preview", () => {
    expect(paneNounFor("project")).toBe("app");
    expect(paneNounFor("file")).toBe("preview");
    expect(paneNounFor("folder")).toBe("preview");
    expect(paneNounFor("")).toBe("preview");
  });

  test("the placeholder is kind-derived, and kind-FREE until the kind is known", () => {
    expect(homePlaceholderFor("project")).toBe("Ask Claude about this project…");
    expect(homePlaceholderFor("")).toBe("Ask Claude…");
  });

  test('the footnote: "files in this file" is not a sentence', () => {
    expect(footnoteFor("file")).toBe("Claude can read and edit this file and the assets it references.");
    expect(footnoteFor("folder")).toBe("Claude can read and edit files in this folder.");
    expect(footnoteFor("project")).toBe("Claude can read and edit files in this project.");
  });

  test("the pane chrome speaks the PANE noun, not the target noun", () => {
    expect(annotateLabelFor("app")).toBe("Comment on the app");
    expect(shotLabelFor("preview")).toBe("Screenshot the preview and attach it to this message");
  });
});

describe("the picker's labels and icons", () => {
  test("the surface names, not the registry keys", () => {
    expect(paneModeLabel("_render")).toBe("Preview");
    expect(paneModeLabel("git")).toBe("Source Control");
    expect(paneModeLabel("app")).toBe("Preview");
    expect(paneModeLabel("_app")).toBe("Preview");
    expect(paneModeLabel("history")).toBe("History");
  });

  test("the fall-through is the shell's own humanizer, so the picker reads like the mode switcher", () => {
    expect(paneModeLabel("code")).toBe("Code");
    expect(paneModeLabel("duckdb")).toBe("DuckDB");
  });

  test("the icon is a currentColor mask served by /api/fs/raw; no icon → a lettered box", () => {
    expect(paneModeIconUrl("/t/code/icon.svg")).toBe('url("/api/fs/raw?path=%2Ft%2Fcode%2Ficon.svg")');
    expect(paneModeIconUrl(null)).toBeNull();
    expect(paneModeLetter("duckdb")).toBe("D");
  });
});

describe("the framing flags' EXACT shape", () => {
  test("`_preview` comes before `_nofocus`, as T spells it", () => {
    // T:10717 is the one site that emits both: `paneSrcFor(t, path, remote) +
    // "&_preview=1&_nofocus=1"`. Nothing reads either flag positionally, so
    // this is literal parity — but the inventory pins these URLs as an EXACT
    // shape, and a snapshot test or a log grep written to T's spelling misses
    // on a src that reads `&_nofocus=1&_preview=1`.
    const src = paneSrcFor(tpl("markdown"), "/w/a.md", false, {
      preview: true,
      noFocus: true,
    });
    expect(src.endsWith("&_preview=1&_nofocus=1")).toBe(true);
  });

  test("either flag alone still lands", () => {
    expect(paneSrcFor(tpl("markdown"), "/w/a.md", false, { preview: true })).toContain(
      "_preview=1",
    );
    const only = paneSrcFor(tpl("markdown"), "/w/a.md", false, { noFocus: true });
    expect(only).toContain("_nofocus=1");
    expect(only).not.toContain("_preview");
  });
});

describe("the Comment seat's two sentences", () => {
  test("the idle tooltip is kind-correct and names both halves of the gesture", () => {
    // T:7505-7508 `annIdleTitle`, extracted there because two writers spelled
    // it out and the first disarm threw the resolved noun away.
    expect(annIdleTitleFor("preview")).toBe(
      "Comment on the preview, then send the notes to Claude",
    );
    expect(annIdleTitleFor("app")).toBe("Comment on the app, then send the notes to Claude");
  });

  test("the spoken name is the short half of the same sentence", () => {
    expect(annIdleTitleFor("app").startsWith(annotateLabelFor("app"))).toBe(true);
  });
});
