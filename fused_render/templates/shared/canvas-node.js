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

  window.CanvasNode = {
    info: info,
    workbench: workbench,
    token: token,
    run: run,
    canvasHref: canvasHref,
    openCanvas: openCanvas,
    openCanvases: openCanvases,
    noteText: noteText,
  };
})();
