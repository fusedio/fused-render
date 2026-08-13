// The Claude config panel: a native React port of the bundled html+py app that
// used to sit here in an iframe (D125), built out of the shell's own primitives
// so it follows the Light/Dark setting and shares one toast/modal surface with
// the rest of the app instead of shipping a second, Claude-branded one.
//
// The layout is NOT the original app's any more. That app owned the whole
// window and could afford a 230px section nav down the left; inside this shell
// that nav landed directly beside the global sidebar and the page read as two
// sidebars glued together. So the sections are a horizontal TAB STRIP across
// the top, and the horizontal budget the nav was eating goes to the content:
//
//   ┌ cc-tabbar ────────────────────────────────────────────────┐
//   │ Preferences Plugins Marketplaces …          [🕘 History •] │
//   ├ cc-body ───────────────────────────┬──────────────────────┤
//   │ caption (the file this tab edits)  │  preview pane        │
//   │ section content                    │  (MD Files only)     │
//   └────────────────────────────────────┴──────────────────────┘
//
// Two pieces of state, each in the place that suits it:
//
//   * the active SECTION lives in the URL (`?cctab=plugins`) — bookmarkable, and
//     the same navigateUrl pattern the shell's own tab strips use;
//   * the git epoch lives here, because every section can dirty the repo and
//     they all report back through one `onChanged`.
//
// History is deliberately NOT one of the tabs. It is a persistent button at the
// strip's right edge, because it is the only page whose state matters while you
// are looking at some other one: it carries the dirty dot that tells you the
// config has uncommitted drift, and it is where you commit that drift. Profiles
// lives on it too — a profile is a git branch over the same repo, so it belongs
// with the history rather than beside Preferences.
//
// The CLAUDE.md explorer ("MD Files", `?cctab=claudemd`) is a section here.
// It briefly had a page of its own; it is back because the sidebar's CLAUDE
// group reads better with one Config entry than with two peers. It is the one
// section that needs a split PREVIEW pane, which is why .cc-body lays out as
// two columns (content / preview) — the pane renders beside the content column,
// so the preview path lives in this file and the section is handed
// `preview`/`onPreview`. Every other section leaves the second column empty.
// A stale `/claude-md` URL redirects here (shell/App.tsx).
//
// A note on remounting: the shell renders this page keyed on the nav epoch, so
// any navigation — including this panel's own `?cctab=` writes — remounts it.
// That is why nothing here tries to cache across a section change: a section
// switch IS a fresh mount, and each section refetches exactly as the original
// app re-rendered.
import { useCallback, useState } from "react";
import { navigateUrl } from "@platform/lib/router";
import { Icon, PreviewPane, useGitStatus } from "./bits";
import ClaudeMdSection from "./sections/ClaudeMdSection";
import HistorySection from "./sections/HistorySection";
import MarketplacesSection from "./sections/MarketplacesSection";
import McpSection from "./sections/McpSection";
import MemorySection from "./sections/MemorySection";
import PluginsSection from "./sections/PluginsSection";
import PreferencesSection from "./sections/PreferencesSection";
import SkillsSection from "./sections/SkillsSection";
import StatuslineSection from "./sections/StatuslineSection";

// The tab strip, in order. `file` is the caption under the strip — it names the
// file (or the git object) the section actually edits, which is the one thing a
// settings UI over someone's dotfiles owes them.
const TABS = [
  { id: "preferences", label: "Preferences", file: "settings.json" },
  { id: "plugins", label: "Plugins", file: "settings.json → enabledPlugins" },
  { id: "marketplaces", label: "Marketplaces", file: "settings.json → extraKnownMarketplaces" },
  {
    id: "claudemd",
    label: "MD Files",
    file: "CLAUDE.md / CLAUDE.local.md across all projects",
  },
  { id: "memory", label: "Memory", file: "projects/*/memory/ (read-only viewer)" },
  { id: "skills", label: "Skills", file: "skills/*/SKILL.md (read-only viewer)" },
  { id: "statusline", label: "Statusline", file: "settings.json → statusLine (read-only viewer)" },
  {
    id: "mcp",
    label: "MCP",
    file: "global MCP servers via the `claude mcp` CLI (not version-controlled)",
  },
] as const;

// The History page: reachable by the strip's right-edge button, never a tab.
const HISTORY = {
  id: "history",
  label: "History",
  file: "uncommitted drift, profiles (git branches) and the commit log over your Claude config",
} as const;

const PAGES = [...TABS, HISTORY];

type SectionId = (typeof PAGES)[number]["id"];

const SECTION_PARAM = "cctab";

// What the nav used to say in its tagline. It is one sentence of standing
// context, not a per-page fact, so it rides the caption row rather than
// claiming a column of its own.
const TAGLINE = "Edits write to your Claude config and commit to git. Applies on the next session.";

