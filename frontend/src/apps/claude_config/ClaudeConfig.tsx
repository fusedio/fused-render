// The Claude config panel: a native React port of the bundled html+py app that
// used to sit here in an iframe (D125). Same information architecture — a
// 210px section nav, one content column, a git status badge pinned to the nav's
// bottom edge — but built out of the shell's own primitives, so it follows the
// Light/Dark setting and shares one toast/modal surface with the rest of the app
// instead of shipping a second, Claude-branded one.
//
// Two pieces of state, each in the place that suits it:
//
//   * the active SECTION lives in the URL (`?cctab=plugins`) — bookmarkable, and
//     the same navigateUrl pattern the shell's own tab strips use;
//   * the git BADGE lives here, because every section can dirty the repo and
//     they all report back through one `onChanged`.
//
// The CLAUDE.md explorer used to be an eleventh section here, and the split
// preview pane it needs lived here with it. Both moved out to their own page
// (./ClaudeMdPage, the sidebar's "MD Files" entry) — it is a file browser over
// the whole machine, not a setting in ~/.claude, and it was the one section
// that made this panel a three-column layout. A stale `?cctab=claudemd`
// bookmark therefore lands on the default section (isSectionId rejects it).
//
// A note on remounting: the shell renders this page keyed on the nav epoch, so
// any navigation — including this panel's own `?cctab=` writes — remounts it.
// That is why nothing here tries to cache across a section change: a section
// switch IS a fresh mount, and each section refetches exactly as the original
// app re-rendered.
import { useCallback, useState } from "react";
import { navigateUrl } from "@platform/lib/router";
import { StatusBadge } from "./bits";
import HistorySection from "./sections/HistorySection";
import MarketplacesSection from "./sections/MarketplacesSection";
import McpSection from "./sections/McpSection";
import MemorySection from "./sections/MemorySection";
import PluginsSection from "./sections/PluginsSection";
import PreferencesSection from "./sections/PreferencesSection";
import ProfilesSection from "./sections/ProfilesSection";
import SkillsSection from "./sections/SkillsSection";
import StatuslineSection from "./sections/StatuslineSection";

// The nav, in order. `file` is the caption under each section's heading — it
// names the file (or the git object) the section actually edits, which is the
// one thing a settings UI over someone's dotfiles owes them.
const SECTIONS = [
  { id: "preferences", label: "Preferences", file: "settings.json" },
  { id: "plugins", label: "Plugins", file: "settings.json → enabledPlugins" },
  { id: "marketplaces", label: "Marketplaces", file: "settings.json → extraKnownMarketplaces" },
  { id: "memory", label: "Memory", file: "projects/*/memory/ (read-only viewer)" },
  { id: "skills", label: "Skills", file: "skills/*/SKILL.md (read-only viewer)" },
  { id: "statusline", label: "Statusline", file: "settings.json → statusLine (read-only viewer)" },
  { id: "profiles", label: "Profiles", file: "git branches over your Claude config" },
  {
    id: "mcp",
    label: "MCP",
    file: "global MCP servers via the `claude mcp` CLI (not version-controlled)",
  },
  { id: "history", label: "History", file: "git log over your Claude config" },
] as const;

type SectionId = (typeof SECTIONS)[number]["id"];

const SECTION_PARAM = "cctab";

function isSectionId(v: string | null): v is SectionId {
  return SECTIONS.some((s) => s.id === v);
}

export default function ClaudeConfig() {
  // Two change signals, because they have two different audiences:
  //
  //   badgeEpoch   — any section wrote to the config, so the git badge is stale.
  //                  The section that wrote already knows what changed and
  //                  reloads itself; remounting it from here would just double
  //                  the fetch.
  //   sectionEpoch — the badge itself committed, folding in drift the ACTIVE
  //                  section can't have accounted for (History gains a commit,
  //                  Memory loses its "uncommitted" markers). Only this remounts
  //                  the section.
  const [badgeEpoch, setBadgeEpoch] = useState(0);
  const [sectionEpoch, setSectionEpoch] = useState(0);
  const onChanged = useCallback(() => setBadgeEpoch((n) => n + 1), []);
  const onCommitted = useCallback(() => {
    setBadgeEpoch((n) => n + 1);
    setSectionEpoch((n) => n + 1);
  }, []);

  const raw = new URLSearchParams(location.search).get(SECTION_PARAM);
  const active: SectionId = isSectionId(raw) ? raw : "preferences";
  const meta = SECTIONS.find((s) => s.id === active) ?? SECTIONS[0];

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
      case "memory":
        return <MemorySection onChanged={onChanged} />;
      case "skills":
        return <SkillsSection />;
      case "statusline":
        return <StatuslineSection />;
      case "profiles":
        return <ProfilesSection onChanged={onChanged} />;
      case "mcp":
        return <McpSection />;
      case "history":
        return <HistorySection onChanged={onChanged} />;
    }
  };

  return (
    <div className="cc-root">
      <nav className="cc-nav" aria-label="Claude config sections">
        <h2 className="cc-nav-title">Claude config</h2>
        <p className="cc-nav-tagline">
          Edits write to your Claude config and commit to git. Applies on the next session.
        </p>
        {SECTIONS.map((s) => (
          <button
            key={s.id}
            type="button"
            className={"cc-nav-item" + (s.id === active ? " active" : "")}
            aria-current={s.id === active ? "page" : undefined}
            onClick={() => setActive(s.id)}
          >
            {s.label}
          </button>
        ))}
        <StatusBadge epoch={badgeEpoch} onCommitted={onCommitted} />
      </nav>
      <main className="cc-main">
        <h2 className="cc-heading">{meta.label}</h2>
        <div className="cc-caption cc-mono">{meta.file}</div>
        {/* Keyed on the commit epoch: a badge commit rewrites state the active
            section can't have predicted, so it remounts and refetches. */}
        <div key={`${active}:${sectionEpoch}`} className="cc-section">
          {body()}
        </div>
      </main>
    </div>
  );
}
