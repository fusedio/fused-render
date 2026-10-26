// The `/install` page: where a `fused-render://open?url=` (SPEC §26 DL-8) or
// `open?git=` (DL-9) deep link lands. GET /clone redirected here with the
// http(s) link in `?_fetch_appfile=` or the GitHub repo link in
// `?_fetch_appgit=`; the page downloads it through the guarded POST
// /api/appfile/fetch — into ~/.fused-render/downloads/<app_id>.fused, one
// file per app however many links point at it — and then opens the saved
// file the way a Finder double-click on a `.fused` does (D390): THIS document
// hard-loads the file's chrome-free EMBED url, where the install page
// (apps/marketplace FusedFilePage, AF-14) is the confirm. Nothing in the file
// runs until the user installs it there.
//
// A page, not a toast over Home (what the boot handler it replaces did): a
// deep link always arrives in a FRESH window (app.py `_open_target` →
// WindowManager.open), and a window that shows Home with a small banner in
// a corner reads as "nothing is happening" while a git clone runs. Here the
// source, the running clock and the one state are the whole window, and a
// failure is read on the page with Retry rather than lost in a toast.
//
// The same page reports a MALFORMED `fused-render://open?file=` link (DL-7):
// the server ferries a payload it could not parse here under
// `?_edit_appfile=`; a well-formed one is redirected straight to the file's
// install page and never reaches this page.
//
// Reload re-runs the download: the fetch is idempotent (one file per app),
// so a reload mid-download is a retry, which is what a reader who reloads
// a stuck page wants.
import { useEffect, useRef, useState } from "react";
import { AlertTriangle, Download, GitBranch, Link2 } from "lucide-react";

import { fetchAppFile } from "@platform/lib/api";
import { useDocumentTitle } from "@platform/lib/hooks";
import { embedUrlForFsPath, navigateUrl } from "@platform/lib/router";

import {
  editAppFileFromSearch,
  fetchAppFileFromSearch,
  fetchAppGitFromSearch,
} from "./edit-appfile-lib";

type Source = { kind: "git"; link: string } | { kind: "url"; link: string };

type Phase =
  | { state: "idle"; error: string }
  | { state: "running" }
  | { state: "opening"; file: string }
  | { state: "failed"; error: string };

/** The source the URL names, or the malformed-link error to show. */
function readSource(search: string): { source: Source | null; error: string | null } {
  const bad = editAppFileFromSearch(search);
  if (bad) {
    return { source: null, error: "Could not open the app file link: not an absolute .fused path — " + bad };
  }
  const git = fetchAppGitFromSearch(search);
  if (git) return { source: { kind: "git", link: git }, error: null };
  const url = fetchAppFileFromSearch(search);
  if (url) {
    if (!/^https?:\/\//i.test(url)) {
      // The server ferried a malformed payload verbatim so the error reads
      // here, in-app, rather than on a page of its own.
      return { source: null, error: "Could not open the app link: not an http(s) URL — " + url };
    }
    return { source: { kind: "url", link: url }, error: null };
  }
  return { source: null, error: "This page opens app links. Nothing to install was named." };
}

/** "github.com/owner/repo" — the link without its scheme, for the headline. */
function shortLink(link: string): string {
  return link.replace(/^https?:\/\//i, "").replace(/\/+$/, "");
}

function useElapsed(running: boolean): number {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    if (!running) return;
    setSeconds(0);
    const started = Date.now();
    const id = setInterval(() => setSeconds(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => clearInterval(id);
  }, [running]);
  return seconds;
}

function clock(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m > 0 ? `${m}:${String(s).padStart(2, "0")}` : `${s}s`;
}

export default function InstallLinkPage() {
  const [{ source, error: badLink }] = useState(() => readSource(location.search));
  const [phase, setPhase] = useState<Phase>(() =>
    source ? { state: "running" } : { state: "idle", error: badLink || "" },
  );
  const [attempt, setAttempt] = useState(0);
  const alive = useRef(true);
  const running = phase.state === "running";
  const seconds = useElapsed(running);

  useDocumentTitle(source ? "Installing " + shortLink(source.link) : "Install");

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    if (!source) return;
    setPhase({ state: "running" });
    const src: { url: string } | { git: string } =
      source.kind === "git" ? { git: source.link } : { url: source.link };
    void (async () => {
      try {
        const r = await fetchAppFile(src);
        if (!alive.current) return;
        setPhase({ state: "opening", file: r.file });
        // A full load, not navigateUrl: the embed-vs-view prefix (IS_EMBED,
        // IS_TOP_EMBED) is read once at module init, so a pushState onto the
        // embed url would keep the shell's chrome and never mount the
        // EmbedStrip — the same rule EmbedStrip applies going back.
        // `replace`, not `assign`: this page must not stay in history, or Back
        // from the install page lands here and re-runs the download forward.
        location.replace(embedUrlForFsPath(r.file));
      } catch (e) {
        if (!alive.current) return;
        setPhase({ state: "failed", error: (e as Error).message || "download failed" });
      }
    })();
    // `attempt` is the Retry button; `source` is fixed for the page's life.
  }, [source, attempt]);

  const isGit = source?.kind === "git";
  const SourceIcon = isGit ? GitBranch : Link2;
  const sourceWord = isGit ? "GitHub repository" : "App file link";

  // What the one line under the title says per phase. A git link is a clone
  // and a build, so after a while the line says so rather than looking stuck.
  const slow = running && seconds >= 20;
  let headline: string;
  let detail: string;
  if (phase.state === "running") {
    headline = isGit ? "Downloading from GitHub…" : "Downloading app file…";
    detail = slow
      ? isGit
        ? "Still cloning the repository. A large repository or a slow connection can take a minute."
        : "Still downloading. A large file or a slow connection can take a minute."
      : isGit
        ? "Cloning the repository and reading the app at its root. Nothing runs until you install it."
        : "Fetching the file. Nothing runs until you install it.";
  } else if (phase.state === "opening") {
    headline = "Opening the install page…";
    detail = "The app file is saved. The next page shows what installing would do.";
  } else if (phase.state === "failed") {
    headline = "Could not download the app";
    detail = phase.error;
  } else {
    headline = "Could not open the link";
    detail = phase.error;
  }

  const busy = phase.state === "running" || phase.state === "opening";

  return (
    <div id="content" className="install-link">
      <div className="install-link-card" role="status" aria-live="polite" data-state={phase.state}>
        <div className="install-link-mark" aria-hidden="true">
          {busy ? <span className="mode-icon-spinner" /> : phase.state === "failed" || phase.state === "idle" ? <AlertTriangle size={22} /> : <Download size={22} />}
        </div>
        <h1 className="install-link-title">{headline}</h1>
        <p className="install-link-detail">{detail}</p>
        {source && (
          <div className="install-link-source">
            <SourceIcon size={14} aria-hidden="true" />
            <span className="install-link-source-kind">{sourceWord}</span>
            <code className="install-link-source-link" title={source.link}>
              {shortLink(source.link)}
            </code>
          </div>
        )}
        <div className="install-link-foot">
          {busy && (
            <span className="install-link-clock" aria-label="elapsed">
              {clock(seconds)}
            </span>
          )}
          {phase.state === "failed" && (
            <button type="button" className="install-link-btn primary" onClick={() => setAttempt((n) => n + 1)}>
              Try again
            </button>
          )}
          <button
            type="button"
            className="install-link-btn"
            onClick={() => navigateUrl("/")}
          >
            {busy ? "Cancel" : "Go to Home"}
          </button>
        </div>
      </div>
    </div>
  );
}
