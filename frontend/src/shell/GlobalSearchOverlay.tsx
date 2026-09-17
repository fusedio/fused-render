// The Mod+K global search overlay (SPEC-index-plugins.md Part 3): one
// in-app dialog, not an OS-wide hotkey, over whatever registered index kind
// can answer a query today — "files" (the home search's own corpus) and
// "apps" (the app-name index), each in its own group.
//
// HARD CONSTRAINTS this file exists to keep:
//   - NO CROSS-SOURCE SCORE CALIBRATION, EVER. Each group is ranked entirely
//     by its own `/api/index/rank` call and rendered in the order the server
//     returned it (`listing/ranked-hits.ts`'s own rule) — there is no merged
//     list, no shared score axis, nothing here ever compares a file's score
//     to an app's.
//   - The file group reuses `indexRank` — the very same `/api/index/rank`
//     call, `resolve_query`/`search_ranked` underneath it, that the in-folder
//     home search already uses (apps/explorer/FilesHome.tsx). No second
//     matcher, no second grammar.
//   - Per-keystroke cancellation via `AbortController`/`opts.signal`, the
//     same pattern FilesHome's own rank effect uses — never a debounce timer
//     racing a fast typist.
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
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import Modal from "@platform/ui/modal/Modal";
import { indexRank, indexRankKind, type IndexRankHit } from "@platform/lib/api";
import { acquireOverlay, releaseOverlay } from "@platform/lib/ui-overlay";
import { isMod } from "@platform/lib/platform";
import { navigate, navigateUrl } from "@platform/lib/router";
import { appPageUrl } from "@shell/current-apps-lib";
import { fuzzyMatch, highlightSegments } from "@platform/lib/fuzzy";

// Per-group cap. Not a page of results to scroll — a fast way to the ONE
// thing being typed for, so a short list beats a long one here.
const GROUP_LIMIT = 8;

type FileGroup = { base: string; hits: IndexRankHit[] };

// Exported purely so a test can exercise the "~/" rebasing rule directly
// (review finding 8's own regression: mislabeling a mount-rooted hit as
// "~/...") — the same reason ActivityDock.tsx exports `retiredEngines`
// rather than only reaching it through a full render.
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

function AppRow({ hit, query, onOpen }: { hit: IndexRankHit; query: string; onOpen: () => void }) {
  // An "apps" hit's `rel` is the app folder's own realpath (search_apps_ranked's
  // own contract, index/query.py) — its basename is the closest thing to the
  // app's display name a bare hit carries without a second round trip.
  const name = hit.rel.split("/").filter(Boolean).pop() ?? hit.rel;
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
  const [apps, setApps] = useState<IndexRankHit[] | null>(null);
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

  // Per-keystroke, per-group cancellation — never a debounce. Backspacing or
  // retyping abandons whatever the previous keystroke asked for; the two
  // groups run independently, so a slow apps store can never hold up files
  // (or vice versa), and neither group's ordering is ever touched by the
  // other's answer landing first or last.
  useEffect(() => {
    const query = q.trim();
    if (!query) {
      setFiles({ base: home, hits: [] });
      setApps([]);
      return;
    }
    const filesCtl = new AbortController();
    const appsCtl = new AbortController();
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
    indexRankKind("apps", query, { signal: appsCtl.signal, limit: GROUP_LIMIT }).then(
      (res) => {
        if (appsCtl.signal.aborted) return;
        setApps(res.covered ? res.hits : []);
      },
      (err: Error) => {
        if (appsCtl.signal.aborted || err.name === "AbortError") return;
        setApps([]);
      },
    );
    return () => {
      filesCtl.abort();
      appsCtl.abort();
    };
  }, [q, home]);

  const openFile = (hit: IndexRankHit, base: string) => {
    navigate(base + "/" + hit.rel, { isDir: hit.is_dir });
    onClose();
  };
  const openApp = (hit: IndexRankHit) => {
    navigateUrl(appPageUrl(hit.rel));
    onClose();
  };

  const fileHits = files?.hits ?? [];
  const appHits = apps ?? [];
  const searched = q.trim().length > 0;
  const nothingFound =
    searched && files !== null && apps !== null && fileHits.length === 0 && appHits.length === 0;

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
              {appHits.map((h) => (
                <AppRow key={h.rel} hit={h} query={q} onOpen={() => openApp(h)} />
              ))}
            </section>
          )}
          {nothingFound && <p className="gso-empty">No results.</p>}
        </div>
      )}
    </Modal>
  );
}
