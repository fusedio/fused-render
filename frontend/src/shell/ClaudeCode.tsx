// Claude Code page — `/claude-code?tab=<tab>`: Claude Code's own configuration
// under ~/.claude. Tabs: Skills (the default), Memory, Plugins, MCP servers,
// Settings and History. The filter box sits above the tab's list and narrows
// that tab's rows.
import { useEffect, useState } from "react";
import { Search, X } from "lucide-react";
import { useClaudeConfigStatus } from "@apps/claude_config/available";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { Tabs, TabsList, TabsTrigger } from "@platform/shadcn/ui/tabs";
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
  { id: "skills", label: "Skills" },
  { id: "memory", label: "Memory" },
  { id: "plugins", label: "Plugins" },
  { id: "mcp", label: "MCP servers" },
  { id: "settings", label: "Settings" },
  { id: "history", label: "History" },
] as const;
type TabId = (typeof TABS)[number]["id"];

const DEFAULT_TAB: TabId = "skills";

function readTab(): TabId {
  const want = new URLSearchParams(location.search).get("tab");
  return TABS.find((t) => t.id === want)?.id ?? DEFAULT_TAB;
}

function writeTab(tab: TabId) {
  const p = new URLSearchParams(location.search);
  p.set("tab", tab);
  history.replaceState(history.state, "", `${location.pathname}?${p.toString()}`);
}

export default function ClaudeCode() {
  const claudeConfig = useClaudeConfigStatus();
  const [tab, setTab] = useState<TabId>(readTab);
  const [filter, setFilter] = useState("");

  useEffect(() => writeTab(tab), [tab]);

  const pickTab = (next: TabId) => {
    setTab(next);
    setFilter("");
  };

  return (
    <ClaudeConfigProvider available={claudeConfig === true}>
      <div className="settings-page">
        <div className="settings-header">
          <div className="cc-page-intro">
            <h1 className="settings-title">Claude Code</h1>
            <p className="cc-page-blurb">
              Skills, memory, plugins, MCP servers and settings.json for Claude Code on this machine.
            </p>
          </div>
        </div>
        <main className="settings-main">
          <div className="settings-head">
            <div className="settings-head-row">
              <Tabs value={tab} onValueChange={(v) => pickTab(String(v) as TabId)}>
                <TabsList>
                  {TABS.map((t) => (
                    <TabsTrigger key={t.id} value={t.id}>
                      {t.label}
                    </TabsTrigger>
                  ))}
                </TabsList>
              </Tabs>
              {tab === "history" && <ClaudeGitChip onOpen={() => pickTab("history")} />}
            </div>
          </div>
          {tab !== "history" && (
            <div className="settings-search cc-page-search" role="search">
              <Search aria-hidden="true" className="settings-search-icon" />
              <Input
                type="search"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder={`Filter ${TABS.find((t) => t.id === tab)?.label.toLowerCase()}`}
                aria-label="Filter this tab"
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
          {tab === "skills" && <ClaudeSkillsTab filter={filter} />}
          {tab === "memory" && <ClaudeMemoryTab filter={filter} />}
          {tab === "plugins" && <ClaudePluginsTab filter={filter} />}
          {tab === "mcp" && <ClaudeMcpTab filter={filter} />}
          {tab === "settings" && <ClaudeSettingsTab filter={filter} />}
          {tab === "history" && <ClaudeHistoryTab />}
        </main>
      </div>
    </ClaudeConfigProvider>
  );
}
