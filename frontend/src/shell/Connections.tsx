// Preferences → Connections (SPEC-connect-oauth.md §6, §11): one card per
// provider the server's registry knows (Notion, GitHub, Slack...), each with
// its sign-in, its state and the tool count its MCP server (or native pack)
// offers. `?tab=connections`.
//
// The client, the types and the polling live in platform/lib/connect.ts (the
// bot Settings › Connections tab shares them: a sign-in is global); this file
// is the page and its cards. NO TOKEN PASSES THROUGH HERE — see that file.
//
// THE PRIMARY BUTTON is the first auth kind the server lists as available;
// everything else (paste a key, bring your own OAuth app, a CLI login) is
// under Advanced, so the card a newcomer sees has one button on it.
import { useContext, useState, type ReactNode } from "react";
import { Copy } from "lucide-react";
import {
  againBody,
  cancelConnection,
  defaultAuth,
  kindLabel,
  logoutConnection,
  pasteCallback,
  toolCount,
  useConnections,
  type ConnectAuth,
  type ConnectProvider,
  type ConnectStatus,
  type LoginBody,
} from "@platform/lib/connect";
import { copyToClipboard } from "@platform/lib/clipboard";
import { ConnectIcon } from "@platform/ui/ConnectIcon";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { PointerRow } from "./settings/ai";
import { rowId, SettingsViewContext } from "./settings/SettingRow";

// The contract and its helpers, re-exported for the callers (and the test)
// that reached them here before they moved to platform/lib/connect.ts.
export {
  defaultAuth,
  FAST_POLL_MS,
  pollDelay,
  SLOW_POLL_MS,
  toolCount,
  type ConnectAuth,
  type ConnectProvider,
  type ConnectState,
  type ConnectStatus,
  type LoginBody,
} from "@platform/lib/connect";

// ── The page ────────────────────────────────────────────────────────────────

export default function Connections() {
  const { rows, error, stateOf, apply, login } = useConnections();

  return (
    <section className="settings-section" aria-label="Services" id={rowId("connections")} data-setting="connections">
      <h3 className="settings-section-title">Services</h3>
      {error && <ErrorBanner>{error}</ErrorBanner>}
      {!rows && !error && <SkeletonLines rows={3} label="Loading connections" />}
      {rows && rows.length === 0 && (
        <div className="settings-card">
          <div className="settings-row">
            <p className="settings-row-desc">No services to connect on this build.</p>
          </div>
        </div>
      )}
      {rows && rows.length > 0 && (
        <div className="flex flex-col gap-3">
          {rows.map((p) => (
            <ProviderCard
              key={p.key}
              provider={p}
              status={stateOf(p)}
              onStatus={(st) => apply(p.key, st)}
              onLogin={(body) => login(p.key, body)}
            />
          ))}
        </div>
      )}
    </section>
  );
}

/** The registry row for search results: a door into the group. */
export function ConnectionsPointer() {
  const view = useContext(SettingsViewContext);
  return <PointerRow id="connections" to={() => view.jump("connections")} label="Connections" />;
}

// ── One provider ───────────────────────────────────────────────────────────

