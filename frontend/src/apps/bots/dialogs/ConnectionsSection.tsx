// Settings › Connections: one row per connected service the server knows (Notion, GitHub, Slack…), with the GLOBAL
// sign-in (one token per service on this machine, the same state Preferences › Connections shows; the client and
// its polling are platform/lib/connect.ts) and the PER-BOT switch: bot.json `connections`, the services this bot may
// use. Off by default: a call into a service that is off raises a card every time, even under "Never ask", and
// "Always approve" on that card flips the switch here (the dialog folds that tick in while it is open, BotSettings).
// The switch is the dialog's: it goes out on Save as `connections_add` / `connections_remove`. The sign-in buttons
// post at once, like the Skills list. Paste-a-key and bring-your-own-app sign-ins stay on the Preferences page: the
// row sends you there rather than carrying a second copy of those forms.
import { useState, type ReactNode } from "react";
import { Copy, ExternalLinkIcon } from "lucide-react";
import {
  againBody,
  cancelConnection,
  defaultAuth,
  logoutConnection,
  toolCount,
  useConnections,
  type ConnectProvider,
  type ConnectStatus,
  type LoginBody,
} from "@platform/lib/connect";
import { copyToClipboard } from "@platform/lib/clipboard";
import { navigateUrl } from "@platform/lib/router";
import { ConnectIcon } from "@platform/ui/ConnectIcon";
import { Button } from "@platform/shadcn/ui/button";
import { Switch } from "@platform/shadcn/ui/switch";
import { normApp } from "../lib/botform";
import { Row, Rows } from "./SettingsRow";

export const PREFERENCES_CONNECTIONS_URL = "/preferences?tab=connections";

export function ConnectionsSection({ botName, enabled, setEnabled }: {
  botName: string;
  /** Provider keys (normalised) this bot may use, as the dialog holds them. */
  enabled: string[];
  setEnabled: (f: (cur: string[]) => string[]) => void;
}) {
  const { rows, error, stateOf, apply, login } = useConnections();
  const toggle = (key: string, on: boolean) => {
    const k = normApp(key);
    setEnabled((cur) => on ? (cur.includes(k) ? cur : [...cur, k]) : cur.filter((x) => x !== k));
  };
  return (
    <Rows>
      <Row title="Services this bot may use" stack
        text={`Sign in once and the service's tools reach every bot, app and task on this Mac; which bots may use it is up to you. ${botName} asks on every call to a service that is off here, even when it never asks otherwise, and “Always approve” on that card turns the service on for it.`}>
        {error ? <p className="m-0 text-[13px] text-destructive">{error}</p> : null}
        {rows && rows.length === 0 ? <p className="m-0 text-[13px] text-muted-foreground">No services to connect on this build.</p> : null}
        {rows && rows.length > 0 ? (
          <div className="divide-y divide-foreground/5 rounded-lg border border-input" id="bmconnections">
            {rows.map((p) => (
              <ProviderRow key={p.key} provider={p} status={stateOf(p)} on={enabled.includes(normApp(p.key))}
                onToggle={(c) => toggle(p.key, c)} onStatus={(st) => apply(p.key, st)} onLogin={(body) => login(p.key, body)} />
            ))}
          </div>
        ) : null}
        <div>
          <Button type="button" variant="ghost" size="sm" onClick={() => navigateUrl(PREFERENCES_CONNECTIONS_URL)}>
            All sign-in options in Preferences<ExternalLinkIcon data-icon="inline-end" />
          </Button>
        </div>
      </Row>
    </Rows>
  );
}

