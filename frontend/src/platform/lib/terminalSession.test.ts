// TerminalSession has no DOM dependency (see the module's own header), so
// every test here drives a hand-written fake socket rather than a real
// WebSocket or a DOM shim. Per the bun-test memory discipline for this
// feature (a bare `bun test` runs the whole suite in one JSC heap): every
// socket and session is a `let`-scoped local created in the test body, and
// `afterEach` disposes whatever the just-finished test made — nothing here
// is held at module scope, and no test leaves a pending `setTimeout` behind
// (every `TerminalSession` is disposed, which clears its retry timer).
import { afterEach, describe, expect, test } from "bun:test";

import { installDomShim } from "@platform/lib/testDomShim";
// Type-only: erased at compile time, so it never triggers the module's real
// evaluation (and its `location` read) the way a value import would.
import type {
  TerminalSession as TerminalSessionType,
  TerminalSocketLike,
} from "@platform/lib/terminalSession";

// terminalSession.ts imports api.ts -> presence.ts -> router.ts, which reads
// `location` at MODULE SCOPE (see api.test.ts's own comment on this same
// trap) — the shim has to be installed before that import evaluates, so this
// is a dynamic import rather than a static one.
installDomShim();

const { TerminalSession, terminalStreamUrl } = await import("@platform/lib/terminalSession");

const OPEN = 1;
const CLOSED = 3;

/** A fake socket the test drives by hand: `factory()` makes one and returns
 * both the socket and a way to fire its lifecycle events, so a test can
 * simulate the server side without a real network stack. Every field a real
 * WebSocket has is stubbed no more elaborately than TerminalSession's own
 * `TerminalSocketLike` interface needs. */
function fakeSocket(): {
  socket: TerminalSocketLike;
  sent: (string | ArrayBuffer | ArrayBufferView)[];
  closeCalls: number;
  open(): void;
  message(data: unknown): void;
  serverClose(): void;
} {
  const sent: (string | ArrayBuffer | ArrayBufferView)[] = [];
  let closeCalls = 0;
  const socket: TerminalSocketLike = {
    readyState: 0,
    onopen: null,
    onmessage: null,
    onclose: null,
    send(data) {
      sent.push(data);
    },
    close() {
      closeCalls += 1;
      socket.readyState = CLOSED;
    },
  };
  return {
    socket,
    sent,
    get closeCalls() {
      return closeCalls;
    },
    open() {
      socket.readyState = OPEN;
      socket.onopen?.();
    },
    message(data: unknown) {
      socket.onmessage?.({ data });
    },
    serverClose() {
      socket.readyState = CLOSED;
      socket.onclose?.();
    },
  };
}

describe("terminalStreamUrl", () => {
  test("builds a ws(s) URL from location and the session id, percent-encoded", () => {
    const originalLocation = (globalThis as { location?: unknown }).location;
    (globalThis as { location?: unknown }).location = {
      protocol: "https:",
      host: "example.test:1234",
    };
    try {
      expect(terminalStreamUrl("ab cd")).toBe(
        "wss://example.test:1234/api/terminal/ab%20cd/stream",
      );
    } finally {
      (globalThis as { location?: unknown }).location = originalLocation;
    }
  });

  test("uses ws:// for a plain-http page", () => {
    const originalLocation = (globalThis as { location?: unknown }).location;
    (globalThis as { location?: unknown }).location = { protocol: "http:", host: "x" };
    try {
      expect(terminalStreamUrl("s1")).toBe("ws://x/api/terminal/s1/stream");
    } finally {
      (globalThis as { location?: unknown }).location = originalLocation;
    }
  });
});

