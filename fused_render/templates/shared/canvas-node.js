// What the three canvas node templates share (SPEC §53.2): canvas_widget,
// canvas_note and canvas_udf each draw ONE node file of a cloned canvas, and
// each needs the same few facts and calls first. Logic only: no markup, no
// styles (each template paints its own strip from its own palette).
//
//   CanvasNode.info(file)       what this file is (GET /api/canvases/node —
//                               the server parses the path; a page never does)
//   CanvasNode.workbench(env)   where that environment's workbench is framed
//                               from, and its real origin
//   CanvasNode.token(env)       the environment's `fused login` bearer
//   CanvasNode.run(file)        one run of a UDF node's file
//   CanvasNode.sync(node, on)   keep the canvas's sync engine running while
//                               this page is open, and hear how it stands
//   CanvasNode.openCanvas(info) the canvas page on this node
//   CanvasNode.noteText(text)   a note file's text without its header lines
(function () {
  "use strict";

  // The app's guard for calls that execute or hand out a credential (D36): a
  // custom header forces a CORS preflight, so no other origin can make them.
  var GUARD = { "X-Fused": "1" };

  async function getJson(url, init) {
    var res = await fetch(url, init);
    var data = null;
    try {
      data = await res.json();
    } catch (_) {
      data = null;
    }
    if (!res.ok) {
      var message = data && typeof data.error === "string" ? data.error : "HTTP " + res.status;
      var err = new Error(message);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function info(file) {
    return getJson("/api/canvases/node?path=" + encodeURIComponent(file));
  }

  // `{base, origin, publicOrigin}`: `base` is what the frame loads from (the
  // custom environment's loopback proxy when that is on, else the workbench
  // itself), `origin` the origin messages to the frame are posted at, and
  // `publicOrigin` the workbench's real one.
  async function workbench(env) {
    var status = await getJson("/api/canvases/status?env=" + encodeURIComponent(env));
    var entry = status && status.envs ? status.envs[env] : null;
    if (!entry || typeof entry.base_url !== "string") {
      throw new Error("this Fused environment has no workbench address");
    }
    var base = typeof entry.embed_url === "string" && entry.embed_url ? entry.embed_url : entry.base_url;
    return {
      base: base.replace(/\/+$/, ""),
      origin: new URL(base).origin,
      publicOrigin: new URL(entry.base_url).origin,
    };
  }

  async function token(env) {
    var data = await getJson("/api/canvases/token?env=" + encodeURIComponent(env), { headers: GUARD });
    if (!data || typeof data.access_token !== "string" || !data.access_token) {
      throw new Error("no Fused access token — sign in again");
    }
    return data.access_token;
  }

  function run(file) {
    return getJson("/api/canvases/node/run", {
      method: "POST",
      headers: Object.assign({ "Content-Type": "application/json" }, GUARD),
      body: JSON.stringify({ path: file }),
    });
  }

  function canvasHref(node) {
    var q = new URLSearchParams();
    q.set("node", node.node);
    q.set("node_file", node.file);
    return "/canvases/" + encodeURIComponent(node.env) + "/" + encodeURIComponent(node.name) + "?" + q.toString();
  }

  // The whole window goes there: a template page is a frame of the shell.
  function navigate(href) {
    try {
      if (window.top && window.top !== window) {
        window.top.location.assign(href);
        return;
      }
    } catch (_) {
      // A cross-origin host: move this document instead.
    }
    window.location.assign(href);
  }

  function openCanvas(node) {
    navigate(canvasHref(node));
  }

  function openCanvases() {
    navigate("/canvases");
  }

  // A note's file opens with the workbench's own header lines
  // (`<!--fused:…-->`): a pin line ends at its newline, any other header at
  // the first blank line. The note's text is what follows.
  function noteText(text) {
    var out = typeof text === "string" ? text : "";
    for (;;) {
      if (out.startsWith("<!--fused:pin=")) {
        var nl = out.indexOf("\n");
        out = nl < 0 ? "" : out.slice(nl + 1);
        continue;
      }
      if (out.startsWith("<!--fused:")) {
        var gap = out.indexOf("\n\n");
        out = gap < 0 ? "" : out.slice(gap + 2);
        continue;
      }
      break;
    }
    return out.endsWith("\n") ? out.slice(0, -1) : out;
  }

  // BACKGROUND SYNC (SPEC §53.2 NT-7). While a node's page is open the
  // canvas's sync engine runs, so an edit to any file of the folder (this
  // one, an upstream node, canvas.toml) is pushed to Fused as it is made —
  // exactly as it is while the canvas page is open. The page does not draw
  // FROM the engine: it holds no frame record and takes no deltas. It only
  // (1) starts the watcher, (2) keeps the events socket open, which is what
  // keeps the engine from idling out, and (3) reads three facts off the
  // feed: the line for its strip, a finished push, a change on disk.
  //
  //   on.status(text, tone)   the strip's line; tone "" | "busy" | "error"
  //   on.pushed(pushSeq)      a push finished after this page opened
  //   on.changed(diskRev)     a file of the clone changed on disk
  //
  // Not signed in, or a reference copy (not a clone this app may sync):
  // nothing is started and `on.status` says why. Returns the stop function.
  var START_RETRY_MS = 5000;
  var RECONNECT_MS = 2000;

  function syncLine(state) {
    var legs = (state && state.legs) || {};
    var up = legs.disk_to_server || {};
    var down = legs.server_to_disk || {};
    if (up.state === "error") return ["Not saved to Fused" + (up.reason ? ": " + up.reason : ""), "error"];
    if (down.state === "stale" || down.state === "error") {
      return ["Fused has newer changes \u2014 open the canvas to pull", "error"];
    }
    if (down.state === "leased") return [down.reason || "Open elsewhere \u2014 not saving from here", "error"];
    if (up.state === "pushing" || up.state === "pending") return ["Saving to Fused\u2026", "busy"];
    if (down.state === "pulling") return ["Pulling from Fused\u2026", "busy"];
    if (up.state === "held") return [up.reason || "Waiting to save", "busy"];
    return [up.last_push_at ? "Saved to Fused" : "Synced", ""];
  }

  function sync(node, on) {
    on = on || {};
    var status = function (text, tone) { if (on.status) on.status(text, tone || ""); };
    if (!node || !node.signed_in) {
      status("", "");
      return function () {};
    }
    if (!node.live) {
      status("Reference copy \u2014 not syncing", "");
      return function () {};
    }
    var stopped = false;
    var ws = null;
    var startTimer = null;
    var reconnectTimer = null;
    var lastStart = 0;
    var owned = false;
    var pushSeq = null;      // null until the first state: only LATER pushes count
    var diskRev = null;
    var epoch = null;

    function start() {
      clearTimeout(startTimer);
      if (stopped) return;
      lastStart = Date.now();
      fetch("/api/canvases/sync/start", {
        method: "POST",
        headers: Object.assign({ "Content-Type": "application/json" }, GUARD),
        body: JSON.stringify({ env: node.env, name: node.name }),
      }).then(function (res) {
        return res.json().catch(function () { return null; }).then(function (data) {
          if (stopped) return;
          if (res.ok) {
            owned = false;
            return;
          }
          // Another fused-render process syncs this clone: it pushes, this
          // page says so and asks again later (it may quit).
          owned = !!(data && data.code === "owned");
          status(owned ? "Synced by another Fused Render window"
            : "Not syncing: " + ((data && data.error) || "HTTP " + res.status), owned ? "" : "error");
          startTimer = setTimeout(start, START_RETRY_MS);
        });
      }, function (error) {
        if (stopped) return;
        status("Not syncing: " + error.message, "error");
        startTimer = setTimeout(start, START_RETRY_MS);
      });
    }

    function onState(state) {
      if (!state || typeof state !== "object") return;
      // No watcher here (it idled out, the server restarted): start it again,
      // at most once per retry interval.
      if (state.watching === false || state.watcher_epoch == null) {
        if (!owned && Date.now() - lastStart >= START_RETRY_MS) start();
        return;
      }
      // A new watcher numbers pushes and revisions from its own start.
      if (state.watcher_epoch !== epoch) {
        epoch = state.watcher_epoch;
        pushSeq = null;
        diskRev = null;
      }
      var seq = state.legs && state.legs.disk_to_server ? state.legs.disk_to_server.push_seq : null;
      if (typeof seq === "number") {
        if (pushSeq !== null && seq > pushSeq && on.pushed) on.pushed(seq);
        pushSeq = seq;
      }
      if (typeof state.disk_rev === "number") {
        if (diskRev !== null && state.disk_rev !== diskRev && on.changed) on.changed(state.disk_rev);
        diskRev = state.disk_rev;
      }
      var line = syncLine(state);
      status(line[0], line[1]);
    }

    function connect() {
      clearTimeout(reconnectTimer);
      if (stopped) return;
      var url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host +
        "/api/canvases/sync/events?name=" + encodeURIComponent(node.name) +
        "&env=" + encodeURIComponent(node.env);
      try {
        ws = new WebSocket(url);
      } catch (_) {
        reconnectTimer = setTimeout(connect, RECONNECT_MS);
        return;
      }
      ws.onmessage = function (event) {
        var msg = null;
        try {
          msg = JSON.parse(event.data);
        } catch (_) {
          return;
        }
        if (msg && msg.type === "state") onState(msg.state);
      };
      ws.onclose = function () {
        ws = null;
        if (!stopped) reconnectTimer = setTimeout(connect, RECONNECT_MS);
      };
    }

    start();
    connect();

    function stop() {
      stopped = true;
      clearTimeout(startTimer);
      clearTimeout(reconnectTimer);
      if (ws) {
        try { ws.close(); } catch (_) { /* already closed */ }
      }
    }
    // The socket is what holds the engine up; let it go with the page.
    window.addEventListener("pagehide", stop);
    return stop;
  }

  window.CanvasNode = {
    info: info,
    workbench: workbench,
    token: token,
    run: run,
    sync: sync,
    canvasHref: canvasHref,
    openCanvas: openCanvas,
    openCanvases: openCanvases,
    noteText: noteText,
  };
})();