function ProviderRow({ provider: p, status: st, on, onToggle, onStatus, onLogin }: {
  provider: ConnectProvider;
  status: ConnectStatus;
  on: boolean;
  onToggle: (on: boolean) => void;
  onStatus: (st: ConnectStatus) => void;
  onLogin: (body: LoginBody) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const act = async (fn: () => Promise<unknown>) => {
    if (busy) return;
    setBusy(true);
    setErr(null);
    try {
      const out = await fn();
      if (out && typeof out === "object" && "state" in out) onStatus(out as ConnectStatus);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const primary = defaultAuth(p.auth);
  const again = againBody(p, st);
  const n = toolCount(st);
  const connected = st.state === "connected";

  let status: ReactNode;
  let control: ReactNode = null;
  switch (st.state) {
    case "pending":
      status = "Waiting for you in the browser…";
      control = <Button variant="outline" size="sm" disabled={busy} onClick={() => void act(() => cancelConnection(p.key))}>Cancel</Button>;
      break;
    case "connected":
      status = <>Connected{st.account ? <> as <b>{st.account}</b></> : null}{n !== null ? ` · ${n} ${n === 1 ? "tool" : "tools"}` : null}</>;
      control = <Button variant="outline" size="sm" disabled={busy} onClick={() => void act(() => logoutConnection(p.key))}>Disconnect</Button>;
      break;
    case "reconnect":
      status = "Needs reconnecting";
      control = <Button variant="outline" size="sm" disabled={busy || !again.kind} onClick={() => void act(() => onLogin(again))}>Reconnect</Button>;
      break;
    case "error":
      status = <span className="text-destructive">{st.error || "The last sign-in failed."}</span>;
      control = <Button variant="outline" size="sm" disabled={busy || !again.kind} onClick={() => void act(() => onLogin(again))}>Try again</Button>;
      break;
    default:
      status = primary ? "Not connected" : p.auth.find((a) => a.reason)?.reason || "Not available on this machine.";
      // A pasted key or a BYO client id needs a form: that lives on the Preferences page.
      control = primary && primary.kind !== "api-key"
        ? <Button variant="outline" size="sm" disabled={busy} onClick={() => void act(() => onLogin({ kind: primary.kind }))}>Connect</Button>
        : <Button variant="outline" size="sm" onClick={() => navigateUrl(PREFERENCES_CONNECTIONS_URL)}>Set up in Preferences</Button>;
  }

  return (
    <div className="flex flex-col gap-2 px-3 py-2.5 text-sm" data-provider={p.key} data-on={on ? "1" : undefined}>
      <div className="flex items-center gap-2.5">
        <ConnectIcon provider={p} />
        <div className="flex min-w-0 flex-1 flex-col">
          <span className="font-medium">{p.name}</span>
          <span className="truncate text-xs text-muted-foreground">{status}</span>
        </div>
        {control}
        <label className="ml-2 flex shrink-0 cursor-pointer items-center gap-2 text-xs text-muted-foreground" title={connected ? "Let this bot use it without asking" : "Takes effect once the service is connected"}>
          <span>{on ? "On for this bot" : "Off for this bot"}</span>
          <Switch checked={on} onCheckedChange={(c) => onToggle(!!c)} aria-label={`${p.name}: this bot may use it`} />
        </label>
      </div>
      {st.state === "pending" && st.user_code ? (
        <div className="flex flex-wrap items-center gap-3 pl-[30px]">
          <span className="font-mono text-lg font-semibold tracking-[0.2em]" aria-label="Your code">{st.user_code}</span>
          <Button variant="outline" size="sm" onClick={() => void copyToClipboard(st.user_code!).then((ok) => setCopied(ok))}>
            <Copy data-icon="inline-start" aria-hidden="true" />{copied ? "Copied" : "Copy"}
          </Button>
          {st.verification_uri ? <a className="text-xs underline" href={st.verification_uri} target="_blank" rel="noopener noreferrer">Enter it at {st.verification_uri}</a> : null}
        </div>
      ) : null}
      {st.state === "pending" && st.url ? (
        <p className="m-0 pl-[30px] text-xs text-muted-foreground">
          Didn&apos;t see a tab open? <a className="underline" href={st.url} target="_blank" rel="noopener noreferrer">Open again</a>
        </p>
      ) : null}
      {err ? <p className="m-0 pl-[30px] text-xs text-destructive">{err}</p> : null}
    </div>
  );
}
