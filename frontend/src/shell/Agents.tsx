// Agents page — `/agents?h=<harness>&tab=<tab>`: the configuration of each
// coding agent fused-render drives. A harness switcher on top, then the tabs
// that harness's capabilities offer (GET /api/agents/harnesses): Claude has
// Settings, Skills, Memory, Plugins, MCP servers and History under ~/.claude;
// Codex and Copilot have Instructions, MCP and Settings files in their own
// homes. The name filter narrows the rows of the current tab.
import { useEffect, useMemo, useState } from "react";
import { Search, X } from "lucide-react";
import * as cc from "@apps/claude_config/api";
import type { HarnessInfo } from "@apps/claude_config/api";
import { useClaudeConfigStatus } from "@apps/claude_config/available";
import { useAgentBackends } from "@platform/lib/agent-backends";
import { navigateUrl } from "@platform/lib/router";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { Tabs, TabsList, TabsTrigger } from "@platform/shadcn/ui/tabs";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import {
  ClaudeConfigProvider,
  ClaudeGitChip,
  ClaudeHistoryTab,
  ClaudeMcpTab,
  ClaudeMemoryTab,
  ClaudePluginsTab,
  ClaudeSettingsTab,
  ClaudeSkillsTab,
} from "./agents/claude";
import { HarnessFileTab, HarnessMcpTab } from "./agents/OtherHarness";

const TAB_LABELS: Record<string, string> = {
  settings: "Settings",
  skills: "Skills",
  memory: "Memory",
  plugins: "Plugins",
  mcp: "MCP servers",
  history: "History",
  instructions: "Instructions",
};
const TAB_ORDER = ["settings", "skills", "memory", "plugins", "mcp", "history", "instructions"];
const HARNESS_ORDER = ["claude", "copilot", "codex"];

function readUrl(): { h: string; tab: string } {
  const p = new URLSearchParams(location.search);
  return { h: p.get("h") || "claude", tab: p.get("tab") || "" };
}

function writeUrl(h: string, tab: string) {
  const p = new URLSearchParams(location.search);
  p.set("h", h);
  p.set("tab", tab);
  history.replaceState(history.state, "", `${location.pathname}?${p.toString()}`);
}

export default function Agents() {
  const claudeConfig = useClaudeConfigStatus();
  const backends = useAgentBackends();
  const [{ h, tab: wantTab }, setSel] = useState(readUrl);
  const [infos, setInfos] = useState<HarnessInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");

  useEffect(() => {
    cc.getHarnesses().then(
      (r) => setInfos(r.harnesses),
      (e) => setError((e as Error).message),
    );
  }, []);

  const caps = infos?.find((i) => i.id === h)?.capabilities ?? [];
  const tabs = TAB_ORDER.filter((t) => caps.includes(t));
  const tab = tabs.includes(wantTab) ? wantTab : (tabs[0] ?? "");
  useEffect(() => {
    if (tab) writeUrl(h, tab);
  }, [h, tab]);

  const rows = useMemo(
    () =>
      HARNESS_ORDER.map((id) => backends.all.find((b) => b.id === id)).filter(
        (b): b is NonNullable<typeof b> => !!b,
      ),
    [backends.all],
  );
  const row = rows.find((b) => b.id === h);
  const info = infos?.find((i) => i.id === h);

  const pickHarness = (next: string) => {
    setSel({ h: next, tab: "" });
    setFilter("");
  };
  const pickTab = (next: string) => {
    setSel({ h, tab: next });
    setFilter("");
  };

  return (
    <ClaudeConfigProvider available={claudeConfig === true}>
      <div className="settings-page">
        <div className="settings-header">
          <h1 className="settings-title">Agents</h1>
          <Tabs value={h} onValueChange={(v) => pickHarness(String(v))}>
            <TabsList>
              {rows.map((b) => (
                <TabsTrigger key={b.id} value={b.id}>
                  {b.label}
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
          <div className="settings-search" role="search">
            <Search aria-hidden="true" className="settings-search-icon" />
            <Input
              type="search"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Filter this tab"
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
        </div>
        <main className="settings-main">
          {error && <ErrorBanner>{error}</ErrorBanner>}
          {row && !row.enabled && (
            <p className="cc-lrow-sub">
              {row.label} is turned off.{" "}
              <a
                href="/preferences?tab=ai"
                onClick={(e) => {
                  e.preventDefault();
                  navigateUrl("/preferences?tab=ai");
                }}
              >
                Enable it in Preferences › AI &amp; agents
              </a>
              .
            </p>
          )}
          {info && !info.exists && (
            <p className="cc-lrow-sub">
              {info.home} does not exist yet; saving a file creates it.
            </p>
          )}
          <div className="settings-head">
            <div className="settings-head-row">
              <Tabs value={tab} onValueChange={(v) => pickTab(String(v))}>
                <TabsList>
                  {tabs.map((t) => (
                    <TabsTrigger key={t} value={t}>
                      {TAB_LABELS[t]}
                    </TabsTrigger>
                  ))}
                </TabsList>
              </Tabs>
              {h === "claude" && tab === "history" && <ClaudeGitChip onOpen={() => pickTab("history")} />}
            </div>
          </div>
          {h === "claude" && tab === "settings" && <ClaudeSettingsTab filter={filter} />}
          {h === "claude" && tab === "skills" && <ClaudeSkillsTab filter={filter} />}
          {h === "claude" && tab === "memory" && <ClaudeMemoryTab filter={filter} />}
          {h === "claude" && tab === "plugins" && <ClaudePluginsTab filter={filter} />}
          {h === "claude" && tab === "mcp" && <ClaudeMcpTab filter={filter} />}
          {h === "claude" && tab === "history" && <ClaudeHistoryTab />}
          {h !== "claude" && tab === "instructions" && <HarnessFileTab harness={h} kind="instructions" />}
          {h !== "claude" && tab === "settings" && <HarnessFileTab harness={h} kind="settings" />}
          {h !== "claude" && tab === "mcp" && <HarnessMcpTab harness={h} filter={filter} />}
        </main>
      </div>
    </ClaudeConfigProvider>
  );
}
