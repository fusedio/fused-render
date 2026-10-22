// Claude Code page — `/claude-code?tab=<tab>`: Claude Code's own configuration
// under ~/.claude, framed like Preferences: title and filter across the top, a
// nav of the six tabs (Skills, the default, Memory, Plugins, MCP servers,
// Settings, History) on the left, the tab's heading and cards on the right.
// Each tab draws its own heading, so the description can carry a live count;
// Skills also swaps between a list and a skill's editor.
import { useEffect, useState } from "react";
import { Blocks, Brain, History, Search, Server, SlidersHorizontal, Sparkles, X, type LucideIcon } from "lucide-react";
import { useClaudeConfigStatus } from "@apps/claude_config/available";
import { Head } from "@apps/claude_config/bits";
import { cn } from "@platform/lib/utils";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import {
  ClaudeConfigProvider,
  ClaudeGitChip,
  ClaudeHistoryTab,
  ClaudeMcpTab,
  ClaudeMemoryTab,
  ClaudePluginsTab,
  ClaudeSettingsTab,
  ClaudeSkillsTab,
} from "./claude_code/tabs";

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
  {
    id: "settings",
    label: "Settings",
    icon: SlidersHorizontal,
    filter: "Search settings",
    blurb: "The keys of settings.json, with Claude's defaults.",
  },
  {
    id: "history",
    label: "History",
    icon: History,
    filter: "",
    blurb: "Every change to this configuration is committed; undo one here.",
  },
] as const satisfies readonly { id: string; label: string; icon: LucideIcon; filter: string; blurb: string }[];
type TabId = (typeof TABS)[number]["id"];

const DEFAULT_TAB: TabId = "skills";

function readTab(): TabId {
  const want = new URLSearchParams(location.search).get("tab");
  return TABS.find((t) => t.id === want)?.id ?? DEFAULT_TAB;
}

function writeTab(tab: TabId) {
  const p = new URLSearchParams(location.search);
  if (p.get("tab") !== tab) p.delete("skill");
  p.set("tab", tab);
  history.replaceState(history.state, "", `${location.pathname}?${p.toString()}`);
}

export default function ClaudeCode() {
  const claudeConfig = useClaudeConfigStatus();
  const [tab, setTab] = useState<TabId>(readTab);
  const [filter, setFilter] = useState("");
  const current = TABS.find((t) => t.id === tab) ?? TABS[0];
  const head = { title: current.label, blurb: current.blurb };

  useEffect(() => writeTab(tab), [tab]);

  const pickTab = (next: TabId) => {
    setTab(next);
    setFilter("");
  };

  return (
    <ClaudeConfigProvider available={claudeConfig === true}>
      <div className="settings-page">
        <div className="settings-header">
          <h1 className="settings-title">Claude Code</h1>
          {tab !== "history" && (
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
          )}
        </div>
        <div className="settings-body">
          <nav className="settings-nav" aria-label="Claude Code">
            <div className="settings-nav-section" role="group" aria-label="Claude Code">
              <div className="settings-nav-heading" aria-hidden="true">
                Claude Code
              </div>
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
            {tab === "skills" && <ClaudeSkillsTab filter={filter} />}
            {tab === "memory" && <ClaudeMemoryTab filter={filter} {...head} />}
            {tab === "plugins" && <ClaudePluginsTab filter={filter} {...head} />}
            {tab === "mcp" && <ClaudeMcpTab filter={filter} {...head} />}
            {tab === "settings" && <ClaudeSettingsTab filter={filter} {...head} />}
            {tab === "history" && (
              <>
                <Head title={head.title} blurb={head.blurb} aside={<ClaudeGitChip onOpen={() => pickTab("history")} />} />
                <ClaudeHistoryTab />
              </>
            )}
          </main>
        </div>
      </div>
    </ClaudeConfigProvider>
  );
}
