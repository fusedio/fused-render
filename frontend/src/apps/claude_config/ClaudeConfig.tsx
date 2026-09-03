// The Claude config panel: a native React port of the bundled html+py app that
// used to sit here in an iframe (D125), built out of the shell's own primitives
// so it follows the Light/Dark setting and shares one toast/dialog surface with
// the rest of the app instead of shipping a second, Claude-branded one.
//
// The layout is NOT the original app's any more. That app owned the whole
// window and could afford a 230px section nav down the left; inside this shell
// that nav landed directly beside the global sidebar and the page read as two
// sidebars glued together. So the sections are a horizontal TAB STRIP across
// the top, and the horizontal budget the nav was eating goes to the content:
//
//   ┌ PageHeader ──────────────────────────────────────────────────┐
//   │ Claude config                          [● Clean / N uncommitted]│
//   ├ Tabs ────────────────────────────────────────────────────────┤
//   │ Plugins  Memory  Skills  Statusline  MCP  Preferences         │
//   ├ body ────────────────────────────────────────────────────────┤
//   │ caption (the file this tab edits)                            │
//   │ section content                                               │
//   └───────────────────────────────────────────────────────────────┘
//
// Two pieces of state, each in the place that suits it:
//
//   * the active SECTION lives in the URL (`?cctab=plugins`) — bookmarkable, and
//     the same navigateUrl pattern the shell's own tab strips use;
//   * the git epoch lives here, because every section can dirty the repo and
//     they all report back through one `onChanged`.
//
// History is deliberately NOT one of the tabs. It is the git chip in the
// header band, because it is the only page whose state matters while you are
// looking at some other one: the chip carries the dirty state that tells you
// the config has uncommitted drift, and it is where you commit that drift.
// Profiles lives on it too — a profile is a git branch over the same repo, so
// it belongs with the history rather than beside Preferences.
//
// A note on remounting: the shell renders this page keyed on the nav epoch, so
// any navigation — including this panel's own `?cctab=` writes — remounts it.
// That is why nothing here tries to cache across a section change: a section
// switch IS a fresh mount, and each section refetches exactly as the original
// app re-rendered.
import { useCallback, useState } from "react";
import { Clock } from "lucide-react";
import { navigateUrl } from "@platform/lib/router";
import { Button } from "@platform/shadcn/ui/button";
import { Tabs, TabsList, TabsTrigger } from "@platform/shadcn/ui/tabs";
import { StatusDot } from "@platform/ui/flow/StatusIcon";
import { Identifier, PageHeader, Tiny } from "@platform/ui/flow/Typography";
import { Pill, useGitStatus } from "./bits";
import HistorySection from "./sections/HistorySection";
import McpSection from "./sections/McpSection";
import MemorySection from "./sections/MemorySection";
import PluginsSection from "./sections/PluginsSection";
import PreferencesSection from "./sections/PreferencesSection";
import SkillsSection from "./sections/SkillsSection";
import StatuslineSection from "./sections/StatuslineSection";

// The tab strip, in order. `file` is the caption under the strip — it names the
// file (or the git object) the section actually edits, which is the one thing a
// settings UI over someone's dotfiles owes them.
// Preferences sits LAST in the strip, not first: it's the tab people touch
// once and leave, while Plugins/MCP are the ones worth landing on first. It
// stays the routing default below regardless of its position here — the
// strip's order is presentation, the default is a bookmark contract.
const TABS = [
  {
    id: "plugins",
    label: "Plugins",
    file: "settings.json → enabledPlugins + extraKnownMarketplaces",
  },
  // `readOnly` puts one pill in the caption row — the same fact, said the same
  // way as the read-only marketplaces and plugin-provided MCP servers say it.
  { id: "memory", label: "Memory", file: "projects/*/memory/", readOnly: true },
  { id: "skills", label: "Skills", file: "skills/*/SKILL.md", readOnly: true },
  { id: "statusline", label: "Statusline", file: "settings.json → statusLine", readOnly: true },
  {
    id: "mcp",
    label: "MCP",
    file: "global MCP servers via the `claude mcp` CLI (not version-controlled)",
  },
  { id: "preferences", label: "Preferences", file: "settings.json" },
] as const;

