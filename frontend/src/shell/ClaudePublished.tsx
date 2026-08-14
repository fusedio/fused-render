// /claude-published: the pages Claude PUBLISHED from this machine — every page
// it rendered with its Artifact tool, as a grid of preview cards
// (GET /api/claude-artifacts, newest first).
//
// A sibling of /claude-artifacts, not a replacement for it: that page is a
// second door onto the Explorer homepage's folder list — one card per project
// directory holding Claude Code transcripts, a CONVERSATION each. An artifact
// is a THING a conversation produced, and the thing is what someone comes back
// looking for; the two lists answer different questions, so they are two pages.
//
// Every artifact has two bodies and the card has to hold both:
//
//   - the LOCAL file Claude rendered (html or md), which is the only one that
//     can be previewed or edited, and which the user may have since deleted;
//   - the PUBLISHED page on claude.ai, which is the only one that survives that
//     deletion, and the only one that can be shared.
//
// Hence `exists` decides the card's primary destination — the file here while it
// is here, the published page once it is not — and the footer carries a small
// door to the published page either way, so "give me the link" is always one
// click and never has to fight the card for the same click.
//
// The card vocabulary is the /apps hub's (.apps-cards / .app-pcard, plus the
// scaled-iframe thumbnail trick from AppPreviewCard); the page chrome is the
// Claude config family's (.cc-root / .cc-main / .cc-page-head). Only what
// neither has an answer for is local — see the `ca-` section at the end of
// styles/apps.css.
import { useEffect, useMemo, useState } from "react";
import { getClaudeArtifacts, type ClaudeArtifact } from "@platform/lib/api";
import { isBrowserHandledClick } from "@platform/lib/appEntry";
import { basename, formatMtimeFull, timeAgo } from "@platform/lib/format";
import { navigate, urlForFsPath } from "@platform/lib/router";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";

type Loaded<T> =
  | { status: "loading" }
  | { status: "ok"; data: T }
  | { status: "error"; message: string };

// Same pure-CSS scale-down as AppPreviewCard: the iframe renders at 400% of the
// thumb box and is scaled to 25%, so the visual size is exactly the box whatever
// width the grid column resolves to.
const PREVIEW_SCALE = 0.25;

// A card is never blank and never lies about what it is showing: when there is
// no live preview to render, the artifact's own tab emoji stands in for it, and
// an artifact published without one gets the generic page glyph.
const FALLBACK_GLYPH = "📄";

// Whether the local file can be previewed AS ITSELF. `/render` serves a file's
// bytes as html with the runtime injected — exactly right for the html Claude
// publishes, and wrong for the md it also publishes: markdown through that route
// paints as one wall of unwrapped source, which is a worse thumbnail than no
// thumbnail. Rendering md properly means resolving its preview TEMPLATE from the
// registry (`/render?path=<template>&_file=<file>`, see explorer/Preview.tsx),
// which is a per-card round trip this grid has no business making. So md falls
// through to the glyph tile, the same way an app with no page entry does.
function isRenderablePage(fsPath: string): boolean {
  return /\.html?$/i.test(fsPath);
}

// What to call it. The <title> Claude gave the page if it gave one, else the
// file's own name — never a bare "Untitled", which tells the reader nothing they
// could use to find it again.
function labelFor(a: ClaudeArtifact): string {
  return a.title?.trim() || basename(a.file_path);
}

// Client-side filter over the three things a person actually remembers about an
// artifact: what it was called, what it said it was, and what the file is named.
function matchesQuery(a: ClaudeArtifact, q: string): boolean {
  if (q === "") return true;
  return (
    (a.title ?? "").toLowerCase().includes(q) ||
    (a.description ?? "").toLowerCase().includes(q) ||
    basename(a.file_path).toLowerCase().includes(q)
  );
}

function ExternalIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M14 4h6v6" />
      <path d="M20 4l-8.5 8.5" />
      <path d="M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4" />
    </svg>
  );
}

