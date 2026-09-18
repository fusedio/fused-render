// The Mod+K global search overlay (SPEC-index-plugins.md Part 3): one
// in-app dialog, not an OS-wide hotkey, with two groups — "Files" (the home
// search's own corpus) and "Apps". Apps is sourced from the LIVE workspace
// walk (GET /api/apps, the same data the Apps hub renders), not from the
// index: there is no built-in "apps" index kind (it was tried and dropped —
// see DECISIONS-index-plugins.md and specs/index-plugins.md §3, an
// unscheduled, easily-stale duplicate of this same live walk at a scale
// that never justified a DuckDB/parquet store). Matching against the fetched
// catalog is client-side, the same `fuzzyMatch` the Files group's highlight
// already uses.
//
// HARD CONSTRAINTS this file exists to keep:
//   - NO CROSS-SOURCE SCORE CALIBRATION, EVER. Each group is ranked entirely
//     within itself and rendered in its own order — there is no merged
//     list, no shared score axis, nothing here ever compares a file's score
//     to an app's.
//   - The file group reuses `indexRank` — the very same `/api/index/rank`
//     call, `resolve_query`/`search_ranked` underneath it, that the in-folder
//     home search already uses (apps/explorer/FilesHome.tsx). No second
//     matcher, no second grammar.
//   - Per-keystroke cancellation via `AbortController`/`opts.signal` for the
//     files group, the same pattern FilesHome's own rank effect uses — never
//     a debounce timer racing a fast typist. The apps group has nothing to
//     cancel per keystroke: the catalog is fetched ONCE (on open) and every
//     keystroke after that just re-filters the already-fetched list in
//     memory, so a slow apps fetch can never block (or be raced by) the
//     files group's per-keystroke requests.
//   - Opens a result. Running an app or firing a command from a result is
//     out of scope (SPEC-index-plugins.md) — an app hit navigates to its
//     OWN management page (AppPage.tsx), the same as clicking it in the
//     Apps hub, never launches or runs anything.
//
// Chassis: `Modal` (platform/ui/modal/Modal.tsx) gives this the same
// focus trap / Esc / backdrop-close / exit animation every other dialog in
// the app has, rather than a second hand-rolled implementation of any of
// that — only the overlay LOCK (`acquireOverlay`/`releaseOverlay`) and the
// Mod+K-toggles-closed behaviour are this component's own, exactly the two
// things `ShortcutsOverlay` also had to add on top of `Modal` itself.
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import Modal from "@platform/ui/modal/Modal";
import { getApps, indexRank, type AppInfo, type IndexRankHit } from "@platform/lib/api";
import { acquireOverlay, releaseOverlay } from "@platform/lib/ui-overlay";
import { isMod } from "@platform/lib/platform";
import { navigate, navigateUrl } from "@platform/lib/router";
import { appPageUrl } from "@shell/current-apps-lib";
import { fuzzyMatch, highlightSegments } from "@platform/lib/fuzzy";

// Per-group cap. Not a page of results to scroll — a fast way to the ONE
// thing being typed for, so a short list beats a long one here.
const GROUP_LIMIT = 8;

type FileGroup = { base: string; hits: IndexRankHit[] };

// Exported purely so a test can exercise the "~/" rebasing rule directly —
// a hit under a mount (or any other configured root that is not the home
// directory) must never be mislabeled as if it lived under "~/" — the same
// reason ActivityDock.tsx exports `retiredEngines` rather than only reaching
// it through a full render.
export function displayText(hit: IndexRankHit, base: string, home: string): string {
  // Same "~/" rebasing FilesHome's own hit rows use (`hit.path.startsWith(
  // home + "/")`) — but keyed on whether `base` IS the home directory, not
  // merely non-empty. `/api/index/rank`'s `base` is whatever root the query
  // actually resolved against — a mount, or any other configured root — and
  // rendering every one of those as "~/..." labelled two different files
  // under two different roots identically as "~/foo/bar.ts".
  return base === home ? "~/" + hit.rel : hit.rel;
}

function HighlightedText({ text, query }: { text: string; query: string }) {
  // fuzzy.ts stays the single source of truth for what highlights — the
  // server never sends positions on the wire (api.ts's own IndexRankHit
  // docstring), so this re-runs the exact same matcher the ranking itself
  // is built on, over the row already returned.
  const m = query ? fuzzyMatch(query, text) : null;
  if (!m) return <>{text}</>;
  const segs = highlightSegments(text, m.positions);
  return (
    <>
      {segs.map((s, i) =>
        s.match ? (
          <mark key={i} className="gso-mark">
            {s.text}
          </mark>
        ) : (
          <span key={i}>{s.text}</span>
        ),
      )}
    </>
  );
}

function FileRow({
  hit,
  base,
  home,
  query,
  onOpen,
}: {
  hit: IndexRankHit;
  base: string;
  home: string;
  query: string;
  onOpen: () => void;
}) {
  const text = displayText(hit, base, home);
  return (
    <button type="button" className="gso-row" onClick={onOpen}>
      <span className="gso-row-name">
        <HighlightedText text={text} query={query} />
      </span>
      {hit.is_dir && <span className="gso-row-tag">Folder</span>}
    </button>
  );
}

