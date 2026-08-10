// The CLAUDE.md explorer as its own page (the sidebar's "MD Files" entry,
// route /claude-md). It was the eleventh section of the Config panel until it
// earned a page of its own: unlike every other section it is not a view onto
// ~/.claude at all — it lists the CLAUDE.md-family files of every project on
// the machine — and it was the only one needing a split preview pane, which
// forced the panel into a three-column layout for one of eleven sections.
//
// What came with it, unchanged in behaviour: the split PREVIEW pane, which
// renders beside the content column rather than inside it, so the path lives
// here and the section is handed `preview`/`onPreview` exactly as before.
//
// What did NOT come with it: the git BADGE. Only one action here can dirty
// the config repo (deleting the global ~/.claude/CLAUDE.md — the claude_md
// module commits that itself), and every other file listed lives outside
// ~/.claude entirely, so a repo-drift badge mostly reported changes this page
// had nothing to do with. The Config panel remains the place to see and
// commit drift.
//
// No section nav: there is one section. The heading/caption pair is the
// panel's (cc-heading + cc-caption, naming the files it edits).
import { useState } from "react";
import { PreviewPane } from "./bits";
import ClaudeMdSection from "./sections/ClaudeMdSection";

export default function ClaudeMdPage() {
  const [preview, setPreview] = useState<string | null>(null);

  return (
    <div className="cc-root">
      <main className="cc-main">
        <div className="cc-page-head">
          <div>
            <h2 className="cc-heading">CLAUDE.md files</h2>
            <div className="cc-caption cc-mono">
              CLAUDE.md / CLAUDE.local.md across all projects
            </div>
          </div>
        </div>
        <ClaudeMdSection onChanged={() => {}} preview={preview} onPreview={setPreview} />
      </main>
      {preview && <PreviewPane path={preview} onClose={() => setPreview(null)} />}
    </div>
  );
}