function ProviderCard({
  provider: p,
  status: st,
  onStatus,
  onLogin,
}: {
  provider: ConnectProvider;
  status: ConnectStatus;
  onStatus: (st: ConnectStatus) => void;
  onLogin: (body: LoginBody) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const act = async (fn: () => Promise<unknown>) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const out = await fn();
      if (out && typeof out === "object" && "state" in out) onStatus(out as ConnectStatus);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const login = (body: LoginBody) => act(() => onLogin(body));

  const primary = defaultAuth(p.auth);
  // A key has nothing to open in a browser: when it is the default, its field
  // is on the card itself rather than behind a Connect that could not work.
  const primaryIsKey = primary?.kind === "api-key";
  const others = p.auth.filter((a) => a !== primary);
  const again: LoginBody = againBody(p, st);
  const n = toolCount(st);

  let status: ReactNode;
  let control: ReactNode = null;
  switch (st.state) {
    case "pending":
      status = "Waiting for you in the browser…";
      control = (
        <Button variant="outline" size="sm" disabled={busy} onClick={() => void act(() => cancelConnection(p.key))}>
          Cancel
        </Button>
      );
      break;
    case "connected":
      status = (
        <>
          <span className="settings-dot">
            Connected{st.account ? <> as <b>{st.account}</b></> : null}
          </span>
          {n !== null && (
            <>
              {" · "}
              {n} {n === 1 ? "tool" : "tools"}
            </>
          )}
        </>
      );
      control = (
        <Button variant="destructive" size="sm" disabled={busy} onClick={() => void act(() => logoutConnection(p.key))}>
          Disconnect
        </Button>
      );
      break;
    case "reconnect":
      status = <span className="settings-dot settings-dot-warn">Needs reconnecting</span>;
      control = (
        <Button variant="accent" size="sm" disabled={busy || !again.kind} onClick={() => void login(again)}>
          Reconnect
        </Button>
      );
      break;
    case "error":
      status = <span className="settings-warn">{st.error || "The last sign-in failed."}</span>;
      control = (
        <Button variant="accent" size="sm" disabled={busy || !again.kind} onClick={() => void login(again)}>
          Try again
        </Button>
      );
      break;
    default:
      status = primary ? "Not connected" : p.auth.find((a) => a.reason)?.reason || "Not available on this machine.";
      if (primary && !primaryIsKey) {
        control = (
          <Button variant="accent" size="sm" disabled={busy} onClick={() => void login({ kind: primary.kind })}>
            Connect
          </Button>
        );
      }
  }

  return (
    <div className="settings-card" data-provider={p.key}>
      <div className="settings-row">
        <div className="settings-row-main">
          <div className="settings-row-label">
            <ConnectIcon provider={p} />
            <span>{p.name}</span>
          </div>
          <div className="settings-row-status">{status}</div>
        </div>
        {control && <div className="settings-row-control">{control}</div>}
        <div className="settings-row-extra">
          {st.state === "pending" && <PendingDetails provider={p} status={st} busy={busy} act={act} />}
          {st.state === "disconnected" && primaryIsKey && primary && (
            <KeyForm auth={primary} busy={busy} onSave={(value) => login({ kind: "api-key", value })} />
          )}
          {error && <ErrorBanner>{error}</ErrorBanner>}
          {p.privacy && <p className="settings-row-desc">{p.privacy}</p>}
          {st.store === "file" && (
            <p className="settings-row-desc">Stored in a private file in your Fused folder (no keychain on this OS)</p>
          )}
          {others.length > 0 && (
            // Open from the start when the card has no button of its own:
            // what is under it (a BYO client id) is then the only way in.
            <details className="settings-row-desc" open={!primary || undefined}>
              <summary className="cursor-pointer select-none">Advanced</summary>
              <div className="mt-2 flex flex-col gap-3">
                {others.map((a) => (
                  <OtherKind key={a.kind} auth={a} busy={busy} onLogin={login} />
                ))}
              </div>
            </details>
          )}
        </div>
      </div>
    </div>
  );
}

function PendingDetails({
  provider: p,
  status: st,
  busy,
  act,
}: {
  provider: ConnectProvider;
  status: ConnectStatus;
  busy: boolean;
  act: (fn: () => Promise<unknown>) => Promise<void>;
}) {
  const [pasted, setPasted] = useState("");
  const [copied, setCopied] = useState(false);
  return (
    <>
      {st.user_code && (
        <div className="flex flex-col gap-1">
          <div className="flex flex-wrap items-center gap-3">
            <span className="font-mono text-2xl font-semibold tracking-[0.2em]" aria-label="Your code">
              {st.user_code}
            </span>
            <Button
              variant="outline"
              size="sm"
              onClick={() => void copyToClipboard(st.user_code!).then((ok) => setCopied(ok))}
            >
              <Copy data-icon="inline-start" aria-hidden="true" />
              {copied ? "Copied" : "Copy"}
            </Button>
          </div>
          {st.verification_uri && (
            <p className="settings-row-desc">
              Enter it at{" "}
              <a className="settings-link" href={st.verification_uri} target="_blank" rel="noopener noreferrer">
                {st.verification_uri}
              </a>
            </p>
          )}
        </div>
      )}
      {st.url && (
        <p className="settings-row-desc">
          Didn&apos;t see a tab open?{" "}
          <a className="settings-link" href={st.url} target="_blank" rel="noopener noreferrer">
            Open again
          </a>
        </p>
      )}
      {!st.user_code && (
        <details className="settings-row-desc">
          <summary className="cursor-pointer select-none">
            Browser on another machine? Paste the address it landed on
          </summary>
          <form
            className="mt-2 flex flex-wrap gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              const url = pasted.trim();
              if (url) void act(() => pasteCallback(p.key, url)).then(() => setPasted(""));
            }}
          >
            <Input
              value={pasted}
              onChange={(e) => setPasted(e.target.value)}
              placeholder="http://localhost:47823/callback?code=…"
              aria-label={`Address the ${p.name} sign-in landed on`}
              className="min-w-0 flex-1 basis-56"
              autoComplete="off"
              spellCheck={false}
            />
            <Button type="submit" variant="outline" size="sm" disabled={busy || !pasted.trim()}>
              Continue
            </Button>
          </form>
        </details>
      )}
    </>
  );
}

