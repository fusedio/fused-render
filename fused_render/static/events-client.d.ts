// Types for events-client.js, the one events-bus client both the React shell
// (via the `@static/events-client.js` alias) and runtime.js share. The module
// is a side-effect script: importing it installs `window.fusedEvents`.

export interface FusedEventsMeta {
  /** The generation the frame stands for, for topics that have one. */
  gen?: number | null;
  /** A cached snapshot replayed to a late subscriber, synchronously. */
  replay?: boolean;
  /** The GET's refusal (status + message), when the frame is an `err`. */
  error?: string;
  status?: number;
}

export type FusedEventsCallback<S = unknown, D = unknown> = (
  snapshot: S | null,
  delta: D | null,
  meta: FusedEventsMeta,
) => void;

export interface FusedEventsSubscribeOptions {
  /** Override the server's hidden policy (D7) for this subscription. */
  hiddenOk?: boolean;
}

export interface FusedEventsHello {
  t: "hello";
  boot_id: string;
  version: string;
  pid: number;
  topics: Record<string, { hidden_ok: boolean; kind: string }>;
}

export interface FusedEvents {
  subscribe<S = unknown, D = unknown>(
    topic: string,
    params: Record<string, unknown> | null | undefined,
    cb: FusedEventsCallback<S, D>,
    opts?: FusedEventsSubscribeOptions,
  ): () => void;
  /** Ask for a fresh snapshot of a subscribed key now. */
  resync(topic: string, params?: Record<string, unknown> | null): boolean;
  /** The cached snapshot of a subscribed key, or null. */
  snapshot<S = unknown>(topic: string, params?: Record<string, unknown> | null): S | null;
  onHello(fn: (hello: FusedEventsHello, restarted: boolean) => void): () => void;
  onState(fn: (connected: boolean) => void): () => void;
  connected(): boolean;
  bootId(): string | null;
  setHidden(hidden: boolean): void;
  _reset(override?: Record<string, unknown>): void;
  _subscriptions(): number;
  _wire(): number;
}

declare global {
  interface Window {
    fusedEvents: FusedEvents;
  }
  // eslint-disable-next-line no-var
  var fusedEvents: FusedEvents;
}

export {};