// The History page: reachable by the header's git chip, never a tab.
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
  //   badgeEpoch   — any section wrote to the config, so the git chip's dirty
  //                  dot is stale. The section that wrote already knows what
  //                  changed and reloads itself; remounting it from here would
  //                  just double the fetch.
  //   sectionEpoch — something committed, folding in drift the ACTIVE section
  //                  can't have accounted for (History gains a commit, Memory
  //                  loses its "uncommitted" markers). Only this remounts the
  //                  section.
  const [badgeEpoch, setBadgeEpoch] = useState(0);
  const [sectionEpoch, setSectionEpoch] = useState(0);
  const onChanged = useCallback(() => setBadgeEpoch((n) => n + 1), []);
  const onCommitted = useCallback(() => {
    setBadgeEpoch((n) => n + 1);
    setSectionEpoch((n) => n + 1);
  }, []);
  // One status read per epoch, for the dot alone — the History page fetches its
  // own drift when you get there.
  const { status, failed } = useGitStatus(badgeEpoch);

  const raw = new URLSearchParams(location.search).get(SECTION_PARAM);
  // `?cctab=profiles` was a tab of its own until Profiles became a block of the
  // History page, `?cctab=marketplaces` until Marketplaces folded into the
  // Plugins rail, and `?cctab=claudemd` until the MD Files tab was deleted
  // outright. Every old bookmark should land where its content went (or, for
  // claudemd, on the default tab — there is no replacement page for it).
  const active: SectionId = raw === "profiles"
    ? HISTORY.id
    : raw === "marketplaces"
      ? "plugins"
      : isSectionId(raw)
        ? raw
        : "preferences";
  // `active` is always a valid SectionId, so this find always hits — the `??`
  // is unreachable, not a real fallback.
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

  // Three chip states, not two: the chip states drift POSITIVELY ("Clean"), so
  // it must never say that from a null status — during the first fetch, or
  // forever if cc.gitOps.status() keeps failing.
  const chipBucket = failed || !status ? "neutral" : status.dirty ? "orange" : "green";
  const chipLabel = failed
    ? "Status unknown"
    : !status
      ? "Checking…"
      : status.dirty
        ? `${status.files.length} uncommitted`
        : "Clean";
  const chipTitle = failed
    ? "Git status unavailable — could not be reached"
    : !status
      ? "Checking git status…"
      : status.dirty
        ? `${status.files.length} uncommitted change(s) — review and commit them in History`
        : "Commits, profiles and uncommitted changes";

  return (
    // The whole page scrolls as one, header and tab strip included (owner,
    // 2026-08-26). Bounded by the shell's #content flex chain above.
    <div className="flex flex-col flex-1 min-h-0 overflow-y-auto overflow-x-hidden scrollbar-auto-hide bg-background text-foreground">
      <PageHeader
        title="Claude config"
        actions={
          <Button
            variant={active === HISTORY.id ? "secondary" : "outline"}
            size="sm"
            aria-current={active === HISTORY.id ? "page" : undefined}
            title={chipTitle}
            onClick={() => setActive(HISTORY.id)}
          >
            <StatusDot bucket={chipBucket} pulse={!status && !failed} />
            {chipLabel}
            <Clock />
          </Button>
        }
      />
      {/* The tablist holds the TABS and nothing else — History is not one of
          them, so it sits in the header instead. A tablist whose children
          aren't all tabs mis-announces the set's size and position ("tab 9 of
          9" for a thing that isn't a tab). */}
      <Tabs
        value={active === HISTORY.id ? null : active}
        onValueChange={(v) => v && setActive(v as SectionId)}
        className="px-6 border-b border-border shrink-0"
      >
        <TabsList variant="line" aria-label="Claude config sections" className="h-10 overflow-x-auto max-w-full">
          {TABS.map((s) => (
            <TabsTrigger key={s.id} value={s.id} className="px-3">
              {s.label}
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>
      <main className="flex-1 min-h-0 px-6 py-4 space-y-5">
        {/* title= on the ROW, not on the note: the note itself is hidden on a
            narrow window, and the sentence should still be reachable there. */}
        <div className="flex items-center justify-between gap-4 min-w-0" title={TAGLINE}>
          <Identifier className="flex items-center gap-2 min-w-0 truncate">
            <span className="truncate">{meta.file}</span>
            {"readOnly" in meta && meta.readOnly && <Pill tone="ro">read-only</Pill>}
          </Identifier>
          <Tiny className="hidden md:inline shrink-0">Edits commit to git · applies next session</Tiny>
        </div>
        {/* Keyed on the commit epoch: a commit rewrites state the active
            section can't have predicted, so it remounts and refetches. */}
        <div key={`${active}:${sectionEpoch}`} className="space-y-5">
          {body()}
        </div>
      </main>
    </div>
  );
}
