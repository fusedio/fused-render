/* Runtime injected into fused-render apps compiled for a shared Fused Canvas. */
(function () {
  "use strict";

  var cfg = window.__FUSED_RENDER_WORKBENCH__ || {
    shell: "fr_shell",
    entrypoints: {},
    assets: {},
    assetRoute: null,
    cacheMaxAge: 0,
  };

  function own(obj, key) {
    return obj && Object.prototype.hasOwnProperty.call(obj, key) ? obj[key] : undefined;
  }

  function base() {
    var parts = window.location.pathname.split("/").filter(Boolean);
    if (parts.length && parts[parts.length - 1] === cfg.shell) parts.pop();
    return "/" + parts.join("/");
  }

  function authQuery() {
    var out = new URLSearchParams();
    var current = new URLSearchParams(window.location.search);
    var session = current.get("fused_session_token");
    if (session) out.set("fused_session_token", session);
    if (cfg.cacheMaxAge > 0) out.set("cache_max_age", String(cfg.cacheMaxAge));
    return out;
  }

  function routeUrl(route, extra) {
    var query = authQuery();
    if (extra) {
      Object.keys(extra).forEach(function (key) {
        query.set(key, extra[key]);
      });
    }
    var text = query.toString();
    return base() + "/" + route + (text ? "?" + text : "");
  }

  function routeFor(path) {
    var route = own(cfg.entrypoints, path);
    if (!route) {
      throw new Error(
        "fused.runPython: no deployed route for " + JSON.stringify(path) +
          " (the path must be a literal known when the app is deployed)"
      );
    }
    return route;
  }

  function normalize(path) {
    var out = [];
    String(path).split("/").forEach(function (part) {
      if (!part || part === ".") return;
      if (part === "..") {
        if (out.length && out[out.length - 1] !== "..") out.pop();
        else out.push(part);
      } else out.push(part);
    });
    return out.join("/") || ".";
  }

  var assetKeys = null;
  function assetKeyFor(path) {
    var exact = own(cfg.assets, path);
    if (exact) return exact;
    if (assetKeys === null) {
      assetKeys = Object.create(null);
      Object.keys(cfg.assets || {}).forEach(function (key) {
        assetKeys[cfg.assets[key]] = true;
      });
    }
    var key = normalize(path);
    if (assetKeys[key]) return key;
    throw new Error(
      "fused.rawUrl: no deployed asset for " + JSON.stringify(path) +
        " (include it in the app's fused bundle manifest)"
    );
  }

  var inflight = new Map();
  function runPython(path, params, opts) {
    opts = opts || {};
    var channel = opts.key === undefined ? path : opts.key;
    var keyed = channel !== null;
    var controller = new AbortController();
    if (keyed) {
      var prior = inflight.get(channel);
      if (prior) {
        prior.superseded = true;
        prior.controller.abort();
      }
      inflight.set(channel, { controller: controller, superseded: false });
    }
    var record = keyed ? inflight.get(channel) : { controller: controller, superseded: false };
    var detach = null;
    if (opts.signal) {
      if (opts.signal.aborted) controller.abort();
      else {
        var onAbort = function () { controller.abort(); };
        opts.signal.addEventListener("abort", onAbort);
        detach = function () { opts.signal.removeEventListener("abort", onAbort); };
      }
    }
    function cleanup() {
      if (detach) detach();
      if (keyed && inflight.get(channel) === record) inflight.delete(channel);
    }
    return fetch(routeUrl(routeFor(path)), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params || {}),
      signal: controller.signal,
    }).then(function (response) {
      if (!response.ok) {
        var error = new Error(
          "runPython(" + JSON.stringify(path) + ") failed: HTTP " + response.status
        );
        error.status = response.status;
        throw error;
      }
      var type = response.headers.get("content-type") || "";
      return type.indexOf("application/json") >= 0 ? response.json() : response.text();
    }).then(function (result) {
      cleanup();
      if (record.superseded) return new Promise(function () {});
      return result;
    }, function (error) {
      cleanup();
      if (opts.signal && opts.signal.aborted) throw error;
      if (record.superseded) return new Promise(function () {});
      throw error;
    });
  }

  function rawUrl(path) {
    if (!cfg.assetRoute) throw new Error("fused.rawUrl: this app has no deployed assets");
    return routeUrl(cfg.assetRoute, { name: assetKeyFor(path) });
  }

  function readFile(path) {
    return fetch(rawUrl(path)).then(function (response) {
      if (!response.ok) {
        throw new Error("readFile(" + JSON.stringify(path) + ") failed: HTTP " + response.status);
      }
      return response.text();
    });
  }

  function unsupported(name) {
    return function () {
      throw new Error(
        "fused." + name + "() is unavailable in a Workbench-deployed app; " +
          "the hosted Canvas has no writable local filesystem"
      );
    };
  }

  function trackJob() {
    return {
      id: null,
      state: "running",
      cancelRequested: false,
      update: function () { return Promise.resolve(null); },
      finish: function () { return Promise.resolve(null); },
      fail: function () { return Promise.resolve(null); },
      cancelled: function () { return Promise.resolve(null); },
    };
  }

  var listeners = new Set();
  var snapshot = "";
  function isReserved(key) {
    return key.charAt(0) === "_" || key === "fused_session_token" || key === "cache_max_age";
  }
  function currentParams() { return new URLSearchParams(window.location.search); }
  function getParam(key) {
    if (isReserved(key)) return undefined;
    var params = currentParams();
    return params.has(key) ? params.get(key) : undefined;
  }
  function allParams() {
    var out = {};
    currentParams().forEach(function (value, key) {
      if (!isReserved(key)) out[key] = value;
    });
    return out;
  }
  function notify() {
    var value = allParams();
    var next = JSON.stringify(value);
    if (next === snapshot) return;
    snapshot = next;
    listeners.forEach(function (listener) {
      try { listener(value); } catch (error) { console.error("[fused] params listener", error); }
    });
  }
  function setParam(key, value) {
    if (isReserved(key)) throw new Error("fused.params.set: reserved key " + JSON.stringify(key));
    var params = currentParams();
    if (value === null || value === undefined) params.delete(key);
    else if (typeof value === "string") params.set(key, value);
    else throw new Error("fused.params.set: values must be strings or null");
    var query = params.toString();
    history.replaceState(history.state, "", location.pathname + (query ? "?" + query : ""));
    notify();
  }
  function onChange(listener) {
    listeners.add(listener);
    return function () { listeners.delete(listener); };
  }
  snapshot = JSON.stringify(allParams());
  window.addEventListener("popstate", notify);
  window.addEventListener("unhandledrejection", function (event) {
    if (event.reason && event.reason.name === "AbortError") event.preventDefault();
  });

  window.fused = {
    env: "hosted",
    runPython: runPython,
    rawUrl: rawUrl,
    readFile: readFile,
    writeFile: unsupported("writeFile"),
    stat: unsupported("stat"),
    trackJob: trackJob,
    autoReload: function () {},
    params: { get: getParam, getAll: allParams, set: setParam, onChange: onChange },
  };
})();