// One artifact.
//
// NOT a single <a> like AppPreviewCard, and the difference is forced: this card
// has two destinations, and an <a> inside an <a> is invalid HTML (nested
// interactive content) — React will build that DOM but no reader should have to
// make sense of it. Instead the card is a plain box, the TITLE is the real link,
// and its stretched ::after covers the whole card (styles/apps.css). The
// browser's own gestures therefore still work anywhere on the card — middle
// click, Cmd/Ctrl-click, "Open in New Tab", copy link — which is the whole
// reason AppPreviewCard is an anchor in the first place. The published-page link
// sits above that overlay (z-index) so it keeps its own click without any
// event-bubbling trickery: it is not inside the primary link to begin with.
function ArtifactCard({ artifact }: { artifact: ClaudeArtifact }) {
  const title = labelFor(artifact);
  const ago = timeAgo(artifact.updated_at);
  const live = artifact.exists && isRenderablePage(artifact.file_path);
  // Gone from disk: the published page is all that is left, so that is where the
  // card goes — in a new tab, because it leaves the app. While the file IS here
  // the card opens it HERE, through the shell's own navigation (the href carries
  // the same target so a new tab lands in the same place).
  const local = artifact.exists;
  const href = local ? urlForFsPath(artifact.file_path) : artifact.remote_url;

  return (
    <div className="app-pcard ca-card">
      <span
        className={
          "app-pcard-thumb ca-card-thumb" +
          (live ? "" : " ca-card-thumb-tinted") +
          (artifact.exists ? "" : " ca-card-thumb-gone")
        }
        // The live preview and the glyph are decoration — the title says the same
        // thing in text. The MISSING notice is not: it is the one piece of state
        // only this box reports, so that card leaves the thumb readable.
        aria-hidden={artifact.exists ? true : undefined}
      >
        {live ? (
          <>
            <iframe
              src={`/render?path=${encodeURIComponent(artifact.file_path)}`}
              style={{
                width: `${100 / PREVIEW_SCALE}%`,
                height: `${100 / PREVIEW_SCALE}%`,
                transform: `scale(${PREVIEW_SCALE})`,
              }}
              loading="lazy"
              tabIndex={-1}
              scrolling="no"
              title=""
            />
            {/* Display-only: the shield keeps every pointer event off the page
                inside the frame, so it lands on the card's stretched link. */}
            <span className="app-pcard-shield" />
          </>
        ) : (
          <>
            <span className="ca-card-glyph">{artifact.favicon || FALLBACK_GLYPH}</span>
            {!artifact.exists && <span className="ca-card-gone">file removed</span>}
          </>
        )}
      </span>
      <span className="app-pcard-body ca-card-body">
        <span className="ca-card-head">
          {/* Repeated from the tinted tile on purpose: the emoji is how the user
              recognises the artifact, and on a card WITH a live preview the tile
              never shows it. */}
          <span className="ca-card-favicon" aria-hidden="true">
            {artifact.favicon || FALLBACK_GLYPH}
          </span>
          <a
            className="ca-card-title"
            href={href}
            target={local ? undefined : "_blank"}
            rel={local ? undefined : "noopener noreferrer"}
            title={local ? artifact.file_path : `${artifact.file_path} (removed) — open ${artifact.remote_url}`}
            onClick={(e) => {
              // Only a plain left click is ours; every modifier and the middle
              // button already mean "somewhere other than this tab" and belong
              // to the href. A remote target is the browser's outright.
              if (!local || isBrowserHandledClick(e)) return;
              e.preventDefault();
              navigate(artifact.file_path);
            }}
          >
            {title}
          </a>
        </span>
        {artifact.description && (
          <span className="ca-card-desc" title={artifact.description}>
            {artifact.description}
          </span>
        )}
        <span className="ca-card-foot">
          <span className="ca-card-ago" title={formatMtimeFull(artifact.updated_at) || undefined}>
            {ago ?? "—"}
          </span>
          {/* Always present, even on the card whose primary link already points
              here: "where is the shareable link" should be answered by the same
              spot on every card, not by first working out whether the local file
              survived. */}
          <a
            className="ca-card-remote"
            href={artifact.remote_url}
            target="_blank"
            rel="noopener noreferrer"
            title={`Open the published page — ${artifact.remote_url}`}
            aria-label={`Open the published page for ${title}`}
          >
            <ExternalIcon />
          </a>
        </span>
      </span>
    </div>
  );
}

export default function ClaudePublished() {
  // One cheap GET on mount — no client-side cache to reconcile, and the shell
  // remounts this page on every navigation anyway (App.tsx keys on nav epoch).
  const [state, setState] = useState<Loaded<ClaudeArtifact[]>>({ status: "loading" });
  const [query, setQuery] = useState("");

  useEffect(() => {
    let alive = true;
    getClaudeArtifacts().then(
      ({ artifacts }) => alive && setState({ status: "ok", data: artifacts }),
      (e: Error) => alive && setState({ status: "error", message: e.message }),
    );
    return () => {
      alive = false;
    };
  }, []);

  const all = state.status === "ok" ? state.data : [];
  const q = query.trim().toLowerCase();
  // No sort control: the server already answers newest-first, and "which one did
  // I just make" is the only ordering anyone wants from a list of artifacts.
  // Filtering never reorders, so a card cannot move under the cursor.
  const shown = useMemo(() => all.filter((a) => matchesQuery(a, q)), [all, q]);

  return (
    <div className="cc-root">
      <main className="cc-main">
        <div className="cc-page-head">
          <div>
            <h2 className="cc-heading">Published</h2>
            <div className="cc-caption">pages Claude published on this machine</div>
          </div>
        </div>

        <div className="cc-toolbar">
          <div className="apps-search">
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <circle cx="11" cy="11" r="7" />
              <path d="M21 21l-4.3-4.3" />
            </svg>
            <input
              type="search"
              placeholder="Search artifacts…"
              aria-label="Search artifacts by title, description or file name"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
          {state.status === "ok" && (
            <div className="cc-count">
              {shown.length === all.length
                ? `${all.length} artifact${all.length === 1 ? "" : "s"}`
                : `${shown.length} of ${all.length} artifacts`}
            </div>
          )}
        </div>

        {state.status === "error" && <ErrorBanner>{state.message}</ErrorBanner>}
        {state.status === "loading" && <SkeletonLines rows={4} label="Loading artifacts" />}
        {state.status === "ok" &&
          (shown.length === 0 ? (
            <div className="cc-empty">
              {all.length === 0 ? (
                <>
                  No Claude artifacts found on this machine.
                  <div className="ca-empty-sub">
                    Artifacts are pages Claude publishes with its Artifact tool — a report, a
                    dashboard, a write-up. Ask it to publish one and it shows up here.
                  </div>
                </>
              ) : (
                "No artifacts match — clear the search."
              )}
            </div>
          ) : (
            <div className="apps-cards">
              {shown.map((a) => (
                // Keyed on the published url, which the server dedupes on — a
                // file republished twice is two artifacts and two cards.
                <ArtifactCard key={a.remote_url} artifact={a} />
              ))}
            </div>
          ))}
      </main>
    </div>
  );
}
