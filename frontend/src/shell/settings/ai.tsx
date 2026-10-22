// AI & agents: which coding agents may run chats and tasks, and the pointers
// to the model and engine settings that live on their own pages.
import { useContext, useSyncExternalStore } from "react";
import { ArrowUpRight } from "lucide-react";
import { putAgentEnabled, putDefaultBackend, type Prefs } from "@platform/lib/api";
import { agentLabel, publishAgentBackendsChanged, useAgentBackends } from "@platform/lib/agent-backends";
import { useAgentSetup } from "@platform/lib/claude-setup";
import { agentAccountLine, agentIssues } from "@platform/lib/claude-health";
import { navigateUrl } from "@platform/lib/router";
import { IssueRow } from "@platform/ui/ClaudeHealthStrip";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { Button } from "@platform/shadcn/ui/button";
import { signedInHint } from "../onboarding/ToolsStep";
import type { SettingId } from "./registry";
import { RowSwitch, SettingRow, SettingSelect, SettingsViewContext, usePrefWrite, type PrefControlProps } from "./SettingRow";

type AgentId = "claude" | "copilot" | "codex";

/** Every switch, in the registry's ORDER (docs/CODEX_BACKEND.md §1). */
const AGENT_IDS: readonly AgentId[] = ["claude", "copilot", "codex"];

/** The name the health line uses — the product's, where the label is the
 *  short word the pills use. */
const HEALTH_NAME: Record<AgentId, string> = { claude: "Claude Code", copilot: "Copilot", codex: "Codex" };

// ONE busy flag for every agent write on the page. The three switches and the
// default picker are separate rows, but each decides what it allows from
// prefs the others' in-flight writes have not updated yet: turning two
// agents off in quick succession would otherwise send two PUTs the server
// has to refuse one of.
let agentWrites = 0;
const agentListeners = new Set<() => void>();
const agentBusy = () => agentWrites > 0;
function subscribeAgentBusy(cb: () => void) {
  agentListeners.add(cb);
  return () => agentListeners.delete(cb);
}
function bumpAgentWrites(by: 1 | -1) {
  agentWrites += by;
  agentListeners.forEach((cb) => cb());
}

/** `usePrefWrite`, with the page-wide agent busy flag around it. */
function useAgentWrite(onChange: PrefControlProps["onChange"]) {
  const { error, run } = usePrefWrite(onChange);
  const busy = useSyncExternalStore(subscribeAgentBusy, agentBusy, agentBusy);
  const write = async (put: () => Promise<Prefs>) => {
    if (agentBusy()) return;
    bumpAgentWrites(1);
    try {
      // Published, not just saved: the Agent pill, the pickers and every
      // agent mark read the catalog, which is cached per page.
      await run(put, publishAgentBackendsChanged);
    } finally {
      bumpAgentWrites(-1);
    }
  };
  return { busy, error, write };
}

/** Absent (an older server) reads as the only answer it could give: Claude. */
function agentsOf(prefs: Prefs) {
  const a = prefs.agents;
  // A server from before Codex answers without its key: off, its default.
  return a ? { ...a, codex: a.codex ?? false } : { claude: true, copilot: false, codex: false, default: "claude" };
}

/** One switched-on agent's health: a line when all is well, the strip's own
 *  issue rows (install, sign in, doctor) when it is not. Mounted per agent, so
 *  each owns its setup machine. */