function KeyForm({ auth, busy, onSave }: { auth: ConnectAuth; busy: boolean; onSave: (value: string) => Promise<void> }) {
  const [value, setValue] = useState("");
  return (
    <form
      className="flex flex-wrap items-center gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        const v = value.trim();
        if (v) void onSave(v).then(() => setValue(""));
      }}
    >
      <Input
        type="password"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder={auth.label || "API key"}
        aria-label={auth.label || "API key"}
        className="min-w-0 flex-1 basis-56"
        autoComplete="off"
        spellCheck={false}
      />
      <Button type="submit" variant="outline" size="sm" disabled={busy || !value.trim()}>
        Save
      </Button>
      {auth.docs && (
        <a className="settings-link" href={auth.docs} target="_blank" rel="noopener noreferrer">
          Get one
        </a>
      )}
    </form>
  );
}

function OtherKind({
  auth: a,
  busy,
  onLogin,
}: {
  auth: ConnectAuth;
  busy: boolean;
  onLogin: (body: LoginBody) => Promise<void>;
}) {
  const [clientId, setClientId] = useState("");
  const head = <div className="font-medium text-[var(--fg)]">{kindLabel(a)}</div>;

  if (a.kind === "api-key") {
    return (
      <div className="flex flex-col gap-1.5" data-kind={a.kind}>
        {head}
        {a.available ? (
          <KeyForm auth={a} busy={busy} onSave={(value) => onLogin({ kind: "api-key", value })} />
        ) : (
          <span>{a.reason}</span>
        )}
      </div>
    );
  }

  // Bring your own OAuth app: the one kind whose "unavailable" (no built-in
  // client id) is exactly what the field fixes, so it is offered either way.
  if (a.kind === "oauth-pkce" && a.byo) {
    return (
      <div className="flex flex-col gap-1.5" data-kind={a.kind}>
        {head}
        {!a.available && a.reason && <span>{a.reason}</span>}
        <form
          className="flex flex-wrap items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            const id = clientId.trim();
            if (id) void onLogin({ kind: a.kind, client_id: id });
          }}
        >
          <Input
            value={clientId}
            onChange={(e) => setClientId(e.target.value)}
            placeholder="Client ID"
            aria-label="Client ID"
            className="min-w-0 flex-1 basis-48"
            autoComplete="off"
            spellCheck={false}
          />
          <Button type="submit" variant="outline" size="sm" disabled={busy || !clientId.trim()}>
            Connect
          </Button>
        </form>
        {a.byo.docs && (
          <a className="settings-link self-start" href={a.byo.docs} target="_blank" rel="noopener noreferrer">
            Create from manifest
          </a>
        )}
      </div>
    );
  }

  return (
    <div className="flex flex-wrap items-center justify-between gap-2" data-kind={a.kind}>
      <div className="min-w-0">
        {head}
        {!a.available && a.reason && <span>{a.reason}</span>}
      </div>
      {a.available && (
        <Button variant="outline" size="sm" disabled={busy} onClick={() => void onLogin({ kind: a.kind })}>
          Connect
        </Button>
      )}
    </div>
  );
}
