// Claude Code page — `/claude-config?tab=<tab>`: Claude Code's own configuration
// under ~/.claude, framed like Preferences: title and filter across the top, a
// nav of the three tabs (Plugins, the default, MCP servers, Memory) on the
// left, the tab's heading and cards on the right. Skills have their own page
// (`/skills`); a faint line under the nav says so.
// Each tab draws its own heading, so the description can carry a live count.
import { useEffect, useState } from "react";
import { Blocks, Brain, Search, Server, X, type LucideIcon } from "lucide-react";
import { navigateUrl, replaceSearch } from "@platform/lib/router";
import { cn } from "@platform/lib/utils";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { ClaudeMark } from "@platform/ui/ClaudeMark";
import { ClaudeMcpTab, ClaudeMemoryTab, ClaudePluginsTab } from "./claude_config/tabs";

const TABS = [
  {
    id: "plugins",
    label: "Plugins",
    icon: Blocks,
    filter: "Search plugins, installed and in your marketplaces",
    blurb: "Installed plugins and the marketplaces to find more in.",
  },
  {
    id: "mcp",
    label: "MCP servers",
    icon: Server,
    filter: "Filter MCP servers",
    blurb: "Servers Claude Code connects to for extra tools.",
  },
  {
    id: "memory",
    label: "Memory",
    icon: Brain,
    filter: "Filter memory",
    blurb: "The memory files Claude Code keeps for each project.",
  },
] as const satisfies readonly { id: string; label: string; icon: LucideIcon; filter: string; blurb: string }[];
type TabId = (typeof TABS)[number]["id"];

const DEFAULT_TAB: TabId = "plugins";

function readTab(): TabId {
  const want = new URLSearchParams(location.search).get("tab");
  return TABS.find((t) => t.id === want)?.id ?? DEFAULT_TAB;
}

// Picking a tab is an in-place sync of ?tab=, held in state and written with
// replaceSearch: a navigation would bump the nav epoch and remount the page.
function writeTab(tab: TabId) {
  const p = new URLSearchParams(location.search);
  if (p.get("tab") !== tab) p.delete("view");
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
  const current = TABS.find((t) => t.id === tab) ?? TABS[0];

  useEffect(() => writeTab(tab), [tab]);

  const pickTab = (next: TabId) => {
    setTab(next);
    setSeen((s) => (s.includes(next) ? s : [...s, next]));
    setFilter("");
  };

  return (
      <div className="settings-page cc-page">
        <div className="settings-header">
          <h1 className="settings-title m-0 inline-flex items-center gap-2">
            <ClaudeMark className="size-5" style={{ width: 20, height: 20 }} />
            Claude Code
          </h1>
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
          <nav className="settings-nav" aria-label="Claude Code">
            <div className="settings-nav-section" role="group" aria-label="Claude Code">
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
            <p className="cc-skill-note-line m-0 mt-4 px-2.5">
              Skills moved to their own page.{" "}
              <a
                href="/skills"
                className="cc-link"
                onClick={(e) => {
                  e.preventDefault();
                  navigateUrl("/skills");
                }}
              >
                Open Skills
              </a>
            </p>
          </nav>
          <main className="settings-main">
            {TABS.filter((t) => seen.includes(t.id)).map((t) => (
              <div key={t.id} style={{ display: t.id === tab ? "contents" : "none" }}>
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