function isSectionId(v: string | null): v is SectionId {
  return PAGES.some((s) => s.id === v);
}

export default function ClaudeConfig() {
  // Two change signals, because they have two different audiences:
  //
  //   badgeEpoch   — any section wrote to the config, so the History button's
  //                  dirty dot is stale. The section that wrote already knows
  //                  what changed and reloads itself; remounting it from here
  //                  would just double the fetch.
  //   sectionEpoch — something committed, folding in drift the ACTIVE section
  //                  can't have accounted for (History gains a commit, Memory
  //                  loses its "uncommitted" markers). Only this remounts the
  //                  section.
  const [badgeEpoch, setBadgeEpoch] = useState(0);
  // The MD Files section's split preview pane — a second column beside the
  // content column, so its path lives here rather than in the section.
  const [preview, setPreview] = useState<string | null>(null);
  const [sectionEpoch, setSectionEpoch] = useState(0);
  const onChanged = useCallback(() => setBadgeEpoch((n) => n + 1), []);
  const onCommitted = useCallback(() => {
    setBadgeEpoch((n) => n + 1);
    setSectionEpoch((n) => n + 1);
  }, []);
  // One status read per epoch, for the dot alone — the History page fetches its
  // own drift when you get there.
  const { status } = useGitStatus(badgeEpoch);

  const raw = new URLSearchParams(location.search).get(SECTION_PARAM);
  // `?cctab=profiles` was a tab of its own until Profiles became a block of the
  // History page. An old bookmark should land where its content went, not on
  // the default tab.
  const active: SectionId = raw === "profiles"
    ? HISTORY.id
    : isSectionId(raw)
      ? raw
      : "preferences";
  const meta = PAGES.find((s) => s.id === active) ?? PAGES[0];

  const setActive = (next: SectionId) => {
    const params = new URLSearchParams(location.search);
    // The default section is the clean URL, matching how the outer tab strip
    // drops `?tab=render`.
    if (next === "preferences") params.delete(SECTION_PARAM);
    else params.set(SECTION_PARAM, next);
    const search = params.toString();
    navigateUrl(location.pathname + (search ? "?" + search : ""));
  };

  const body = () => {
    switch (active) {
      case "preferences":
        return <PreferencesSection onChanged={onChanged} />;
      case "plugins":
        return <PluginsSection onChanged={onChanged} />;
      case "marketplaces":
        return <MarketplacesSection onChanged={onChanged} />;
      case "claudemd":
        return (
          <ClaudeMdSection onChanged={onChanged} preview={preview} onPreview={setPreview} />
        );
      case "memory":
        return <MemorySection onChanged={onChanged} />;
      case "skills":
        return <SkillsSection />;
      case "statusline":
        return <StatuslineSection />;
      case "mcp":
        return <McpSection />;
      case "history":
        return <HistorySection onChanged={onChanged} onCommitted={onCommitted} />;
    }
  };

  return (
    <div className="cc-root">
      <div className="cc-tabbar" role="tablist" aria-label="Claude config sections">
        {TABS.map((s) => (
          <button
            key={s.id}
            type="button"
            role="tab"
            aria-selected={s.id === active}
            className={"cc-tab" + (s.id === active ? " active" : "")}
            onClick={() => setActive(s.id)}
          >
            {s.label}
          </button>
        ))}
        <button
          type="button"
          role="tab"
          aria-selected={active === HISTORY.id}
          className={"cc-tab cc-tab-history" + (active === HISTORY.id ? " active" : "")}
          title={
            status?.dirty
              ? `${status.files.length} uncommitted change(s) — review and commit them here`
              : "Commits, profiles and uncommitted changes"
          }
          onClick={() => setActive(HISTORY.id)}
        >
          <Icon name="clock" />
          {HISTORY.label}
          {/* The badge that used to sit at the nav's bottom edge, reduced to its
              signal: on any tab you still learn the repo has drifted, and the
              page that does something about it is one click away. */}
          {status?.dirty && <span className="cc-tab-dot" aria-label="uncommitted changes" />}
        </button>
      </div>
      <div className="cc-body">
        <main className="cc-main">
          {/* title= on the ROW, not on the note: the note itself is hidden on a
              narrow window, and the sentence should still be reachable there. */}
          <div className="cc-caption-row" title={TAGLINE}>
            <div className="cc-caption cc-mono">{meta.file}</div>
            <div className="cc-caption cc-caption-note">
              Edits commit to git · applies next session
            </div>
          </div>
          {/* Keyed on the commit epoch: a commit rewrites state the active
              section can't have predicted, so it remounts and refetches. */}
          <div key={`${active}:${sectionEpoch}`} className="cc-section">
            {body()}
          </div>
        </main>
        {active === "claudemd" && preview && (
          <PreviewPane path={preview} onClose={() => setPreview(null)} />
        )}
      </div>
    </div>
  );
}