function AgentHealth({ backend }: { backend: AgentId }) {
  const {
    health, loaded, busy, install, login, doctor, actionError, acting,
    linkedNote, load, act, cancelLogin,
  } = useAgentSetup(backend, true);
  const name = HEALTH_NAME[backend];
  const again = (
    <button type="button" className="settings-link" onClick={() => load(true)} disabled={busy}>
      {busy ? "Checking…" : "Check again"}
    </button>
  );
  if (!loaded) return <span>Checking {name}…</span>;
  if (!health) return <span>Couldn't check {name}. {again}</span>;
  const issues = agentIssues(health, backend);
  // `null` is "could not ask", never "signed out".
  const account = health.signed_in === null
    ? "Couldn't confirm sign-in."
    : agentAccountLine(health, backend, signedInHint);
  return (
    <>
      <span className={issues.length ? "settings-dot settings-dot-warn" : "settings-dot settings-dot-ok"}>
        {issues.length ? `${name} needs attention.` : `${name}${health.version ? ` ${health.version}` : ""} is ready.`}
        {account ? ` ${account.endsWith(".") ? account : account + "."}` : ""}
      </span>{" "}
      {again}
      {issues.length > 0 && (
        <ul className="claude-health-issues">
          {issues.map((issue) => (
            <IssueRow
              key={issue.id}
              issue={issue}
              install={install}
              login={login}
              doctor={issue.id === "broken" ? doctor : null}
              onAct={act}
              onCancelLogin={cancelLogin}
              busy={acting}
              actionError={issue.action ? actionError : null}
              doneNote={issue.id === "not-on-path" ? linkedNote : null}
            />
          ))}
        </ul>
      )}
    </>
  );
}

// The server refuses a write that would leave no agent on (400 "turn on
// another agent first"), so the last switch on is drawn disabled saying so.
function AgentSwitch({ id, backend, prefs, onChange }: PrefControlProps & { id: SettingId; backend: AgentId }) {
  useAgentBackends();
  const { busy, error, write } = useAgentWrite(onChange);
  const agents = agentsOf(prefs);
  const on = agents[backend];
  const last = on && AGENT_IDS.filter((a) => agents[a]).length === 1;
  return (
    <SettingRow
      id={id}
      status={on ? <AgentHealth backend={backend} /> : undefined}
      control={
        <RowSwitch
          id={id}
          checked={on}
          disabled={busy || last}
          title={last ? "Turn on another agent first" : undefined}
          onChange={(v) => void write(() => putAgentEnabled(backend, v))}
        />
      }
    >
      {last && <p className="settings-row-desc">Turn on another agent before turning this one off.</p>}
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </SettingRow>
  );
}

export const ClaudeAgentControl = (p: PrefControlProps) => <AgentSwitch {...p} id="agent-claude" backend="claude" />;
export const CopilotAgentControl = (p: PrefControlProps) => <AgentSwitch {...p} id="agent-copilot" backend="copilot" />;
export const CodexAgentControl = (p: PrefControlProps) => <AgentSwitch {...p} id="agent-codex" backend="codex" />;

export function DefaultAgentControl({ prefs, onChange }: PrefControlProps) {
  useAgentBackends();
  const { busy, error, write } = useAgentWrite(onChange);
  const agents = agentsOf(prefs);
  const ids = AGENT_IDS.filter((a) => agents[a]);
  return (
    <SettingRow
      id="agent-default"
      control={
        <SettingSelect
          id="agent-default"
          value={ids.includes(agents.default as AgentId) ? agents.default : ids[0]}
          options={ids.map((id) => ({ key: id, label: agentLabel(id) }))}
          disabled={busy}
          onChange={(v) => void write(() => putDefaultBackend(v))}
        />
      }
    >
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </SettingRow>
  );
}

/** A "Set elsewhere" row: the setting's name and a door to the page that owns it. */
export function PointerRow({ id, to, label }: { id: SettingId; to: string | (() => void); label: string }) {
  return (
    <SettingRow
      id={id}
      control={
        <Button
          variant="outline"
          size="sm"
          onClick={() => (typeof to === "string" ? navigateUrl(to) : to())}
        >
          {label}
          <ArrowUpRight data-icon="inline-end" />
        </Button>
      }
    />
  );
}

export const ClaudeModelPointer = () => (
  <PointerRow id="claude-model" to="/preferences?tab=claude-cli&s=claude-pref:model" label="Claude Code" />
);
export const EnginesPointer = () => <PointerRow id="engines" to="/ai-models/engines" label="AI Models › Engines" />;

export function AgentSigninPointer() {
  const view = useContext(SettingsViewContext);
  return <PointerRow id="agent-signin" to={() => view.jump("agent-claude")} label="AI & agents" />;
}
