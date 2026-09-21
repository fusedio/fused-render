// The status-bar terminal's client-side session: build the attach URL, frame
// bytes on and off a WebSocket, and reconnect with backoff when the socket
// drops out from under a live shell (sleep/wake, a flaky LAN hop, a server
// restart) — the shell itself is still alive server-side (pty_session.py)
// and a reattach replays its scrollback, so losing the socket must not lose
// the session.
//
// Modeled on apps/explorer/listing/useDirListing.ts's dir-watch socket (same
// "WebSockets don't auto-reconnect the way EventSource did, so schedule a
// retry from onclose" shape), but the frame contract here is a raw byte pipe
// plus a JSON side channel rather than JSON-only, so frames are typed by
// their WebSocket frame kind (binary vs text) instead of by a field inside
// one shape — see fused_render/server/routers/terminal.py's module docstring
// for the exact contract this mirrors:
//   client -> server: BINARY = keystrokes/paste; TEXT = a JSON control frame,
//     today only `{"resize": [rows, cols]}`.
//   server -> client: BINARY = shell output; TEXT = JSON, today only
//     `{"exit": code}`, sent once, when the child dies.
//
// This module has no DOM/React dependency by design (Decisions in
// PLAN-status-bar-terminal.md) — everything here is testable against a fake
// WebSocket with no renderer involved. `platform/ui/TerminalView.tsx` is the
// thin xterm.js wrapper that owns one of these.
import { postJson } from "@platform/lib/api";

export type TerminalStatus = "connecting" | "open" | "closed";

/** The subset of the DOM `WebSocket` this module actually uses — small
 * enough that a test's fake socket only has to implement this, not the real
 * interface's many read-only fields and constants. */
export interface TerminalSocketLike {
  binaryType?: string;
  readyState: number;
  onopen: (() => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  onclose: (() => void) | null;
  send(data: string | ArrayBuffer | ArrayBufferView): void;
  close(): void;
}

//: WebSocket.OPEN's value, per the DOM spec — a fake socket in a test can set
//: this numeric readyState directly without needing the real global's other
//: three constants (CONNECTING/CLOSING/CLOSED) this module never reads.
const OPEN = 1;

export interface TerminalSessionCallbacks {
  /** One chunk of the shell's raw output. */
  onData: (chunk: Uint8Array) => void;
  /** The child has exited; `code` is its exit status (or null if unknown).
   * Sent once, and reconnecting after it is pointless — the server-side
   * session is gone — so this module stops retrying once this fires. */
  onExit: (code: number | null) => void;
  /** Optional: connection status, for a UI that wants to grey out the pane
   * or show "reconnecting…" while the socket is down. */
  onStatus?: (status: TerminalStatus) => void;
}

export interface TerminalSessionOptions extends TerminalSessionCallbacks {
  /** An existing pty session id (from `createTerminalSession`). */
  id: string;
  /** Test-only: build the socket instead of `new WebSocket(url)`. */
  wsFactory?: (url: string) => TerminalSocketLike;
  /** Test-only: the reconnect delay sequence, in ms, indexed by attempt
   * number and clamped to the last entry once attempts run past its length.
   * Real callers get real (larger) delays via the default. */
  backoffScheduleMs?: number[];
  /** Test-only: override `location`-derived host/protocol lookup. */
  wsUrl?: (id: string) => string;
}

const DEFAULT_BACKOFF_MS = [500, 1000, 2000, 4000, 8000];

export function terminalStreamUrl(id: string): string {
  const proto = location.protocol === "https:" ? "wss://" : "ws://";
  return `${proto}${location.host}/api/terminal/${encodeURIComponent(id)}/stream`;
}

/** POST /api/terminal — create a new pty session and return its id. Attach
 * to it with `new TerminalSession({ id, ... })`. */
export function createTerminalSession(cwd?: string): Promise<string> {
  return postJson<{ id: string }>("/api/terminal", cwd ? { cwd } : {}).then((r) => r.id);
}

/** One attached terminal: owns the WebSocket for a pty session id, decodes
 * its frames, and reconnects with backoff on an unexpected close. Callers
 * MUST call `dispose()` exactly once (unmount, drawer close) — it is the
 * only thing that stops a pending reconnect timer or a live socket. */
export class TerminalSession {
  readonly id: string;

