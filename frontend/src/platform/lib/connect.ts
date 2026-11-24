// The `/api/connect` client (SPEC-connect-oauth.md §11 manager.list / manager.status)
// and the one hook that drives it: the provider rows, each one's live status,
// the login → consent tab → poll → connected round trip. Shared by the
// Preferences › Connections page (shell/Connections.tsx) and the bot
// Settings › Connections tab (apps/bots/dialogs/ConnectionsSection.tsx): a
// sign-in is GLOBAL (one token per service on this machine), so both surfaces
// read and write the same thing through this file.
//
// NO TOKEN PASSES THROUGH THIS FILE, in either direction. A login starts the
// server's flow (`POST /api/connect/<key>/login`), the vendor's consent page
// opens in a new tab, the server stores what comes back, and the client only
// reads the state and the account name. The one exception is a pasted API
// key, which goes up once and is never read back.
//
// POLLING: once a second while any card is settling — pending (a person is
// finishing a browser round trip, and the page should flip the moment they
// do) or just connected with its tool count still being listed — else every
// 30 s (a token can lapse, or another window can connect). Only the settling
// cards are re-read on the fast cadence.
import { useCallback, useEffect, useRef, useState } from "react";
import { getJson, postJson } from "./api";

export type ConnectState = "disconnected" | "pending" | "connected" | "reconnect" | "error";

export interface ConnectAuth {
  kind: string;
  label?: string;
  available: boolean;
  reason?: string | null;
  byo?: { manifest?: string; docs?: string } | null;
  /** api-key: where to make one. */
  docs?: string | null;
  /** oauth-pkce with an https localhost redirect (Slack): the browser will warn about the listener's own
   *  certificate — the card opens ConnectCertNotice before Connect and says so while pending (CERT_WARNING). */
  cert_warning?: boolean;
}

export interface ConnectStatus {
  state: ConnectState;
  kind?: string | null;
  account?: string | null;
  url?: string | null;
  opened?: boolean;
  user_code?: string | null;
  verification_uri?: string | null;
  error?: string | null;
  /** pending, https on localhost (Slack): the browser will warn about the listener's own certificate. */
  cert_warning?: boolean;
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

export const connectPath = (key: string, tail = "") => `/api/connect/${encodeURIComponent(key)}${tail}`;

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

/** A status worth re-reading every second: a sign-in in flight, or a fresh
 *  connection whose tool count the server is still listing (`tools: null`). */
export function settling(st: ConnectStatus): boolean {
  return st.state === "pending" || (st.state === "connected" && toolCount(st) === null);
}

/** How long until the next status read: fast while any card is settling. */
export function pollDelay(items: readonly (ConnectState | ConnectStatus)[]): number {
  const fast = items.some((x) => (typeof x === "string" ? x === "pending" : settling(x)));
  return fast ? FAST_POLL_MS : SLOW_POLL_MS;
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
  "local-app": "Use the app on this Mac",
};
export const kindLabel = (a: ConnectAuth) => a.label || KIND_LABELS[a.kind] || a.kind;

/** An app on this Mac (Apple Notes, Apple Calendar): no browser, no token. Its
 *  sign-in is macOS's own "allow Fused to control Notes?" prompt, so a pending
 *  card waits on that, and the paste-the-address fallback does not apply. */
export const isLocalApp = (kind: string | null | undefined) => kind === "local-app";
export const pendingLabel = (st: ConnectStatus) =>
  isLocalApp(st.kind) ? "Waiting for macOS to allow access…" : "Waiting for you in the browser…";

/** The sentence under a pending card whose redirect is https on localhost: the
 *  listener's certificate is the app's own, so the browser warns once. Both
 *  ways on: click through, or paste the address the warning page sits on. */
export const CERT_WARNING =
  "Your browser will warn that the page is not secure: the sign-in lands on this app's own localhost certificate. " +
  "Choose Advanced › Proceed, or paste the address the warning page sits on here.";

/** Reconnect / Try again keep the kind the record was made with when it is
 *  still on offer — a CLI login should not silently become the default flow.
 *  The server keeps the client id the stored record was made with (a pasted
 *  or environment one), so the body carries only the kind. */
export function againBody(p: ConnectProvider, st: ConnectStatus): LoginBody {
  const primary = defaultAuth(p.auth);
  const was = p.auth.find((a) => a.kind === st.kind && a.available);
  const use = was ?? primary;
  return use ? { kind: use.kind } : {};
}

export interface Connections {
  /** null until the first list answers. */
  rows: ConnectProvider[] | null;
  error: string | null;
  /** The live status of a row (its own, until a poll or an action replaced it). */
  stateOf: (p: ConnectProvider) => ConnectStatus;
  /** Fold an answer (a login's, a cancel's, a logout's) into the live statuses. */
  apply: (key: string, st: ConnectStatus) => void;
  /** Start a sign-in and open the consent page once it is known. */
  login: (key: string, body: LoginBody) => Promise<void>;
}

/** The provider rows and their polled statuses, the way the Preferences page
 *  keeps them (see the POLLING note above). */
export function useConnections(): Connections {
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
        // The server opens the consent page itself when it can (desktop app:
        // same machine); it says so with `opened`, and a second tab is noise.
        // noopener: a consent page (for mcp-oauth, whatever server the resource's
        // metadata names) must not be able to steer this tab through window.opener.
        if (!st.opened) window.open(st.url, "_blank", "noopener");
      } else if (st.state !== "pending") {
        wantOpen.current.delete(key);
      }
    }
    setStatuses((prev) => ({ ...prev, [key]: st }));
  }, []);

  const stateOf = useCallback((p: ConnectProvider): ConnectStatus => statuses[p.key] ?? p, [statuses]);
  const delay = pollDelay((rows ?? []).map((p) => stateOf(p)));

  useEffect(() => {
    if (!rows || rows.length === 0) return;
    let alive = true;
    const t = window.setTimeout(() => {
      const now = rows.map((p) => ({ key: p.key, st: statusesRef.current[p.key] ?? p }));
      const keys = (delay === FAST_POLL_MS ? now.filter((r) => settling(r.st)) : now).map((r) => r.key);
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

  const login = useCallback(
    async (key: string, body: LoginBody) => {
      wantOpen.current.add(key);
      try {
        apply(key, await loginConnection(key, body));
      } catch (e) {
        wantOpen.current.delete(key);
        throw e;
      }
    },
    [apply],
  );

  return { rows, error, stateOf, apply, login };
}
