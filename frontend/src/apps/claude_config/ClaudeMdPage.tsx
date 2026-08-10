// The CLAUDE.md explorer as its own page (the sidebar's "MD Files" entry,
// route /claude-md). It was the eleventh section of the Config panel until it
// earned a page of its own: unlike every other section it is not a view onto
// ~/.claude at all — it lists the CLAUDE.md-family files of every project on
// the machine — and it was the only one needing a split preview pane, which
// forced the panel into a three-column layout for one of eleven sections.
//
// What came with it, unchanged in behaviour:
//
//   * the split PREVIEW pane, which renders beside the content column rather
//     than inside it, so the path lives here and the section is handed
//     `preview`/`onPreview` exactly as before;
//   * the git BADGE. This page can dirty the config repo — deleting the global
//     ~/.claude/CLAUDE.md goes through the claude_md module, which commits —
//     so it keeps the same badge the panel has rather than reporting that
//     through toasts alone and leaving the commit unreachable from here.
//
// No section nav: there is one section. The heading/caption pair is the panel's
// (cc-heading + cc-caption, naming the files it edits) and the badge rides the
// title row instead of a nav's bottom edge (.cc-page-head).
import { useCallback, useState } from "react";
import { PreviewPane, StatusBadge } from "./bits";
import ClaudeMdSection from "./sections/ClaudeMdSection";

export default function ClaudeMdPage() {
  // The same two change signals the Config panel keeps, for the same reasons:
  //
  //   badgeEpoch   — the section wrote to disk, so the git badge is stale. The
  //                  section already reloaded itself; remounting it from here
  //                  would just double the fetch.
  //   sectionEpoch — the badge itself committed, folding in drift the section
  //                  can't have accounted for. Only this remounts the section.
  const [badgeEpoch, setBadgeEpoch] = useState(0);
  const [sectionEpoch, setSectionEpoch] = useState(0);
  const [preview, setPreview] = useState<string | null>(null);
  const onChanged = useCallback(() => setBadgeEpoch((n) => n + 1), []);
  const onCommitted = useCallback(() => {
    setBadgeEpoch((n) => n + 1);
    setSectionEpoch((n) => n + 1);
  }, []);

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
          <StatusBadge epoch={badgeEpoch} onCommitted={onCommitted} />
        </div>
        {/* Keyed on the commit epoch: a badge commit rewrites state the section
            can't have predicted, so it remounts and rescans. */}
        <div key={sectionEpoch} className="cc-section">
          <ClaudeMdSection onChanged={onChanged} preview={preview} onPreview={setPreview} />
        </div>
      </main>
      {preview && <PreviewPane path={preview} onClose={() => setPreview(null)} />}
    </div>
  );
}
