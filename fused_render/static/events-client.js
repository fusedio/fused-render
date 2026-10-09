// The events bus client: ONE WebSocket per document, every live fact a
// subscription, no timer in here ever fetches (D3, D8, D12).
//
// Shared by both worlds, which is why it is plain JS with no imports: the
// React shell imports it through the `@static/events-client.js` alias (typed
// by the sibling .d.ts), and runtime.js loads it with a <script> tag ahead of
// its own body. There is no second copy to keep in step.
//
//   fusedEvents.subscribe(topic, params, cb, opts) -> unsubscribe
//     cb(snapshot, null, meta)   a full body (the GET's), on subscribe and on
//                                every change of a topic without deltas
//     cb(null, delta, meta)      a delta, for topics that offer them
//     cb(null, null, {error, status})  the GET's refusal
//     meta.gen                   the generation the frame stands for
//
//   Refcounted by (topic, canonical params): twelve chat cards subscribing to
//   the same listing hold ONE server subscription and share one cached
//   snapshot — a late subscriber is replayed the cached snapshot synchronously.
//
//   Hidden policy (D7): on `visibilitychange → hidden` every subscription whose
//   topic is `hidden_ok` (the server's catalog, or `opts.hiddenOk`) is
//   unsubscribed; on visible it is resubscribed and the snapshot that answers
//   is the catch-up. Topics that must fire unseen are never dropped.
//
//   Socket or nothing (D8): a refused or dropped socket redials with backoff
//   (1 s → 30 s); subscribers keep the last snapshot and get a fresh one on
//   resubscribe. Server pings every 15 s; if nothing at all arrives for 45 s the
//   socket is closed and redialled, so a half-open WKWebView socket after sleep
//   cannot leave a window stale. A `hello` with a new `boot_id` means the
//   server restarted: every generation is forgotten and everything resubscribes.
(function (root) {
  "use strict";

  var PATH = "/api/events";
  var RETRY_MIN_MS = 1000;
  var RETRY_MAX_MS = 30000;
  var SILENCE_MS = 45000;

  var subs = new Map(); // key -> entry
  var byId = new Map(); // wire id -> entry
  var sock = null;
  var opened = false; // the current socket said hello
  var nextId = 0;
  var bootId = null;
  var catalog = {};
  var retryMs = 0;
  var retryTimer = null;
  var watchdog = null;
  var hidden = false;
  var helloListeners = new Set();
  var stateListeners = new Set();
  var deps = {
    WebSocket: typeof WebSocket === "function" ? WebSocket : null,
    location: typeof location !== "undefined" ? location : null,
    document: typeof document !== "undefined" ? document : null,
    setTimeout: function (fn, ms) { return setTimeout(fn, ms); },
    clearTimeout: function (h) { clearTimeout(h); },
  };

  function canonical(params) {
    if (!params || typeof params !== "object") return "{}";
    var keys = Object.keys(params).filter(function (k) { return params[k] !== undefined; }).sort();
    var out = {};
    keys.forEach(function (k) { out[k] = params[k]; });
    return JSON.stringify(out);
  }

  function usable() {
    var loc = deps.location;
    return !!deps.WebSocket && !!loc && !!loc.host && (loc.protocol === "http:" || loc.protocol === "https:");
  }

  function url() {
    var loc = deps.location;
    return (loc.protocol === "https:" ? "wss://" : "ws://") + loc.host + PATH;
  }

  function emitState() {
    stateListeners.forEach(function (fn) {
      try { fn(opened); } catch (e) { /* a listener's own bug */ }
    });
  }

  function armWatchdog() {
    deps.clearTimeout(watchdog);
    watchdog = deps.setTimeout(function () {
      // Nothing — frame or ping — for 45 s: the socket is half-open.
      if (sock) {
        var s = sock;
        sock = null;
        try { s.close(); } catch (e) { /* already closing */ }
        onClosed(s);
      }
    }, SILENCE_MS);
  }

  function send(obj) {
    if (!sock || sock.readyState !== 1 || !opened) return false;
    try {
      sock.send(JSON.stringify(obj));
      return true;
    } catch (e) {
      return false;
    }
  }

  function wantsWire(entry) {
    if (entry.cbs.size === 0) return false;
    if (hidden && hiddenOk(entry)) return false;
    return true;
  }

  function hiddenOk(entry) {
    if (typeof entry.hiddenOk === "boolean") return entry.hiddenOk;
    var meta = catalog[entry.topic];
    return meta ? !!meta.hidden_ok : false;
  }

  function putOnWire(entry) {
    if (entry.id !== null) return;
    entry.id = ++nextId;
    byId.set(entry.id, entry);
    var msg = { t: "sub", id: entry.id, topic: entry.topic, params: entry.params };
    if (typeof entry.gen === "number") msg.since = entry.gen;
    send(msg);
  }

  function takeOffWire(entry) {
    if (entry.id === null) return;
    byId.delete(entry.id);
    send({ t: "unsub", id: entry.id });
    entry.id = null;
  }

  function syncWire() {
    if (!opened) return;
    subs.forEach(function (entry) {
      if (wantsWire(entry)) putOnWire(entry);
      else takeOffWire(entry);
    });
  }

  function deliver(entry, snap, delta, meta) {
    entry.cbs.forEach(function (cb) {
      try {
        cb(snap, delta, meta);
      } catch (e) {
        if (typeof console !== "undefined" && console.error) console.error("[fusedEvents] a subscriber threw:", e);
      }
    });
  }

  function onFrame(msg) {
    armWatchdog();
    var t = msg.t;
    if (t === "ping") return;
    if (t === "hello") {
      var restarted = bootId !== null && msg.boot_id !== bootId;
      bootId = msg.boot_id || null;
      catalog = msg.topics && typeof msg.topics === "object" ? msg.topics : {};
      opened = true;
      retryMs = 0;
      // Everything resubscribes on every hello: a fresh socket has no
      // subscriptions, and a restarted server has no generations either.
      byId.clear();
      subs.forEach(function (entry) {
        entry.id = null;
        if (restarted) entry.gen = null;
      });
      syncWire();
      helloListeners.forEach(function (fn) {
        try { fn(msg, restarted); } catch (e) { /* a listener's own bug */ }
      });
      emitState();
      return;
    }
    var entry = byId.get(msg.id);
    if (!entry) return; // unsubscribed while the server was answering
    if (t === "snap") {
      if (typeof msg.gen === "number") entry.gen = msg.gen;
      entry.snap = msg.body;
      entry.loaded = true;
      deliver(entry, msg.body, null, { gen: entry.gen });
    } else if (t === "delta") {
      if (typeof msg.gen === "number") entry.gen = msg.gen;
      deliver(entry, null, msg.body, { gen: entry.gen });
    } else if (t === "err") {
      deliver(entry, null, null, { error: msg.error || "refused", status: msg.status || 500 });
    }
  }

  function onClosed(s) {
    if (sock === s) sock = null;
    var wasOpen = opened;
    opened = false;
    byId.clear();
    subs.forEach(function (entry) { entry.id = null; });
    deps.clearTimeout(watchdog);
    watchdog = null;
    if (wasOpen) emitState();
    scheduleRedial();
  }

  function scheduleRedial() {
    if (retryTimer !== null || subs.size === 0) return;
    retryMs = Math.min(RETRY_MAX_MS, retryMs ? retryMs * 2 : RETRY_MIN_MS);
    retryTimer = deps.setTimeout(function () {
      retryTimer = null;
      connect();
    }, retryMs);
  }

  function connect() {
    if (sock || subs.size === 0 || !usable()) return;
    var s;
    try {
      s = new deps.WebSocket(url());
    } catch (e) {
      scheduleRedial();
      return;
    }
    sock = s;
    s.onopen = function () {
      // Nothing is sent before `hello`: the server's catalog decides the
      // hidden policy, and a restarted server must be noticed first.
      armWatchdog();
    };
    s.onmessage = function (ev) {
      if (sock !== s) return;
      var msg;
      try {
        msg = JSON.parse(String(ev.data));
      } catch (e) {
        return;
      }
      if (msg && typeof msg === "object") onFrame(msg);
    };
    s.onerror = function () { /* always followed by close */ };
    s.onclose = function () {
      if (sock !== s && !opened) return;
      onClosed(s);
    };
  }

  function disconnectIfIdle() {
    if (subs.size > 0 || !sock) return;
    var s = sock;
    sock = null;
    opened = false;
    byId.clear();
    deps.clearTimeout(watchdog);
    watchdog = null;
    if (retryTimer !== null) {
      deps.clearTimeout(retryTimer);
      retryTimer = null;
    }
    try { s.close(); } catch (e) { /* already closing */ }
  }

  /**
   * Follow one topic. `cb(snapshot, delta, meta)`; returns the unsubscribe.
   * `opts.hiddenOk` overrides the server's hidden policy for this entry.
   */
  function subscribe(topic, params, cb, opts) {
    if (typeof cb !== "function") return function () {};
    var p = params && typeof params === "object" ? params : {};
    var key = topic + "\u0000" + canonical(p);
    var entry = subs.get(key);
    if (!entry) {
      entry = { topic: topic, params: p, key: key, cbs: new Set(), id: null, gen: null, snap: null, loaded: false, hiddenOk: undefined };
      subs.set(key, entry);
    }
    if (opts && typeof opts.hiddenOk === "boolean") entry.hiddenOk = opts.hiddenOk;
    entry.cbs.add(cb);
    if (entry.loaded) {
      // The replay: a card mounted five minutes in must not wear a skeleton
      // until something happens to change.
      try {
        cb(entry.snap, null, { gen: entry.gen, replay: true });
      } catch (e) {
        if (typeof console !== "undefined" && console.error) console.error("[fusedEvents] a subscriber threw:", e);
      }
    }
    if (!sock) connect();
    else syncWire();
    var on = true;
    return function () {
      if (!on) return;
      on = false;
      entry.cbs.delete(cb);
      if (entry.cbs.size === 0) {
        takeOffWire(entry);
        subs.delete(key);
        disconnectIfIdle();
      }
    };
  }

  /** Ask for a fresh snapshot of a key now (a local event said something moved
   *  that the producer may take a beat to notice). Not a timer: an event. */
  function resync(topic, params) {
    var entry = subs.get(topic + "\u0000" + canonical(params));
    if (!entry || !opened) return false;
    takeOffWire(entry);
    if (wantsWire(entry)) putOnWire(entry);
    return true;
  }

  /** The cached snapshot of a key, or null. */
  function snapshot(topic, params) {
    var entry = subs.get(topic + "\u0000" + canonical(params));
    return entry && entry.loaded ? entry.snap : null;
  }

  function onHello(fn) {
    helloListeners.add(fn);
    return function () { helloListeners.delete(fn); };
  }

  function onState(fn) {
    stateListeners.add(fn);
    return function () { stateListeners.delete(fn); };
  }

  function setHidden(next) {
    if (hidden === !!next) return;
    hidden = !!next;
    syncWire();
  }

  if (deps.document && typeof deps.document.addEventListener === "function") {
    hidden = !!deps.document.hidden;
    deps.document.addEventListener("visibilitychange", function () {
      setHidden(!!deps.document.hidden);
    });
  }

  /** Test seam: swap the browser pieces and forget every subscription. */
  function _reset(override) {
    subs.forEach(function (entry) { entry.cbs.clear(); });
    subs.clear();
    byId.clear();
    if (sock) {
      var s = sock;
      sock = null;
      try { s.close(); } catch (e) { /* ignore */ }
    }
    opened = false;
    bootId = null;
    catalog = {};
    retryMs = 0;
    hidden = false;
    deps.clearTimeout(retryTimer);
    retryTimer = null;
    deps.clearTimeout(watchdog);
    watchdog = null;
    nextId = 0;
    if (override) Object.keys(override).forEach(function (k) { deps[k] = override[k]; });
  }

  root.fusedEvents = {
    subscribe: subscribe,
    resync: resync,
    snapshot: snapshot,
    onHello: onHello,
    onState: onState,
    connected: function () { return opened; },
    bootId: function () { return bootId; },
    setHidden: setHidden,
    _reset: _reset,
    _subscriptions: function () { return subs.size; },
    _wire: function () { return byId.size; },
  };
})(typeof window !== "undefined" ? window : globalThis);