  private readonly callbacks: TerminalSessionCallbacks;
  private readonly wsFactory: (url: string) => TerminalSocketLike;
  private readonly backoff: number[];
  private readonly buildUrl: (id: string) => string;

  private sock: TerminalSocketLike | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private attempt = 0;
  private disposed = false;
  private exited = false;

  constructor(opts: TerminalSessionOptions) {
    this.id = opts.id;
    this.callbacks = opts;
    this.wsFactory = opts.wsFactory ?? ((url) => new WebSocket(url) as unknown as TerminalSocketLike);
    this.backoff = opts.backoffScheduleMs ?? DEFAULT_BACKOFF_MS;
    this.buildUrl = opts.wsUrl ?? terminalStreamUrl;
    this.connect();
  }

  private connect(): void {
    if (this.disposed || this.exited) return;
    this.callbacks.onStatus?.("connecting");
    const sock = this.wsFactory(this.buildUrl(this.id));
    sock.binaryType = "arraybuffer";
    this.sock = sock;
    sock.onopen = () => {
      this.attempt = 0;
      this.callbacks.onStatus?.("open");
    };
    sock.onmessage = (ev) => this.handleMessage(ev.data);
    sock.onclose = () => {
      this.sock = null;
      if (this.disposed || this.exited) return;
      this.callbacks.onStatus?.("closed");
      this.scheduleReconnect();
    };
  }

  private handleMessage(data: unknown): void {
    if (typeof data === "string") {
      this.handleControl(data);
      return;
    }
    const bytes =
      data instanceof Uint8Array
        ? data
        : data instanceof ArrayBuffer
          ? new Uint8Array(data)
          : null;
    if (bytes) this.callbacks.onData(bytes);
  }

  private handleControl(text: string): void {
    let msg: unknown;
    try {
      msg = JSON.parse(text);
    } catch {
      return;
    }
    if (msg && typeof msg === "object" && "exit" in msg) {
      // A dead server-side session is gone for good — reconnecting after
      // this would just get another 1008 close from the route (see
      // routers/terminal.py: unknown/dead ids reject the handshake).
      this.exited = true;
      if (this.retryTimer !== null) {
        clearTimeout(this.retryTimer);
        this.retryTimer = null;
      }
      const code = (msg as { exit: unknown }).exit;
      this.callbacks.onExit(typeof code === "number" ? code : null);
    }
  }

  private scheduleReconnect(): void {
    const delay = this.backoff[Math.min(this.attempt, this.backoff.length - 1)];
    this.attempt += 1;
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this.connect();
    }, delay);
  }

  /** Send keystrokes/paste as a BINARY frame. */
  write(data: string | Uint8Array): void {
    if (!this.sock || this.sock.readyState !== OPEN) return;
    this.sock.send(typeof data === "string" ? new TextEncoder().encode(data) : data);
  }

  /** Send a resize control frame (a TEXT frame, per the server's contract). */
  resize(rows: number, cols: number): void {
    if (!this.sock || this.sock.readyState !== OPEN) return;
    this.sock.send(JSON.stringify({ resize: [rows, cols] }));
  }

  /** Tear down: clears any pending reconnect timer and closes a live socket.
   * Idempotent. No callback fires as a result of calling this. */
  dispose(): void {
    this.disposed = true;
    if (this.retryTimer !== null) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    if (this.sock) {
      const sock = this.sock;
      this.sock = null;
      sock.onopen = null;
      sock.onmessage = null;
      sock.onclose = null;
      sock.close();
    }
  }
}
