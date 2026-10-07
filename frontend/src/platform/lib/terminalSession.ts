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
import { mutateJson, postJson } from "@platform/lib/api";
import { terminalSizeHint, type GridSize } from "@platform/ui/terminalSizeHint";

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

/** The POST /api/terminal body: `cwd` and, when known, the size the pty
 * should start at (the server sets it before the shell starts, so zsh draws
 * its first prompt for the right width). Omitted keys keep the server's
 * defaults. */
export function createTerminalBody(cwd?: string, size?: GridSize | null): Record<string, unknown> {
  return { ...(cwd ? { cwd } : {}), ...(size ? { rows: size.rows, cols: size.cols } : {}) };
}

/** POST /api/terminal — create a new pty session and return its id. Attach
 * to it with `new TerminalSession({ id, ... })`. Sends a size guess
 * (terminalSizeHint.ts) so the pty is not born 0x0. */
export async function createTerminalSession(cwd?: string): Promise<string> {
  const size = await terminalSizeHint();
  const r = await postJson<{ id: string }>("/api/terminal", createTerminalBody(cwd, size));
  return r.id;
}

/** DELETE /api/terminal/{id} — kill a pty session server-side. Used for a
 * session nobody ever attaches to: a create that resolves after its caller
 * has already moved on (the drawer closed, or a React StrictMode
 * double-mount tore down the effect that started it) leaves an id alive in
 * the server's registry with nothing left to kill it, unless this is called
 * on it explicitly. */
export function killTerminalSession(id: string): Promise<{ ok: boolean }> {
  return mutateJson<{ ok: boolean }>("DELETE", `/api/terminal/${encodeURIComponent(id)}`);
}

/** POST /api/terminal/claude:<chat>/stop — stop the commands that chat's
 * Claude is running right now (the Claude tab's Stop button). */
export function stopClaudeCommands(id: string): Promise<{ ok: boolean; stopped: number }> {
  return postJson<{ ok: boolean; stopped: number }>(`/api/terminal/${encodeURIComponent(id)}/stop`, {});
}

/** POST /api/terminal/{id}/input — write a string straight into the pty
 * without an attached `stream` socket. Used by the "open in terminal / run a
 * command" flow (EntryActionsMenu, `fused.terminal.run`), which sends this
 * right after `createTerminalSession` — before `TerminalDrawer`'s
 * WebSocket has necessarily attached — rather than routing through a live
 * `TerminalSession.write()`. */
export function sendTerminalInput(id: string, data: string): Promise<{ ok: boolean }> {
  return postJson<{ ok: boolean }>(`/api/terminal/${encodeURIComponent(id)}/input`, { data });
}

export interface BuildTerminalCommandArgs {
  cwd?: string;
  command?: string;
  /** Defaults to true. False leaves the trailing Enter off, so the string
   * lands at the prompt typed but not yet run — for a model-suggested
   * command a person should read before it executes. */
  execute?: boolean;
}

/** Build the string to type into a freshly-opened (or already-open) terminal
 * for a "cd here" / "run this" request — POSIX-shell only, matching the pty
 * this session always runs (see pty_session.py: never constructed on
 * Windows). Single-quotes `cwd` the POSIX way (`'` -> `'\''`, ie. close the
 * quote, an escaped literal quote, reopen the quote) so a path with spaces,
 * `$`, or embedded quotes still lands as one argument to `cd`. With both
 * `cwd` and `command`: `cd '<cwd>' && <command>`. With only `cwd`: `cd
 * '<cwd>'`. With only `command`: `<command>`. With neither: `""` (no-op —
 * nothing to send). The trailing `\r` that runs the line is appended unless
 * `execute` is explicitly `false`. */
export function buildTerminalCommand({ cwd, command, execute }: BuildTerminalCommandArgs): string {
  const quotedCwd = cwd ? `'${cwd.replace(/'/g, "'\\''")}'` : undefined;
  const line = quotedCwd && command ? `cd ${quotedCwd} && ${command}` : quotedCwd ? `cd ${quotedCwd}` : command || "";
  if (!line) return "";
  if (execute === false) {
    // A multi-line block typed with execute:false must not let its embedded
    // newlines act as Enter at the prompt for the inner lines. Bracketed
    // paste mode (bash >=5.1, zsh, fish) makes the shell treat the whole
    // span — cd prefix included — as one pasted unit sitting at the prompt
    // instead of running each line as it arrives.
    return line.includes("\n") ? `\x1b[200~${line}\x1b[201~` : line;
  }
  return `${line}\r`;
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
      // this would just get another `{"exit": null}`-then-close from the
      // route for the same id (see routers/terminal.py: an id the registry
      // no longer knows, reaped or never created, gets that same exit frame
      // rather than a bare reject).
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
