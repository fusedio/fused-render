// Claude Config page — `/claude-config?tab=<tab>`: Claude Code's own configuration
// under ~/.claude, framed like Preferences: title and filter across the top, a
// nav of the four tabs (Skills, the default, Memory, Plugins, MCP servers)
// on the left, the tab's heading and cards on the right.
// Each tab draws its own heading, so the description can carry a live count;
// Skills also swaps between a list and a skill's editor.
import { useEffect, useState } from "react";
import { Blocks, Brain, Search, Server, Sparkles, X, type LucideIcon } from "lucide-react";
import { replaceSearch } from "@platform/lib/router";
import { cn } from "@platform/lib/utils";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import {
  ClaudeMcpTab,
  ClaudeMemoryTab,
  ClaudePluginsTab,
  ClaudeSkillsTab,
} from "./claude_config/tabs";

const TABS = [
  { id: "skills", label: "Skills", icon: Sparkles, filter: "Filter skills", blurb: "" },
  {
    id: "memory",
    label: "Memory",
    icon: Brain,
    filter: "Filter memory",
    blurb: "The memory files Claude Code keeps for each project.",
  },
  {
    id: "plugins",
    label: "Plugins",
    icon: Blocks,
    filter: "Filter plugins",
    blurb: "Installed plugins and the marketplaces to find more in.",
  },
  {
    id: "mcp",
    label: "MCP servers",
    icon: Server,
    filter: "Filter MCP servers",
    blurb: "Servers Claude Code connects to for extra tools.",
  },
] as const satisfies readonly { id: string; label: string; icon: LucideIcon; filter: string; blurb: string }[];
type TabId = (typeof TABS)[number]["id"];

const DEFAULT_TAB: TabId = "skills";

function readTab(): TabId {
  const want = new URLSearchParams(location.search).get("tab");
  return TABS.find((t) => t.id === want)?.id ?? DEFAULT_TAB;
}

// Picking a tab is an in-place sync of ?tab=, held in state and written with
// replaceSearch: a navigation would bump the nav epoch and remount the page.
function writeTab(tab: TabId) {
  const p = new URLSearchParams(location.search);
  if (p.get("tab") !== tab) {
    p.delete("skill");
    p.delete("view");
  }
  p.set("tab", tab);
  replaceSearch(`${location.pathname}?${p.toString()}`);
}

export default function ClaudeConfig() {
  const [tab, setTab] = useState<TabId>(readTab);
  const [filter, setFilter] = useState("");
  // Tabs already opened stay mounted, hidden, so going back to one shows its
  // loaded rows rather than a skeleton and a second read. A tab nobody opened
  // reads nothing.
  const [seen, setSeen] = useState<readonly TabId[]>(() => [tab]);
  // Bumped to remount Plugins and Skills, which read `?view=` and `?skill=`
  // once at mount.
  const [pluginsRun, setPluginsRun] = useState(0);
  const [skillsRun, setSkillsRun] = useState(0);
  const current = TABS.find((t) => t.id === tab) ?? TABS[0];

  useEffect(() => writeTab(tab), [tab]);

  const pickTab = (next: TabId) => {
    // Leaving a skill's editor drops `?skill=` (writeTab); the hidden Skills
    // tab starts over on its list rather than keep an editor the URL no longer
    // names.
    if (next !== "skills" && new URLSearchParams(location.search).has("skill")) setSkillsRun((n) => n + 1);
    setTab(next);
    setSeen((s) => (s.includes(next) ? s : [...s, next]));
    setFilter("");
  };

  // Skills' Discover points at the Plugins catalog. `?view=` carries which
  // Plugins list to open on; writeTab clears it once the tab has changed.
  const openPluginsDiscover = () => {
    const p = new URLSearchParams(location.search);
    p.set("view", "discover");
    history.replaceState(history.state, "", `${location.pathname}?${p.toString()}`);
    setPluginsRun((n) => n + 1);
    pickTab("plugins");
  };

  return (
      <div className="settings-page cc-page">
        <div className="settings-header">
          <h1 className="settings-title">Claude Config</h1>
          <div className="settings-search" role="search">
            <Search aria-hidden="true" className="settings-search-icon" />
            <Input
              type="search"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder={current.filter}
              aria-label={current.filter}
              className="settings-search-input"
              autoComplete="off"
              spellCheck={false}
            />
            {filter && (
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label="Clear filter"
                className="settings-search-clear"
                onClick={() => setFilter("")}
              >
                <X />
              </Button>
            )}
          </div>
        </div>
        <div className="settings-body">
          <nav className="settings-nav" aria-label="Claude Config">
            <div className="settings-nav-section" role="group" aria-label="Claude Config">
              {TABS.map((t) => {
                const Icon = t.icon;
                return (
                  <Button
                    key={t.id}
                    variant="ghost"
                    aria-current={t.id === tab ? "page" : undefined}
                    className={cn("settings-nav-item", t.id === tab && "is-active")}
                    onClick={() => pickTab(t.id)}
                  >
                    <Icon data-icon="inline-start" aria-hidden="true" />
                    <span className="settings-nav-label">{t.label}</span>
                  </Button>
                );
              })}
            </div>
          </nav>
          <main className="settings-main">
            {TABS.filter((t) => seen.includes(t.id)).map((t) => (
              <div key={t.id === "plugins" ? `plugins-${pluginsRun}` : t.id === "skills" ? `skills-${skillsRun}` : t.id} style={{ display: t.id === tab ? "contents" : "none" }}>
                {t.id === "skills" && <ClaudeSkillsTab filter={filter} onOpenPluginsDiscover={openPluginsDiscover} />}
                {t.id === "memory" && <ClaudeMemoryTab filter={filter} title={t.label} blurb={t.blurb} />}
                {t.id === "plugins" && <ClaudePluginsTab filter={filter} title={t.label} blurb={t.blurb} />}
                {t.id === "mcp" && <ClaudeMcpTab filter={filter} title={t.label} blurb={t.blurb} />}
              </div>
            ))}
          </main>
        </div>
      </div>
  );
}
