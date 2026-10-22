// Preferences → Connections (SPEC-connect-oauth.md §6, §11): one card per
// provider the server's registry knows (Notion, GitHub, Slack...), each with
// its sign-in, its state and the tool count its MCP server (or native pack)
// offers. `?tab=connections`.
//
// NO TOKEN PASSES THROUGH THIS FILE, in either direction — the same rule as
// settings/accounts.tsx's sign-ins. A login starts the server's flow
// (`POST /api/connect/<key>/login`), the vendor's consent page opens in a new
// tab, the server stores what comes back, and this page only reads the state
// and the account name. The one exception is a pasted API key, which goes up
// once and is never read back.
//
// POLLING: once a second while any card is pending (a person is finishing a
// browser round trip, and the page should flip the moment they do), else
// every 30 s (a token can lapse, or another window can connect). Only the
// pending cards are re-read on the fast cadence.
//
// THE PRIMARY BUTTON is the first auth kind the server lists as available;
// everything else (paste a key, bring your own OAuth app, a CLI login) is
// under Advanced, so the card a newcomer sees has one button on it.
import { useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { Copy } from "lucide-react";
import { getJson, postJson } from "@platform/lib/api";
import { copyToClipboard } from "@platform/lib/clipboard";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { PointerRow } from "./settings/ai";
import { rowId, SettingsViewContext } from "./settings/SettingRow";

// ── The /api/connect contract (SPEC §11 manager.list / manager.status) ─────

export type ConnectState = "disconnected" | "pending" | "connected" | "reconnect" | "error";

export interface ConnectAuth {
  kind: string;
  label?: string;
  available: boolean;
  reason?: string | null;
  byo?: { manifest?: string; docs?: string } | null;
  /** api-key: where to make one. */
  docs?: string | null;
}

export interface ConnectStatus {
  state: ConnectState;
  kind?: string | null;
  account?: string | null;
  url?: string | null;
  user_code?: string | null;
  verification_uri?: string | null;
  error?: string | null;
  /** A count from status(); the list row carries the pack's source with the
   *  count inside it (`{remote, native, count}`). */
  tools?: number | { remote?: string | null; native?: boolean; count?: number } | null;
  store?: "keychain" | "file" | string;
}

export interface ConnectProvider extends ConnectStatus {
  key: string;
  name: string;
  icon?: string | null;
  privacy?: string | null;
  auth: ConnectAuth[];
}

export interface LoginBody {
  kind?: string;
  client_id?: string;
  value?: string;
}

const connectPath = (key: string, tail = "") => `/api/connect/${encodeURIComponent(key)}${tail}`;

export function listConnections(): Promise<ConnectProvider[]> {
  return getJson<ConnectProvider[] | { providers?: ConnectProvider[] }>("/api/connect").then((d) =>
    Array.isArray(d) ? d : (d?.providers ?? []),
  );
}
export const getConnection = (key: string) => getJson<ConnectStatus>(connectPath(key));
export const loginConnection = (key: string, body: LoginBody) =>
  postJson<ConnectStatus>(connectPath(key, "/login"), body);
export const cancelConnection = (key: string) => postJson<ConnectStatus>(connectPath(key, "/cancel"), {});
export const logoutConnection = (key: string) => postJson<ConnectStatus>(connectPath(key, "/logout"), {});
export const pasteCallback = (key: string, url: string) =>
  postJson<ConnectStatus>(connectPath(key, "/callback"), { url });

export const FAST_POLL_MS = 1000;
export const SLOW_POLL_MS = 30_000;

/** How long until the next status read: fast while a sign-in is in flight. */
export function pollDelay(states: readonly ConnectState[]): number {
  return states.includes("pending") ? FAST_POLL_MS : SLOW_POLL_MS;
}

/** The kind the card's main button uses: the first one the server says works. */
export function defaultAuth(auth: readonly ConnectAuth[]): ConnectAuth | null {
  return auth.find((a) => a.available) ?? null;
}

export function toolCount(st: ConnectStatus): number | null {
  if (typeof st.tools === "number") return st.tools;
  if (st.tools && typeof st.tools.count === "number") return st.tools.count;
  return null;
}

const KIND_LABELS: Record<string, string> = {
  "mcp-oauth": "Sign in with your browser",
  "oauth-pkce": "Sign in with your own OAuth app",
  "oauth-device": "Sign in with a device code",
  cli: "Use the command-line login",
  "api-key": "Paste an API key",
};
const kindLabel = (a: ConnectAuth) => a.label || KIND_LABELS[a.kind] || a.kind;

// ── The page ────────────────────────────────────────────────────────────────

export default function Connections() {
  const [rows, setRows] = useState<ConnectProvider[] | null>(null);
  const [statuses, setStatuses] = useState<Record<string, ConnectStatus>>({});
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  // Keys whose login this page started and whose consent URL it has not
  // opened yet. The flow runs on a server thread, so the URL can arrive on
  // the login's answer or on a poll after it; either way it opens once.
  const wantOpen = useRef(new Set<string>());
  const statusesRef = useRef(statuses);
  statusesRef.current = statuses;

  useEffect(() => {
    let alive = true;
    listConnections()
      .then((r) => alive && setRows(r))
      .catch((e) => alive && setError((e as Error).message));
    return () => {
      alive = false;
    };
  }, []);

  const apply = useCallback((key: string, st: ConnectStatus) => {
    if (wantOpen.current.has(key)) {
      if (st.url) {
        wantOpen.current.delete(key);
        window.open(st.url, "_blank");
      } else if (st.state !== "pending") {
        wantOpen.current.delete(key);
      }
    }
    setStatuses((prev) => ({ ...prev, [key]: st }));
  }, []);

  const stateOf = (p: ConnectProvider): ConnectStatus => statuses[p.key] ?? p;
  const states = (rows ?? []).map((p) => stateOf(p).state);
  const delay = pollDelay(states);

  useEffect(() => {
    if (!rows || rows.length === 0) return;
    let alive = true;
    const t = window.setTimeout(() => {
      const now = rows.map((p) => ({ key: p.key, st: statusesRef.current[p.key] ?? p }));
      const pending = now.filter((r) => r.st.state === "pending");
      const keys = (delay === FAST_POLL_MS ? pending : now).map((r) => r.key);
      void Promise.all(
        keys.map((k) =>
          getConnection(k)
            .then((s) => alive && apply(k, s))
            .catch(() => undefined), // a blip mid-login is not worth a banner
        ),
      ).then(() => alive && setTick((n) => n + 1));
    }, delay);
    return () => {
      alive = false;
      window.clearTimeout(t);
    };
  }, [rows, delay, tick, apply]);

  const login = async (key: string, body: LoginBody) => {
    wantOpen.current.add(key);
    try {
      apply(key, await loginConnection(key, body));
    } catch (e) {
      wantOpen.current.delete(key);
      throw e;
    }
  };

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

function ProviderIcon({ provider }: { provider: ConnectProvider }) {
  if (provider.icon) {
    return (
      <img
        src={connectPath(provider.key, "/icon")}
        alt=""
        aria-hidden="true"
        className="size-5 shrink-0 rounded object-contain"
      />
    );
  }
  return (
    <span
      aria-hidden="true"
      className="grid size-5 shrink-0 place-items-center rounded bg-muted text-[11px] font-semibold uppercase"
    >
      {(provider.name || provider.key).slice(0, 1)}
    </span>
  );
}

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
  // Reconnect / Try again keep the kind the record was made with when it is
  // still on offer — a BYO client id or a CLI login should not silently
  // become the default flow.
  const again: LoginBody = (() => {
    const was = p.auth.find((a) => a.kind === st.kind && a.available);
    const use = was ?? primary;
    return use ? { kind: use.kind } : {};
  })();
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
            <ProviderIcon provider={p} />
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