function AppRow({ app, query, onOpen }: { app: AppInfo; query: string; onOpen: () => void }) {
  // Same fallback the Apps hub's own card uses (AppPreviewCard.tsx,
  // Apps.tsx's list filter) — an app's `title` is the human-facing name when
  // set, its folder-derived `name` otherwise.
  const name = app.title || app.name;
  return (
    <button type="button" className="gso-row" onClick={onOpen}>
      <span className="gso-row-name">
        <HighlightedText text={name} query={query} />
      </span>
      <span className="gso-row-tag">App</span>
    </button>
  );
}

export default function GlobalSearchOverlay({
  home,
  onClose,
}: {
  home: string;
  onClose: () => void;
}) {
  const [q, setQ] = useState("");
  const [files, setFiles] = useState<FileGroup | null>(null);
  const [appCatalog, setAppCatalog] = useState<AppInfo[] | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // `Modal` owns focus trap / Esc / backdrop but not the shared overlay
  // registry — its callers do (ShortcutsOverlay's own comment). Without this
  // hold, Listing's document-level nav/file-op handlers keep firing behind
  // this dialog.
  useLayoutEffect(() => {
    acquireOverlay();
    return () => releaseOverlay();
  }, []);

  // Mod+K toggles closed while open — Modal already closes on Esc, so only
  // the second chord this dialog owns needs its own listener.
  useLayoutEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isMod(e) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Files: per-keystroke, cancellable — never a debounce. Backspacing or
  // retyping abandons whatever the previous keystroke asked for.
  useEffect(() => {
    const query = q.trim();
    if (!query) {
      setFiles({ base: home, hits: [] });
      return;
    }
    const filesCtl = new AbortController();
    indexRank(home, query, { signal: filesCtl.signal, limit: GROUP_LIMIT }).then(
      (res) => {
        if (filesCtl.signal.aborted) return;
        setFiles({ base: res.base, hits: res.covered ? res.hits : [] });
      },
      (err: Error) => {
        if (filesCtl.signal.aborted || err.name === "AbortError") return;
        setFiles({ base: home, hits: [] });
      },
    );
    return () => filesCtl.abort();
  }, [q, home]);

  // Apps: fetched ONCE per mount (the same live listing the Apps hub
  // renders, GET /api/apps — there is no "apps" index kind to rank per
  // keystroke, see the header comment). Every keystroke after that just
  // re-filters this already-fetched catalog in memory below, so a slow
  // fetch here can never hold up (or race) the files group above.
  useEffect(() => {
    let alive = true;
    getApps().then(
      (r) => alive && setAppCatalog(r.apps),
      () => alive && setAppCatalog([]),
    );
    return () => {
      alive = false;
    };
  }, []);

  const appHits = useMemo(() => {
    const query = q.trim();
    const catalog = appCatalog ?? [];
    if (!query) return [];
    return catalog
      .map((app) => ({ app, m: fuzzyMatch(query, app.title || app.name) }))
      .filter((r): r is { app: AppInfo; m: NonNullable<typeof r.m> } => r.m !== null)
      .sort((a, b) => b.m.score - a.m.score)
      .slice(0, GROUP_LIMIT)
      .map((r) => r.app);
  }, [appCatalog, q]);

  const openFile = (hit: IndexRankHit, base: string) => {
    navigate(base + "/" + hit.rel, { isDir: hit.is_dir });
    onClose();
  };
  const openApp = (app: AppInfo) => {
    navigateUrl(appPageUrl(app.path));
    onClose();
  };

  const fileHits = files?.hits ?? [];
  const searched = q.trim().length > 0;
  const nothingFound =
    searched && files !== null && appCatalog !== null && fileHits.length === 0 && appHits.length === 0;

  return (
    <Modal
      title="Search"
      ariaLabel="Search files and apps"
      onClose={onClose}
      width={560}
      dialogClassName="gso-dialog"
      plainBody
      initialFocus={inputRef}
    >
      <input
        ref={inputRef}
        className="gso-input"
        type="text"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Search files and apps"
        aria-label="Search files and apps"
        autoComplete="off"
        spellCheck={false}
      />
      {searched && (
        <div className="gso-results">
          {fileHits.length > 0 && (
            <section className="gso-group">
              <h3 className="gso-group-title">Files</h3>
              {fileHits.map((h) => (
                <FileRow
                  key={h.rel}
                  hit={h}
                  base={files?.base ?? home}
                  home={home}
                  query={q}
                  onOpen={() => openFile(h, files?.base ?? home)}
                />
              ))}
            </section>
          )}
          {appHits.length > 0 && (
            <section className="gso-group">
              <h3 className="gso-group-title">Apps</h3>
              {appHits.map((app) => (
                <AppRow key={app.path} app={app} query={q} onOpen={() => openApp(app)} />
              ))}
            </section>
          )}
          {nothingFound && <p className="gso-empty">No results.</p>}
        </div>
      )}
    </Modal>
  );
}
