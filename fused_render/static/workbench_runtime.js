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
    // Validate first: routeFor throws synchronously, as the local runtime
    // does. Resolving it after the supersede bookkeeping would let a bad path
    // abort a perfectly good in-flight request sharing its opts.key, leave
    // that key's record in `inflight` forever (cleanup never runs), and never
    // detach the abort listener from a reused long-lived signal.
    var url = routeUrl(routeFor(path));
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
    return fetch(url, {
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
  // A real gesture in this document, tracked exactly as the local runtime
  // tracks it (capture phase, sticky): a param the page writes for itself
  // before any interaction is part of the as-loaded state, not a step the
  // user can press Back out of.
  var sawGesture = false;
  function markGesture() { sawGesture = true; }
  ["pointerdown", "keydown"].forEach(function (type) {
    document.addEventListener(type, markGesture, true);
  });

  // The local runtime's set() contract, ported whole (fused_render/static/
  // runtime.js). It has to be the same contract: a page is authored against
  // the local one and then deployed, and every divergence here is a bug the
  // author cannot see until it is live. That means the loud validation as
  // well as the behaviour — silently accepting `{history: "replce"}` would
  // hand back the push it was passed to avoid.
  //
  // What is deliberately NOT ported is the coalescing/rAF budget around the
  // history write: it is a scrub-performance device, not a semantic, and the
  // entry it produces is the same one this immediate write produces.
  function setParam(key, value, options) {
    if (isReserved(key)) {
      throw new Error(
        "fused.params.set: '" + key + "' is a reserved param name and cannot be set"
      );
    }
    var removing = value === null;
    if (!removing && typeof value !== "string") {
      throw new Error(
        "fused.params.set: value for '" + key + "' must be a string or null, got " + typeof value
      );
    }
    var opts = options || {};
    if (opts.history !== undefined && opts.history !== "replace") {
      throw new Error(
        'fused.params.set: options.history must be "replace", got ' + JSON.stringify(opts.history)
      );
    }
    if (opts.default !== undefined && typeof opts.default !== "string") {
      throw new Error(
        "fused.params.set: options.default for '" + key + "' must be a string, got " +
          typeof opts.default
      );
    }
    if (removing && opts.default !== undefined) {
      throw new Error(
        "fused.params.set: options.default is meaningless when removing '" + key + "'"
      );
    }

    var params = currentParams();
    if (removing) params.delete(key);
    else params.set(key, value);
    var query = params.toString();
    var newSearch = query ? "?" + query : "";
    // Two ways a write is a no-op: the URL already says this byte-for-byte,
    // or (opts.default) it already MEANS this by saying nothing.
    var meansDefault =
      opts.default !== undefined && value === opts.default && getParam(key) === undefined;
    var unchanged = meansDefault || newSearch === window.location.search;
    if (!unchanged) {
      var prevState = history.state;
      var newUrl = window.location.pathname + newSearch;
      if (opts.history === "replace" || !sawGesture || (prevState && prevState.fusedParamEntry)) {
        history.replaceState(prevState, "", newUrl);
      } else {
        // The once-per-visit push: the first user-caused write gets one entry
        // so Back restores the as-loaded state; every later write replaces on
        // top of it, so param churn costs at most one entry per visit.
        var nextState = Object.assign({}, prevState, { fusedParamEntry: true });
        try {
          history.pushState(nextState, "", newUrl);
        } catch (error) {
          console.warn("[fused] history write throttled:", error);
        }
      }
    }
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
