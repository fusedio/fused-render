// `.bookmark` open flow (SB-9, D99) — the `/view/_bookmark?file=<abs path>`
// sentinel route, entered from a Finder double-click (app.py view_url_path)
// or by browsing to a `.bookmark` file in the explorer (App.tsx passes the fs
// path as the `file` prop then). Reads the file via GET /api/bookmark-file,
// resolves its relative paths against the file's own directory
// (lib/bookmark-file.ts bookmarkOpenUrl) and location.replace()s to the
// resulting view — replace, not assign, so Back never lands on the redirect
// page (and a bad file just renders its error, no loop).
import { useEffect, useState } from "react";
import { getBookmarkFile } from "../lib/api";
import { bookmarkOpenUrl } from "../lib/bookmark-file";

export default function BookmarkOpen({ file }: { file?: string }) {
  const target = file ?? new URLSearchParams(location.search).get("file");
  const [error, setError] = useState<string | null>(
    target ? null : "missing `file` query parameter"
  );

  useEffect(() => {
    if (!target) return;
    let alive = true;
    getBookmarkFile(target).then(
      ({ dir, bookmark }) => {
        if (!alive) return;
        try {
          location.replace(bookmarkOpenUrl(dir, bookmark));
        } catch (e) {
          setError((e as Error).message);
        }
      },
      (e: Error) => alive && setError(e.message)
    );
    return () => {
      alive = false;
    };
  }, [target]);

  if (error) {
    return (
      <div className="status-message error">
        Failed to open {target || "bookmark"}: {error}
      </div>
    );
  }
  return <div className="status-message">Opening bookmark…</div>;
}