describe("TerminalSession", () => {
  // Scoped per-test, torn down in afterEach — never module-scope state
  // holding a live session.
  let session: TerminalSessionType | null = null;

  afterEach(() => {
    session?.dispose();
    session = null;
  });

  test("connects to the id's stream URL via the injected factory", () => {
    // A mutable box rather than a bare `let`: TS narrows a `let` captured only
    // by a callback's reassignment back to its literal initializer at the
    // read site below, since it cannot see the callback run. A boxed field
    // has no such literal-initializer narrowing.
    const requested: { url: string | null } = { url: null };
    const fake = fakeSocket();
    session = new TerminalSession({
      id: "sid-1",
      onData: () => {},
      onExit: () => {},
      wsFactory: (url) => {
        requested.url = url;
        return fake.socket;
      },
      wsUrl: (id) => `ws://fake/${id}/stream`,
    });
    expect(requested.url).toBe("ws://fake/sid-1/stream");
    expect(fake.socket.binaryType).toBe("arraybuffer");
  });

  test("decodes a binary frame as output", () => {
    const fake = fakeSocket();
    const chunks: Uint8Array[] = [];
    session = new TerminalSession({
      id: "sid-1",
      onData: (c) => chunks.push(c),
      onExit: () => {},
      wsFactory: () => fake.socket,
      wsUrl: () => "ws://fake",
    });
    fake.open();
    const payload = new TextEncoder().encode("hello").buffer;
    fake.message(payload);
    expect(chunks).toHaveLength(1);
    expect(new TextDecoder().decode(chunks[0])).toBe("hello");
  });

  test("decodes a JSON exit frame and stops retrying", () => {
    const fake = fakeSocket();
    let exitCode: number | null | undefined;
    let status: string[] = [];
    session = new TerminalSession({
      id: "sid-1",
      onData: () => {},
      onExit: (c) => {
        exitCode = c;
      },
      onStatus: (s) => status.push(s),
      wsFactory: () => fake.socket,
      wsUrl: () => "ws://fake",
      backoffScheduleMs: [1],
    });
    fake.open();
    fake.message(JSON.stringify({ exit: 7 }));
    expect(exitCode).toBe(7);

    // The server closes the socket right after an exit frame in the real
    // route; that must NOT schedule a reconnect once exit has already fired.
    status = [];
    fake.serverClose();
    expect(status).toEqual([]);
  });

  test("write() sends a binary frame only while the socket is open", () => {
    const fake = fakeSocket();
    session = new TerminalSession({
      id: "sid-1",
      onData: () => {},
      onExit: () => {},
      wsFactory: () => fake.socket,
      wsUrl: () => "ws://fake",
    });
    session.write("x");
    expect(fake.sent).toHaveLength(0); // not open yet

    fake.open();
    session.write("ls\n");
    expect(fake.sent).toHaveLength(1);
    const sentBytes = fake.sent[0] as Uint8Array;
    expect(new TextDecoder().decode(sentBytes)).toBe("ls\n");
  });

  test("resize() sends a JSON text control frame", () => {
    const fake = fakeSocket();
    session = new TerminalSession({
      id: "sid-1",
      onData: () => {},
      onExit: () => {},
      wsFactory: () => fake.socket,
      wsUrl: () => "ws://fake",
    });
    fake.open();
    session.resize(40, 120);
    expect(fake.sent).toEqual([JSON.stringify({ resize: [40, 120] })]);
  });

  test("reconnects with the injected backoff schedule after an unexpected close", async () => {
    const sockets: ReturnType<typeof fakeSocket>[] = [];
    session = new TerminalSession({
      id: "sid-1",
      onData: () => {},
      onExit: () => {},
      wsFactory: () => {
        const fake = fakeSocket();
        sockets.push(fake);
        return fake.socket;
      },
      wsUrl: () => "ws://fake",
      // Tiny real delays so the test runs fast without needing a fake-timer
      // API bun:test doesn't provide for plain `setTimeout` in this repo.
      backoffScheduleMs: [1, 2],
    });
    expect(sockets).toHaveLength(1);
    sockets[0].open();
    sockets[0].serverClose();

    await new Promise((r) => setTimeout(r, 20));
    expect(sockets).toHaveLength(2);

    sockets[1].open();
    sockets[1].serverClose();
    await new Promise((r) => setTimeout(r, 20));
    expect(sockets).toHaveLength(3);
  });

  test("dispose() clears a pending reconnect timer so it never fires", async () => {
    const sockets: ReturnType<typeof fakeSocket>[] = [];
    session = new TerminalSession({
      id: "sid-1",
      onData: () => {},
      onExit: () => {},
      wsFactory: () => {
        const fake = fakeSocket();
        sockets.push(fake);
        return fake.socket;
      },
      wsUrl: () => "ws://fake",
      backoffScheduleMs: [5],
    });
    sockets[0].open();
    sockets[0].serverClose();
    session.dispose();
    session = null; // afterEach must not double-dispose

    await new Promise((r) => setTimeout(r, 30));
    // No second socket was ever created by the (cleared) retry timer.
    expect(sockets).toHaveLength(1);
  });

  test("dispose() closes a live socket and detaches its handlers", () => {
    const fake = fakeSocket();
    session = new TerminalSession({
      id: "sid-1",
      onData: () => {},
      onExit: () => {},
      wsFactory: () => fake.socket,
      wsUrl: () => "ws://fake",
    });
    fake.open();
    session.dispose();
    expect(fake.closeCalls).toBe(1);
    expect(fake.socket.onopen).toBeNull();
    expect(fake.socket.onmessage).toBeNull();
    expect(fake.socket.onclose).toBeNull();
  });
});
