/*
 * Injected into every rendered HTML file (see server.py `/render`).
 * Provides `window.fused`:
 *   fused.runPython(pyPath, params, opts?) -> Promise<result>
 *     Stale-request cancellation is ON by default (D114): a new call for a .py
 *     supersedes (aborts) the prior in-flight call for that same file — cancels
 *     stale slider scrubs with no author effort. opts.key regroups the channel;
 *     opts.key:null opts out (fully concurrent); opts.signal is a caller
 *     AbortSignal that composes.
 *   fused.ai(prompt, opts?) -> Promise<{text, model, usage}>
 *     Ask an AI model via the shell's /api/ai, which runs the local claude
 *     (Claude Code) CLI. Resolves with exactly {text: string, model: full model
 *     id that ran, usage: {input_tokens, output_tokens} | null} — Anthropic-style
 *     usage names, NOT OpenAI's prompt_tokens/completion_tokens. opts:
 *     systemPrompt, model, effort ("low"|"medium"|"high"|"xhigh"),
 *     onChunk. Local-only — not available on hosted/exported pages.
 *   fused.params.get(key) / getAll() / set(key, value) / onChange(cb) -> unsubscribe
 *   fused.env -> "local" — the runtime identity. This is the local fused-render app;
 *                the hosted/exported runtime (fused wheel) sets "hosted" instead, so a
 *                page can branch on where it runs and gate any local-only behaviour
 *                when deployed. See docs/EXPORT.md.
 *
 * `window.fused` is the DOCUMENTED public bridge (docs/EXPORT.md's portable-subset
 * table) — every user-authored template writes against it, so anything added here
 * is a contract kept forever, mirrored by the separate hosted stub (the `fused`
 * wheel's own copy of this file). The per-file sidecar's location mapping
 * (window._fusedSidecarPath / window._fusedTargetPathFromSidecarPath, below) is
 * deliberately NOT on `fused`: it is app-internal bookkeeping — the exact JSON file
 * five other built-in features read-merge-write (claudeSessions/bookmarkHistory/
 * comments/...) — not a general-purpose "store a file next to mine" feature, and a
 * third-party template calling writeFile on it directly would clobber that file
 * wholesale instead of merging. Built-in templates reach it as a plain global
 * because they run in the same window this script is injected into; it is not
 * present in the hosted runtime and is not meant to be.
 *
 * Same-origin iframe model: this script talks to an ancestor window's URL
 * directly (no postMessage bridge — see DECISIONS.md D3/D4). The param target
 * is the TOPMOST same-origin ancestor (D46), stopping BELOW any ancestor
 * marked as a param boundary (`_fusedParamBoundary` — both layout shells set
 * one, LM-3/TM-3/D72): it climbs window.parent while the next ancestor is
 * same-origin, reachable, and not a boundary. In normal view/embed mode the
 * direct parent is already the top, so this is unchanged; inside a layout mode
 * (panel or tab) the shell is a boundary, so the climb stops at each pane's/
 * tab's own embed shell — params stay pane-local, captured segment-local
 * inside `_layout` by the shell's ordinary URL sync.
 *
 * Global params still exist but only by hand (D72): top-level params a user
 * types on a layout shell URL are READABLE from every pane (get/getAll merge
 * the same-origin ancestor chain above the boundary; nearer wins, pane-local
 * wins over all), but set() never writes them — writes always land on the
 * pane's own URL. When loaded as a top-level
 * page (target === window, e.g. visiting /render?path=... directly, or a
 * cross-origin ancestor) it falls back to reading/writing its own URL, treating
 * the `path` query key as reserved alongside any `_`-prefixed key.
 *
 * It also carries the appearance theme into OPTED-IN view documents — see the
 * theme block at the top of the IIFE (SPEC §30, D134).
 */
(function () {
  "use strict";

  // --- Appearance (SPEC §30, D134) -----------------------------------------
  //
  // A built-in template that authored a light palette marks its <html> with
  // `data-fused-theme`; this then keeps `data-theme` on that same element in
  // step with the shell's appearance setting, which is all its
  // `:root[data-theme="light"]` block needs. Everything else — every
  // user-authored .html view, and every built-in view not yet converted — is
  // left completely alone: no attribute, no signal, CSS stays theirs.
  //
  // The opt-in has to be an ATTRIBUTE ON <html>, not a <meta>: this script is
  // injected at the very top of <head> (server.py `/render`) and is
  // parser-blocking, so it runs before anything further down the head has been
  // parsed. That ordering is also why there is no flash — the attribute lands
  // before the document's own stylesheet, let alone its first paint.
  //
  // The theme is READ here rather than pushed in from the shell: reading the
  // same localStorage key (same origin) plus this document's own matchMedia
  // means the shell never has to reach into a view, so a theme change is never
  // a re-render and can never remount or reload a live iframe. Cross-window
  // convergence rides the `storage` event, which fires in every other
  // same-origin browsing context — including this iframe — when the shell
  // writes the key.
  //
  // Must stay in sync with frontend/src/lib/theme.ts and frontend/index.html;
  // tests/test_theme.py pins the three spellings of the key together.
  var THEME_KEY = "fused-render:theme";
  var DARK_QUERY = "(prefers-color-scheme: dark)";

  function resolvedTheme() {
    var pref = null;
    try {
      pref = localStorage.getItem(THEME_KEY);
    } catch (e) {
      /* private mode / blocked storage — fall through to the OS preference */
    }
    if (pref === "light" || pref === "dark") return pref;
    try {
      return window.matchMedia(DARK_QUERY).matches ? "dark" : "light";
    } catch (e) {
      return "dark"; // no matchMedia — keep today's appearance
    }
  }

  function startTheme() {
    var root = document.documentElement;
    if (!root || !root.hasAttribute("data-fused-theme")) return;
    var apply = function () {
      root.setAttribute("data-theme", resolvedTheme());
    };
    apply();
    // Another window changed the setting (a `clear()` reports key === null).
    window.addEventListener("storage", function (event) {
      if (event.key === null || event.key === THEME_KEY) apply();
    });
    // The OS flipped while the setting is System — including macOS's automatic
    // sunset switch. Harmless when the setting is pinned: apply() re-reads the
    // preference, which still wins.
    try {
      window.matchMedia(DARK_QUERY).addEventListener("change", apply);
    } catch (e) {
      /* no matchMedia — a pinned Light/Dark still works */
    }
  }

  startTheme();

  // Climb to the topmost same-origin ancestor (D46). Reading .location.href on
  // a cross-origin window throws, so a try/catch marks the boundary.
  function findTarget() {
    let t = window;
    try {
      while (t.parent && t.parent !== t) {
        // Probe: throws if t.parent is cross-origin — stop at the last
        // same-origin ancestor. Probe first so cross-origin catch semantics are
        // unchanged, then honor a param boundary (tab shell, TM-3): stop below
        // it so the page targets its own pane's URL, not the shared tab URL.
        void t.parent.location.href;
        if (t.parent._fusedParamBoundary) break;
        t = t.parent;
      }
    } catch (e) {
      /* hit a cross-origin ancestor; t is the topmost same-origin one */
    }
    return t;
  }

  const target = findTarget();
  const standalone = target === window;

  // Same-origin ancestors ABOVE the target, nearest first (non-empty only when
  // a param boundary stopped the climb). Their top-level queries hold
  // hand-typed global params (D72): read-only from here — set() never touches
  // them. Computed per read: an ancestor's URL changes over time.
  function ancestorWindows() {
    const out = [];
    let t = target;
    try {
      while (t.parent && t.parent !== t) {
        void t.parent.location.href; // throws when cross-origin
        t = t.parent;
        out.push(t);
      }
    } catch (e) {
      /* hit a cross-origin ancestor — chain ends */
    }
    return out;
  }

  function isReserved(key) {
    if (key.startsWith("_")) return true;
    if (standalone && key === "path") return true;
    return false;
  }

  // In layout mode the target URL carries the reserved `_layout` param, which
  // is parenthesized and may contain LITERAL `&` (D51 — see the shell's
  // layout-codec.js, whose balanced-paren scan this duplicates: the runtime is
  // injected standalone and imports nothing). Raw URLSearchParams would split
  // inside the parens and leak layout fragments as visible params, so every
  // read/write splits the search string first: `layoutSpan` is the raw
  // `_layout=(...)` span (preserved byte-for-byte, reinserted last on write —
  // never decoded here; only the layout shells parse it), `rest` is the
  // remainder. Literal parens in the span are structural and balanced by
  // construction (codec-escaped otherwise); an unbalanced span (truncated URL)
  // runs to end-of-string so it still can't pollute `rest`.
  function splitSearch(search) {
    const s = (search || "").replace(/^\?/, "");
    const m = /(^|&)_layout=\(/.exec(s);
    if (!m) return { layoutSpan: null, rest: s };
    const start = m.index + m[1].length;
    let i = start + "_layout=(".length;
    let depth = 1;
    while (i < s.length && depth > 0) {
      if (s[i] === "(") depth++;
      else if (s[i] === ")") depth--;
      i++;
    }
    return {
      layoutSpan: s.slice(start, i),
      rest: (s.slice(0, m.index) + s.slice(i)).replace(/^&|&$/g, ""),
    };
  }

  // ---- coalesced history writes (D99) ---------------------------------------
  // WebKit (Safari, and the WKWebView the menu-bar popover uses, §25) hard-
  // limits history.replaceState/pushState to 100 calls per 30 s — past that it
  // THROWS SecurityError, which would kill the caller mid-scrub. Chrome has no
  // such limit, so this only ever bit inside the popover. Params therefore
  // take effect immediately through a pending-search overlay (targetSearch()),
  // while the actual history write is rate-limited to one per
  // HISTORY_MIN_INTERVAL_MS with a trailing flush — a scrub burst costs ~75
  // writes/30 s, safely under the cap, and the URL still lands on the final
  // value.
  const HISTORY_MIN_INTERVAL_MS = 400;
  let pendingSearch = null; // what target.location.search WILL be after flush
  let pendingUrl = null;
  let historyTimer = null;
  let lastHistoryWrite = 0;

  function targetSearch() {
    return pendingSearch !== null ? pendingSearch : target.location.search;
  }

  function flushHistory() {
    historyTimer = null;
    if (pendingUrl === null) return;
    const url = pendingUrl;
    pendingUrl = null;
    pendingSearch = null;
    lastHistoryWrite = Date.now();
    try {
      target.history.replaceState(target.history.state, "", url);
    } catch (e) {
      // WebKit throttle hit anyway (e.g. another writer burned the budget).
      // The overlay already served readers; losing one URL write is benign.
      console.warn("[fused] history write throttled:", e);
    }
  }

  function currentParams() {
    return new URLSearchParams(splitSearch(targetSearch()).rest);
  }

  const listeners = new Set();

  // Only the visible (non-reserved) params matter to onChange; snapshotting
  // that lets notifyIfChanged() skip no-op fires and notification loops (D46).
  let lastSnapshot = null;

  function fire(snapshot) {
    for (const cb of listeners) {
      try {
        cb(snapshot);
      } catch (e) {
        console.error("[fused] params.onChange listener threw:", e);
      }
    }
  }

  // Fire onChange only when the visible param snapshot actually changed. This
  // is the single notification channel — set() and any ancestor URL change
  // both route through the fused:urlchange event, and the diff guard kills the
  // duplicate a self-set would otherwise produce.
  function notifyIfChanged() {
    const snapshot = getAll();
    const serialized = JSON.stringify(snapshot);
    if (serialized === lastSnapshot) return;
    lastSnapshot = serialized;
    fire(snapshot);
  }

  function get(key) {
    if (key === "_file") {
      // _file normally rides on this frame's own URL (set by the shell on the
      // iframe src). Fall back to the shell URL for manually-opened views like
      // /view/<template>.html?_file=<target>.
      const own = new URLSearchParams(window.location.search);
      if (own.has("_file")) return own.get("_file");
      const outer = currentParams();
      return outer.has("_file") ? outer.get("_file") : undefined;
    }
    if (isReserved(key)) return undefined;
    const params = currentParams();
    if (params.has(key)) return params.get(key);
    // Hand-typed global fallback (D72): nearest ancestor above the boundary
    // that carries the key wins.
    for (const win of ancestorWindows()) {
      const p = new URLSearchParams(splitSearch(win.location.search).rest);
      if (p.has(key)) return p.get(key);
    }
    return undefined;
  }

  function getAll() {
    const result = {};
    // Farthest ancestor first, then nearer, then the target's own params —
    // later writes overwrite, so pane-local wins over hand-typed globals (D72).
    const chain = ancestorWindows().reverse();
    chain.push(target);
    for (const win of chain) {
      const search = win === target ? targetSearch() : win.location.search;
      const params = new URLSearchParams(splitSearch(search).rest);
      for (const [key, value] of params) {
        if (isReserved(key)) continue;
        result[key] = value;
      }
    }
    const file = get("_file");
    if (file !== undefined) result._file = file;
    return result;
  }

  function set(key, value) {
    if (isReserved(key)) {
      throw new Error(`fused.params.set: '${key}' is a reserved param name and cannot be set`);
    }
    if (typeof value !== "string") {
      throw new Error(
        `fused.params.set: value for '${key}' must be a string, got ${typeof value}`
      );
    }
    const { layoutSpan, rest } = splitSearch(targetSearch());
    const params = new URLSearchParams(rest);
    params.set(key, value);
    // Rebuild with the raw `_layout=(...)` span untouched and LAST (D51): the
    // layout stays readable (no URLSearchParams.toString() percent-soup) and
    // the global/local boundary stays visually stable.
    let search = params.toString();
    if (layoutSpan) search += (search ? "&" : "") + layoutSpan;
    const newSearch = search ? "?" + search : "";
    const newUrl = target.location.pathname + newSearch;
    // First-change-push: the first param write on a pristine history entry
    // pushes a new entry (preserving the as-loaded state for Back), every
    // later write replaces on top of it — so param churn costs at most one
    // entry per visit. "Pristine" is tracked via a flag on history.state, not
    // a JS variable: the flag travels with the entry, so after Back to the
    // pristine entry the next write correctly pushes again (truncating the
    // old forward branch), and it survives reloads. Existing state (e.g. the
    // tab shell's fusedActiveTab) is merged, not clobbered.
    const prevState = target.history.state;
    const unchanged = newSearch === targetSearch();
    if (unchanged) {
      // Nothing to write; fall through to the notification below.
    } else if (prevState && prevState.fusedParamEntry) {
      // Replace-on-top writes are the scrub-hot path: coalesce them (D99).
      // Readers see the new value immediately via the overlay; the history
      // write happens now if the budget allows, else on the trailing timer.
      pendingSearch = newSearch;
      pendingUrl = newUrl;
      if (!historyTimer) {
        const wait = Math.max(
          0,
          HISTORY_MIN_INTERVAL_MS - (Date.now() - lastHistoryWrite)
        );
        if (wait === 0) flushHistory();
        else historyTimer = setTimeout(flushHistory, wait);
      }
    } else {
      // The once-per-visit push: immediate, so Back gets its entry even if
      // the page dies within the debounce window.
      const nextState = Object.assign({}, prevState, { fusedParamEntry: true });
      pendingSearch = null;
      pendingUrl = null;
      lastHistoryWrite = Date.now();
      try {
        target.history.pushState(nextState, "", newUrl);
      } catch (e) {
        console.warn("[fused] history write throttled:", e);
      }
    }
    // Notify via the event path only (no direct notify(), D46). When the shell
    // wrapper exists it also fires fused:urlchange on the history write — the
    // snapshot diff in notifyIfChanged() makes the duplicate harmless; this
    // explicit dispatch covers standalone /render pages that have no wrapper.
    target.dispatchEvent(new Event("fused:urlchange"));
  }

  function onChange(cb) {
    listeners.add(cb);
    return () => listeners.delete(cb);
  }

  // Baseline the snapshot at load so the first no-op fused:urlchange doesn't
  // fire, while a real set() (which changes a param) still does.
  lastSnapshot = JSON.stringify(getAll());

  // Single notification channel: any change to the target window's URL — our
  // own set() or the shell's own history writes — arrives as fused:urlchange
  // (D46/LM-8). Ancestor shells above a boundary are watched too, so an edit
  // to a hand-typed global (D72) also notifies; the snapshot diff guard makes
  // the layout shell's frequent `_layout` re-syncs no-ops here.
  // Target and ancestor shells outlive this document (they survive pane
  // reloads/navigation), so detach on pagehide — otherwise every reload
  // stacks another stale notifyIfChanged on the shared shell windows.
  const hookedWindows = [target, ...ancestorWindows()];
  for (const win of hookedWindows) {
    win.addEventListener("fused:urlchange", notifyIfChanged);
  }
  window.addEventListener("pagehide", () => {
    // A pending coalesced write (D99) must not die with this document — the
    // URL is the bookmarkable truth.
    flushHistory();
    // Likewise a queued supersession report: without it the abandoned calls a
    // closing page had in flight get recorded as ordinary successes.
    flushSuperseded();
    for (const win of hookedWindows) {
      try {
        win.removeEventListener("fused:urlchange", notifyIfChanged);
      } catch (e) {
        /* window already gone */
      }
    }
  });

  // ---- stale-request cancellation (RH-9 / D114) -----------------------------
  // Every runPython call belongs to a "latest-wins channel". By DEFAULT the
  // channel is the .py path, so firing a new call for a file ABORTS the prior
  // in-flight call for that same file — the slider primitive, on by default:
  // scrubbing through values leaves only the last one's request alive (each
  // superseded fetch is cancelled, freeing the browser connection and letting
  // the server drop the now-irrelevant subprocess when it sees the closed
  // socket). Callers that genuinely need several concurrent calls to the SAME
  // file — polling loops, per-tile fetches, writes that must finish — pass
  // `opts.key: null` to opt OUT (fully concurrent, D113's old default), or a
  // distinct `opts.key` string to group differently. `opts.signal` (a standard
  // AbortSignal) composes — the fetch aborts on whichever fires first.
  //
  // A call SUPERSEDED by a newer same-channel call is stale by definition: its
  // result would only be overwritten, so its promise NEVER settles and the
  // caller's continuation (its await / .then — even inside a try/catch) simply
  // stops, drawing nothing. That keeps a scrub silent for every page shape: no
  // AbortError surfaces through the page's own catch (which would otherwise
  // flash a stale error while the latest value is still computing). An abort
  // from the caller's OWN signal instead rejects with a standard AbortError,
  // which the unhandledrejection handler below treats as benign.
  const inflightByKey = new Map();

  // ---- call-log attribution (docs/CALL_LOG_DESIGN.md §4.3) ------------------
  // The server logs one record per API call a page makes, but the middleware
  // sees only the route — not WHICH page called it. These headers carry that:
  // X-Fused-Page is the page's own absolute path (the `path` query param of
  // this iframe's /render URL), X-Fused-Target the file it is previewing
  // (`_file`, set when this page is a template), X-Fused-Call a per-call id.
  //
  // The paths ride percent-encoded (calls.py decodes them): a header value is
  // ISO-8859-1 only, and fetch() throws rather than sending one that is not.
  //
  // Only this runtime sends them, so the shell's own requests (/api/fs/list,
  // the conditions probe) carry no attribution and are excluded from the app
  // log by construction — no endpoint blocklist to keep in sync. A custom
  // header forces a CORS preflight exactly as X-Fused does; this is not auth
  // (D3/D36 stand), it is attribution.
  function ownQuery(key) {
    try {
      return new URLSearchParams(window.location.search).get(key);
    } catch (e) {
      return null;
    }
  }

  function newCallId() {
    try {
      if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    } catch (e) {
      /* fall through to the Math.random id below */
    }
    return "c" + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
  }

  // Merge the attribution headers into a call's own headers. Callers pass the
  // headers they need (Content-Type, X-Fused) and get those plus attribution.
  function callHeaders(extra, callId) {
    const headers = Object.assign({}, extra || {});
    const page = ownQuery("path");
    if (page) headers["X-Fused-Page"] = encodeURIComponent(page);
    const target = ownQuery("_file");
    if (target) headers["X-Fused-Target"] = encodeURIComponent(target);
    // A caller-supplied id lets the abort path name the call it cancelled
    // (see reportSuperseded); anything else gets a fresh one.
    headers["X-Fused-Call"] = callId || newCallId();
    // Supersessions ride the request that CAUSED them, when there is one — see
    // takePendingSupersedes.
    const abandoned = takePendingSupersedes();
    if (abandoned) headers["X-Fused-Supersedes"] = abandoned;
    return headers;
  }

  // ---- superseded reporting (docs/CALL_LOG_DESIGN.md §6.2, SPEC CL-5) -------
  // The server CANNOT infer that a call was abandoned: aborting the fetch does
  // not raise into the handler, so it runs to completion and gets recorded as an
  // ordinary success — which would make one slider drag look like a dozen real
  // requests and put their durations into the latency percentiles. Only the page
  // knows, so it says so, keyed by the X-Fused-Call id it already sent.
  //
  // The mark has to reach the server BEFORE the superseded call's record is
  // written, because the store is append-only and finish() stamps the outcome in
  // place rather than patching a line after the fact.
  //
  // So it rides the request that caused it. A supersession only ever happens
  // because the page is issuing a NEW call on the same channel, and that request
  // leaves in the same synchronous task as the abort — so `X-Fused-Supersedes`
  // on it reaches the server as early as anything can, with no extra round trip.
  //
  // The separate POST below used to be the only path, deferred by setTimeout(0)
  // to batch. Measured in Chromium against a local server, that landed ~19 ms
  // after the abort — and any superseded call whose handler finished inside that
  // window was written as `ok` and counted in the latency percentiles, which is
  // the exact failure CL-5 exists to prevent. In-process helpers (D72) routinely
  // finish that fast, so a template re-querying per keystroke hit it often.
  // Reported by Bugbot; the header closes the gap for every supersession that
  // has a causing request, which is all of them.
  //
  // The POST survives as the unload backstop: pagehide has ids with no request
  // left to carry them.
  const supersededIds = [];
  let supersededQueued = false;

  function reportSuperseded(callId) {
    if (!callId || !ownQuery("path")) return;
    supersededIds.push(callId);
    if (supersededQueued) return;
    supersededQueued = true;
    // Backstop only — the header normally drains this first (see
    // takePendingSupersedes), leaving the flush a no-op.
    setTimeout(flushSuperseded, 0);
  }

  // Hand the pending ids to the outgoing request, and take them out of the
  // queue so the backstop POST does not re-send what the server already has.
  // Re-sending is not harmless: `finish()` CONSUMES a mark, so a duplicate
  // arriving after the record was written would sit in the server's map until
  // its TTL instead of matching anything.
  function takePendingSupersedes() {
    if (!supersededIds.length) return "";
    return supersededIds.splice(0, supersededIds.length).join(",");
  }

  function flushSuperseded() {
    supersededQueued = false;
    const ids = supersededIds.splice(0, supersededIds.length);
    if (!ids.length) return;
    try {
      // keepalive, NOT navigator.sendBeacon: a beacon cannot set the X-Fused
      // header this endpoint requires, so it would simply 403. keepalive gives
      // the same survives-unload property with headers intact.
      fetch("/api/calls/event", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Fused": "1" },
        body: JSON.stringify({ kind: "superseded", call_ids: ids }),
        keepalive: true,
      }).catch(() => {});
    } catch (e) {
      /* reporting is best-effort; never let it break a run */
    }
  }

  // ---- the script-venv install loader (SPEC PY-18, D173) --------------------
  //
  // Most .py files run on the app's own interpreter and install nothing. A file
  // that declares a `# /// script` header naming something the app doesn't ship
  // (geotiff's imagecodecs, zarr_aoi's s3fs, pano's py360convert…) needs a
  // one-time download, and /api/run answers `needs_install` rather than blocking
  // past runPython's ~30s budget. Handled HERE, in the shell, so every template
  // gets it without a line of its own code.
  //
  // Shape follows the docs template's typst install (a detached worker writing
  // progress.json, polled) — one pattern in this app, not two.
  const INSTALL_POLL_MS = 500;
  // The first second is polled faster. A fixed 500ms grid is invisible next to a
  // four-minute download but dominates a short one: a ~540ms install lands
  // mid-grid, so it is not noticed until the SECOND poll, turning ~0.5s of work
  // into ~1.5s of blocked page. The window is bounded rather than adaptive because
  // the only installs it can help are the ones that finish inside it, and a long
  // download must not end up polled harder than it was before.
  const INSTALL_POLL_FAST_MS = 100;
  const INSTALL_FAST_POLL_WINDOW_MS = 1000;

  // How long an install may run before the overlay appears at all.
  //
  // The overlay used to mount synchronously, before anything was known about how
  // long the install would take — so an install that finishes in tens of
  // milliseconds still threw a full-screen modal over the page and tore it down
  // again, which reads as a flicker/flash rather than as progress. Now that a
  // header the app interpreter already satisfies installs NOTHING (see engine.py's
  // `app_satisfies`), the installs that remain are either genuinely long (a real
  // download, where 600ms of delay is imperceptible) or genuinely short (a warm uv
  // cache, where the modal was pure noise). Delay separates the two without having
  // to predict which one this is.
  const INSTALL_MOUNT_DELAY_MS = 600;

  let installUi = null;
  // Live installs, as key -> { row, count }.
  //
  // A page can call several .py files, each with its OWN header, so N installs
  // with N DISTINCT keys run at once. Each therefore gets its own ROW — its own
  // title, detail, bar and Cancel — because one shared set of nodes made N
  // installs illegible: the title named whichever install started last, N pollers
  // rewrote one detail line at 2Hz, and one Cancel button carried N listeners, so
  // a single click cancelled every install while each chain's message overwrote
  // the others'.
  //
  // Still ref-COUNTED per key, which is a different case and remains real: two .py
  // files with IDENTICAL requirement sets share one venv key, so they share one
  // row, and the row may only go when the LAST of them settles. The count lives
  // inside the entry so both facts — which row, how many waiters — are one piece of
  // state that cannot disagree with itself.
  const installing = new Map();

  // The indeterminate bar (D213). The worker parks at pct 25 for the WHOLE download
  // — `ensure_requirements_venv` runs uv behind captured output, so there is no
  // per-package progress to report — and a bar sitting at 25% for four minutes reads
  // as frozen, which is what users reported. An indeterminate bar says the true
  // thing: this is alive, and its remaining time is unknown.
  //
  // Keyframes need a stylesheet (the overlay is otherwise built from inline styles,
  // which cannot express an animation), so one <style> is injected on first use and
  // found by id afterwards — a per-install copy would pile up in `head` over a
  // session. Injected here rather than at module load because a page that never
  // installs anything should not carry it.
  const INSTALL_BAR_STYLE_ID = "fused-install-bar-style";
  const INSTALL_BAR_ANIM = "fused-install-sweep";

  function ensureInstallBarStyle() {
    if (document.getElementById(INSTALL_BAR_STYLE_ID)) return;
    const style = document.createElement("style");
    style.id = INSTALL_BAR_STYLE_ID;
    style.textContent =
      "@keyframes " + INSTALL_BAR_ANIM + "{" +
      "0%{transform:translateX(-110%)}100%{transform:translateX(410%)}}";
    document.head.appendChild(style);
  }

  // `dataset.indeterminate` is the DOM-observable contract: the tests assert on it,
  // because no headless test can see whether an animation LOOKS right. Takes a ROW
  // (one install's nodes), not the overlay: with several installs live, "the bar" is
  // not a thing there is one of.
  function installBarIndeterminate(row, on) {
    const bar = row.bar;
    if (on) {
      if (bar.dataset.indeterminate === "1") return; // never restart the sweep
      ensureInstallBarStyle();
      bar.dataset.indeterminate = "1";
      // A narrow fill that travels, rather than a width that grows: `transition` is
      // turned off first, or the jump to 30% animates as if it were progress.
      bar.style.transition = "none";
      bar.style.width = "30%";
      bar.style.animation = INSTALL_BAR_ANIM + " 1.1s ease-in-out infinite";
    } else {
      if (bar.dataset.indeterminate !== "1") return;
      delete bar.dataset.indeterminate;
      bar.style.animation = "";
      bar.style.transition = "width 0.3s ease";
    }
  }

  // Paint one progress record. Module-scope (not a closure inside installEnv) so the
  // stage-to-bar rule has exactly one definition and can be driven directly by a
  // test; `notice` is installEnv's sticky message, which must outrank the record's
  // own detail (see the `notice` comment there).
  function paintInstall(row, prog, notice) {
    if (!prog) return;
    row.detail.textContent = notice || prog.detail || prog.stage || "";
    // Both long steps are indeterminate, for the same reason: `install` runs uv
    // behind captured output, and `python` (D214, the interpreter download) captures
    // it too, so neither has a percentage to report. Listed rather than inferred from
    // pct, because a stage that legitimately sits at one number is exactly what an
    // indeterminate bar is for — and a stage added later without a decision here
    // should render as a plain bar, not silently inherit the sweep.
    if ((prog.stage === "install" || prog.stage === "python") && !prog.done) {
      installBarIndeterminate(row, true);
      return;
    }
    installBarIndeterminate(row, false);
    if (typeof prog.pct === "number") row.bar.style.width = prog.pct + "%";
  }

  // Act on this needs_install, or fail? The rule is PROGRESS, not a count: a key we
  // have not installed yet is a new thing to install, and the same key coming back
  // after we installed it means nothing changed — the real loop, and one clear
  // failure beats installing forever.
  //
  // A boolean "already installed once" is what this replaces, and it was wrong as
  // soon as a run could legitimately need two rounds: with no pinned Python on this
  // machine the first install is the interpreter and the packages follow, each under
  // its own key (D214), so the second — correct — round died as "something disagrees
  // about the venv key". Named and at module scope for the same reason `paintInstall`
  // is: one definition of a subtle rule, directly testable, rather than a condition
  // buried in a promise chain.
  function shouldInstall(need, installed) {
    return Boolean(need && need.key) && !installed.has(need.key);
  }

  // The backdrop, and the container every install's row is appended to. It owns no
  // title/detail/bar of its own any more — those belong to a row, because there is
  // no longer one install to describe.
  function installOverlay() {
    if (installUi) return installUi;
    const el = document.createElement("div");
    el.style.cssText = [
      "position:fixed", "inset:0", "z-index:2147483646",
      "background:rgba(12,14,18,0.94)", "color:#e6edf3",
      "font-family:ui-sans-serif,system-ui,-apple-system,sans-serif",
      "font-size:14px", "padding:32px", "box-sizing:border-box",
      "display:flex", "flex-direction:column", "gap:14px",
      "align-items:center", "justify-content:center", "text-align:center",
    ].join(";");
    const rows = document.createElement("div");
    rows.style.cssText = [
      "display:flex", "flex-direction:column", "gap:26px",
      "align-items:center", "width:100%",
    ].join(";");
    el.appendChild(rows);
    // `mountTimer` is part of the state, not a local in `showInstall`: the delay has
    // to be cancellable from `hideInstall` (an install that finishes inside the
    // window must not mount an overlay afterwards) and must not be restarted by a
    // second install arriving while it is still pending.
    installUi = { el, rows, mounted: false, mountTimer: null };
    return installUi;
  }

  // One install's nodes. Built per key, and — deliberately — built SYNCHRONOUSLY in
  // `showInstall` even though mounting is delayed: `installEnv` registers its cancel
  // handler and paints its first record immediately, so the nodes have to exist
  // before the overlay does. A row that is never mounted is simply never seen.
  function installRow() {
    const el = document.createElement("div");
    el.style.cssText = [
      "display:flex", "flex-direction:column", "gap:10px",
      "align-items:center", "text-align:center",
    ].join(";");
    const title = document.createElement("div");
    title.style.cssText = "font-size:17px;font-weight:600;";
    const detail = document.createElement("div");
    detail.style.cssText =
      "opacity:0.8;max-width:60ch;white-space:pre-wrap;word-break:break-word;" +
      "font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;";
    const track = document.createElement("div");
    track.style.cssText =
      "width:min(420px,80vw);height:6px;border-radius:3px;background:#2b313b;overflow:hidden;";
    const bar = document.createElement("div");
    bar.style.cssText =
      "height:100%;width:0%;background:#4c8eda;transition:width 0.3s ease;";
    track.appendChild(bar);
    const cancel = document.createElement("button");
    cancel.textContent = "Cancel";
    cancel.style.cssText = [
      "margin-top:6px", "padding:6px 16px", "border-radius:6px",
      "border:1px solid #3a424e", "background:#1d222a", "color:#e6edf3",
      "font-size:13px", "cursor:pointer",
    ].join(";");
    el.append(title, track, detail, cancel);
    return { el, title, detail, bar, cancel };
  }

  // Mount after INSTALL_MOUNT_DELAY_MS, at most one timer at a time.
  //
  // The `installing.size` re-check inside the callback is the point of the whole
  // mechanism, not a precaution: between scheduling and firing, every install can
  // have finished and called `hideInstall`, and mounting then would put a modal over
  // the page with nothing running behind it and no live Cancel to dismiss it.
  function mountInstallSoon(ui) {
    if (ui.mounted || ui.mountTimer !== null) return;
    ui.mountTimer = setTimeout(function () {
      ui.mountTimer = null;
      if (!installing.size) return;
      document.body.appendChild(ui.el);
      ui.mounted = true;
    }, INSTALL_MOUNT_DELAY_MS);
  }

  function showInstall(need) {
    const ui = installOverlay();
    let entry = installing.get(need.key);
    if (!entry) {
      entry = { row: installRow(), count: 0 };
      installing.set(need.key, entry);
      ui.rows.appendChild(entry.row.el);
    }
    entry.count += 1;
    mountInstallSoon(ui);
    const row = entry.row;
    // Name what is actually being fetched. On the interpreter round (D214) the
    // packages are NOT downloading yet, and titling that round with their names is
    // the kind of small lie that makes a four-minute wait feel broken — the user
    // watches "Installing tensorflow" and nothing about tensorflow is happening.
    row.title.textContent = need.python
      ? "Installing Python " + need.python
      : "Installing " + (need.requirements || []).join(", ");
    // Deliberately NOT "starting…" at 0%. `/api/env/install` JOINS an install
    // already in flight rather than duplicating it, so re-opening a page whose
    // download is four minutes old used to paint 0% and then jump to 25% on the
    // first poll — nothing was lost, but a user switching between apps saw
    // 0% → 25% → freeze over and over and concluded it was looping. The initial
    // state therefore asserts no percentage at all: indeterminate until the
    // server's own record arrives (installEnv paints the POST response, which
    // carries it), so the first honest paint is the only paint.
    row.detail.textContent = "contacting the installer…";
    installBarIndeterminate(row, true);
    return row;
  }

  function hideInstall(key) {
    const entry = installing.get(key);
    if (entry) {
      entry.count -= 1;
      if (entry.count > 0) return; // another call is still waiting on this key
      entry.row.el.remove();
      installing.delete(key);
    }
    if (installing.size) return; // another install is still running
    const ui = installUi;
    if (!ui) return;
    // A pending mount is cancelled, not left to fire: this is the path a fast
    // install takes, and without the clear the overlay would appear ~600ms AFTER
    // everything finished and stay there (the callback's own size check would also
    // catch it, but leaving a timer armed to do nothing is how the next person
    // reading this concludes the delay is unreliable).
    if (ui.mountTimer !== null) {
      clearTimeout(ui.mountTimer);
      ui.mountTimer = null;
    }
    if (ui.mounted) {
      ui.el.remove();
      ui.mounted = false;
    }
  }

  function envPost(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Fused": "1" },
      body: JSON.stringify(body),
    }).then((res) => res.json().then((data) => ({ res, data })));
  }

  // Run the install to completion. Resolves when the venv is ready; rejects with
  // the installer's VERBATIM message otherwise — a resolver failure ("no wheels
  // with a matching platform tag for imagecodecs") is the actual answer the user
  // needs, and rewriting it into something friendlier is what made this opaque
  // in the first place.
  function installEnv(need, pyPath, ownPath) {
    const row = showInstall(need);
    let cancelled = false;
    // The key to poll and to cancel is the INSTALLER's, not the pre-flight's.
    // /api/env/install re-derives the requirements from the .py on disk and
    // returns its own key, and editing a .py and letting live-reload re-run it is
    // this app's core workflow — so the file really can change between /api/run's
    // needs_install and this POST. Polling the stale key then reads a null
    // progress record and fails an install that is running fine; cancelling the
    // stale key leaves the real download running. Mutable because the cancel
    // handler is registered BEFORE the POST resolves and must see the update.
    let activeKey = need.key;
    // A message that must survive the next poll's paint(). `cancel()` reports
    // False when there is nothing to kill YET — inside the spawn window the claim
    // exists but `Popen` has not returned, so no pid is recorded. That answer used
    // to vanish: "cancelling…" was overwritten by the installer's own detail on
    // the very next poll, the install ran to completion, and the script the user
    // had just cancelled executed with nothing anywhere admitting the cancel was
    // dropped. Held in a variable rather than written straight to the element
    // because paint() runs on a timer and would win.
    let notice = "";
    const onCancel = () => {
      cancelled = true;
      notice = "";
      row.detail.textContent = "cancelling…";
      envPost("/api/env/cancel", { key: activeKey })
        .then(({ data }) => {
          if (data && data.cancelled === false) {
            // Not a failure of the request — the server had no installer to
            // signal. Said out loud, and the button stays live (its listener is
            // never removed on click) so a second press reaches the pid once the
            // record carries one.
            notice =
              "the installer could not be stopped — it had not started yet, or " +
              "had already finished. Press Cancel again if it is still running.";
            row.detail.textContent = notice;
          }
        })
        .catch(() => {});
    };
    row.cancel.addEventListener("click", onCancel);

    const paint = (prog) => paintInstall(row, prog, notice);

    // Measured from the click, not from the previous poll, so the fast window is a
    // property of the INSTALL's age rather than of how many times we happened to
    // poll — a slow first response would otherwise stretch the fast phase
    // arbitrarily.
    const startedAt = Date.now();
    const pollDelay = () =>
      Date.now() - startedAt < INSTALL_FAST_POLL_WINDOW_MS
        ? INSTALL_POLL_FAST_MS
        : INSTALL_POLL_MS;

    const poll = () =>
      fetch("/api/env/progress?key=" + encodeURIComponent(activeKey), {
        headers: { "X-Fused": "1" },
      })
        .then((res) => res.json())
        .then((body) => {
          const prog = body && body.progress;
          paint(prog);
          if (!prog) {
            // The record vanished (or never landed). Treat as failure rather
            // than polling forever — a silent loader is the failure mode this
            // whole flow exists to remove.
            throw new Error("the installer left no progress record");
          }
          if (!prog.done) {
            return new Promise((r) => setTimeout(r, pollDelay())).then(poll);
          }
          if (prog.error) throw new Error(prog.error);
          return prog;
        });

    return envPost("/api/env/install", { py: pyPath, html: ownPath })
      .then(({ res, data }) => {
        if (!res.ok) throw new Error((data && data.error) || "HTTP " + res.status);
        // The installer's key wins over the pre-flight's from here on (see
        // `activeKey`). `hideInstall` still gets need.key — that is the entry
        // `showInstall` counted.
        if (data && typeof data.key === "string" && data.key) activeKey = data.key;
        paint(data && data.progress);
        return poll();
      })
      .then(
        (prog) => {
          row.cancel.removeEventListener("click", onCancel);
          hideInstall(need.key);
          if (cancelled) {
            // The install finished anyway — a cancel the server could not honour,
            // or one that lost a race with the last poll. The user's intent still
            // decides whether the SCRIPT runs: resolving here ran it, which is the
            // one outcome pressing Cancel must never produce. The venv is built
            // and stays built; only the run is abandoned.
            const e = new Error("the install was cancelled");
            e.type = "EnvInstallCancelled";
            throw e;
          }
          return prog;
        },
        (err) => {
          row.cancel.removeEventListener("click", onCancel);
          hideInstall(need.key);
          if (cancelled) {
            const e = new Error("the install was cancelled");
            e.type = "EnvInstallCancelled";
            throw e;
          }
          // Verbatim, and tagged so a page can tell an install failure from its
          // script's own error.
          err.type = "EnvInstallError";
          err.traceback = err.message;
          throw err;
        }
      );
  }

  function runPython(pyPath, params, opts) {
    opts = opts || {};
    // Default channel = the .py path; opts.key === null opts out, a string regroups.
    const key = opts.key === undefined ? pyPath : opts.key;
    const keyed = key !== null;
    const controller = new AbortController();
    controller._callId = newCallId();
    if (keyed) {
      const prev = inflightByKey.get(key);
      if (prev) {
        prev._supersededByKey = true; // its impending abort is supersession, not an error
        reportSuperseded(prev._callId); // so the log doesn't count it as a real call
        prev.abort();
      }
      inflightByKey.set(key, controller);
    }
    let detachSignal = null;
    if (opts.signal) {
      if (opts.signal.aborted) controller.abort();
      else {
        const onAbort = () => controller.abort();
        opts.signal.addEventListener("abort", onAbort);
        detachSignal = () => opts.signal.removeEventListener("abort", onAbort);
      }
    }
    // Detach the caller's abort listener (reusing one long-lived signal across
    // many calls must not accumulate listeners / pin controllers) and free the
    // channel slot — the latter only if it is still ours (a newer same-key call
    // may have already replaced us in the map).
    const cleanup = () => {
      if (detachSignal) detachSignal();
      if (keyed && inflightByKey.get(key) === controller) inflightByKey.delete(key);
    };
    const ownPath = new URLSearchParams(window.location.search).get("path");
    const attempt = () =>
      fetch("/api/run", {
        method: "POST",
        // X-Fused forces a CORS preflight so a foreign page can't fire this
        // execute endpoint blind (see server.py _require_fused).
        headers: callHeaders({ "Content-Type": "application/json", "X-Fused": "1" },
                             controller._callId),
        body: JSON.stringify({ py: pyPath, html: ownPath, params: params || {} }),
        signal: controller.signal,
      }).then((res) => res.json());

    // `installed` guards against a loop, and holds the KEYS already installed
    // rather than a boolean because a run can legitimately need two rounds: with no
    // pinned Python on this machine the first install is the interpreter and the
    // packages follow (D214), each under its own key. A boolean would fail that
    // second, correct round with "something disagrees about the venv key".
    //
    // The rule is progress, not a count: a needs_install naming a key we have not
    // installed yet is a new thing to install, and the SAME key coming back after we
    // installed it means nothing changed — which is the real loop, and still one
    // clear failure rather than installing forever.
    const handle = (data, installed) => {
      if (data.stdout) {
        console.log("[python]", data.stdout);
      }
      // Watch the executed file for auto-reload, even on failure (LR-2): a
      // broken py that gets fixed must still trigger a reload. Read before
      // the ok check so it's recorded either way.
      if (data.resolved_py) watchPath(data.resolved_py);
      if (shouldInstall(data.needs_install, installed)) {
        installed.add(data.needs_install.key);
        return installEnv(data.needs_install, pyPath, ownPath).then(() =>
          attempt().then((next) => handle(next, installed))
        );
      }
      if (!data.ok) {
        const err = new Error(data.error && data.error.message);
        err.type = data.error && data.error.type;
        err.traceback = data.error && data.error.traceback;
        err.stdout = data.stdout;
        throw err;
      }
      return data.result;
    };

    return attempt()
      .then((data) => handle(data, new Set()))
      .then(
        (result) => {
          cleanup();
          // A newer same-channel call superseded us after the response arrived
          // but before this ran — honor never-settle so the stale continuation
          // still doesn't run.
          if (controller._supersededByKey) return new Promise(() => {});
          return result;
        },
        (err) => {
          cleanup();
          // The caller's OWN signal aborting takes precedence: they asked to
          // cancel, so reject with the standard AbortError (their catch/finally
          // must run) — even in the common abort-then-new-call idiom where a
          // newer same-channel call also marked us superseded.
          if (opts.signal && opts.signal.aborted) throw err;
          // Otherwise, superseded by a newer same-channel call → hang forever so
          // the stale continuation never runs. Real errors propagate.
          if (controller._supersededByKey) return new Promise(() => {});
          throw err;
        }
      );
  }

  // Synchronous URL of the raw-bytes endpoint for a file — for <img>/<embed>
  // src, "open raw" links, etc. A RELATIVE path is resolved page-relative
  // (SPEC RH-1): we pass the page's own absolute path as `base` and the server
  // joins them (the same contract runPython uses via `html`), so a page can say
  // fused.rawUrl("data/x.json") and have it work here AND, when hosted, resolve
  // against the bundle's _asset route by the same key. An absolute path needs no
  // base and is sent unchanged.
  function rawUrl(path) {
    let url = "/api/fs/raw?path=" + encodeURIComponent(path);
    if (path && path[0] !== "/") {
      const ownPath = new URLSearchParams(window.location.search).get("path");
      if (ownPath) url += "&base=" + encodeURIComponent(ownPath);
    }
    return url;
  }

  // Fetch file metadata (same shape as /api/fs/stat). Rejects with an Error
  // carrying the server's message, mirroring runPython's rejection style.
  function stat(path) {
    return fetch("/api/fs/stat?path=" + encodeURIComponent(path), { headers: callHeaders() })
      .then((res) => res.json().then((data) => ({ res, data })))
      .then(({ res, data }) => {
        if (!res.ok) throw new Error((data && data.error) || "HTTP " + res.status);
        return data;
      });
  }

  // Read a file's text via the raw endpoint.
  // NOTE (call log): readFile is attributed because it fetches, so the server
  // sees the headers. rawUrl() is SYNCHRONOUS and returns a URL string that
  // usually lands in an <img>/<embed> src — the browser issues that request
  // with no way to attach a header, so element-src reads are NOT in the call
  // log. Deliberate: adding a `_page` query param instead would change every
  // raw URL (cache keys, and the hosted runtime's bundle-key resolution in
  // docs/EXPORT.md), which is not worth it for one route's attribution.
  function readFile(path) {
    return fetch(rawUrl(path), { headers: callHeaders() }).then((res) => {
      if (!res.ok) throw new Error("failed to read " + path + " (HTTP " + res.status + ")");
      return res.text();
    });
  }

  // Write UTF-8 text to a file, returning the fresh stat object. opts:
  //   { expectedMtime } — optimistic lock; omit to write unconditionally.
  //   { create: true }  — create only if absent; an existing path rejects.
  // A 409 becomes an Error with `type: "conflict"` and the server's current
  // `mtime` attached, so callers can offer reload/overwrite. A read-only
  // refusal (403 {"error":"readonly"}) becomes `type: "readonly"` — the
  // backstop for templates that never checked stat().writable.
  //
  // `create` exists so "make this file if it isn't there" can be ONE call the
  // server decides, rather than stat-then-write with a window in between — the
  // markdown template's ghost-note create is exactly that, and stat-then-write
  // there meant a click on a note that already existed replaced it with a stub.
  // Its 409 is typed `"exists"`, not `"conflict"`: the two mean different
  // things (this one is "it is already there", the lock's is "it changed"), and
  // a caller that offers overwrite-anyway on a conflict must not offer it here.
  function writeFile(path, content, opts) {
    const payload = { path: path, content: content };
    if (opts && opts.expectedMtime !== undefined && opts.expectedMtime !== null) {
      payload.expected_mtime = opts.expectedMtime;
    }
    if (opts && opts.create) payload.create = true;
    return fetch("/api/fs/write", {
      method: "POST",
      headers: callHeaders({ "Content-Type": "application/json", "X-Fused": "1" }),
      body: JSON.stringify(payload),
    })
      .then((res) => res.json().then((data) => ({ res, data })))
      .then(({ res, data }) => {
        if (res.status === 409 && payload.create) {
          const err = new Error("file already exists");
          err.type = "exists";
          throw err;
        }
        if (res.status === 409) {
          const err = new Error("file changed on disk");
          err.type = "conflict";
          err.mtime = data && data.mtime;
          throw err;
        }
        if (res.status === 403 && data && data.error === "readonly") {
          const err = new Error("file is read-only");
          err.type = "readonly";
          throw err;
        }
        if (!res.ok) throw new Error((data && data.error) || "HTTP " + res.status);
        return data;
      });
  }

  // ---- sidecar path mapping (D83-reversal) ---------------------------------
  // INTERNAL ONLY — see the file header for why this is not on `window.fused`.
  // Reached by built-in templates as window._fusedSidecarPath /
  // window._fusedTargetPathFromSidecarPath, assigned alongside window.fused
  // below.
  //
  // Every per-file sidecar now lives under sidecarRoot (~/.fused-render/
  // sidecar/<mapped path>.json) instead of beside the target file. Mirrors
  // fused_render/shell/storage.py's _sidecar_subpath — keep the two in step.
  // A drive letter becomes its own single-letter folder, a UNC share nests
  // under "unc/<server>/<share>/...", and a POSIX path just drops its
  // leading "/". Case is preserved exactly throughout.
  function _sidecarSubpath(absPath) {
    const drive = /^([A-Za-z]):[\\/](.*)$/.exec(absPath);
    if (drive) {
      const tail = drive[2].replace(/\\/g, "/").replace(/^\/+/, "");
      return drive[1].toUpperCase() + (tail ? "/" + tail : "");
    }
    const unc = /^\\\\([^\\]+)\\([^\\]+)(\\.*)?$/.exec(absPath);
    if (unc) {
      const tail = (unc[3] || "").replace(/\\/g, "/").replace(/^\/+/, "");
      return "unc/" + unc[1] + "/" + unc[2] + (tail ? "/" + tail : "");
    }
    return absPath.replace(/^\/+/, "");
  }

  // The `<file>.json` sidecar's location for an absolute `file` path. Async:
  // the mapping needs sidecarRoot, which arrives from the server's one-time
  // /api/config fetch (see loadConfig/sidecarRoot above) — every template
  // that used to build `file + ".json"` synchronously now awaits this once.
  function sidecarPath(file) {
    return loadConfig().then(() => {
      if (typeof sidecarRoot !== "string") {
        throw new Error("sidecar root unavailable (no /api/config response)");
      }
      return sidecarRoot + "/" + _sidecarSubpath(file) + ".json";
    });
  }

  // Inverse of sidecarPath, for the history/inspector view (HV-3): given a
  // path that MAY be a sidecar location (a user can navigate to one
  // directly), the target file it belongs to — or null if `path` isn't
  // under sidecarRoot. Heuristic on this host's own path shape, same as the
  // forward mapping: a leading single-letter segment is a drive, a leading
  // "unc" segment is a share, anything else is POSIX — meaningful only for a
  // path this same host's sidecarPath could have produced.
  //
  // Windows-shaped segments (drive letter / "unc") are only ever real on a
  // Windows host — gated on sidecarRoot's OWN shape, not just a segment's
  // length, so a real POSIX top-level dir that happens to be one letter long
  // (e.g. a real "/a/file.txt", subpath "a/file.txt") is never misread as a
  // drive letter on a POSIX host. A Windows home can itself be a UNC path
  // (a roaming profile on a network share) — canonical_fs_path only
  // normalizes a DRIVE-letter path (backslash is a legal POSIX filename
  // character, so a UNC root stays backslashed on purpose, same as a POSIX
  // one) — so drive-letter-shaped is not the only Windows shape sidecarRoot
  // can take; a leading "\\\\" is the other (Bugbot).
  function targetPathFromSidecarPath(path) {
    return loadConfig().then(() => {
      if (typeof sidecarRoot !== "string") return null;
      if (path.indexOf(sidecarRoot + "/") !== 0 || !/\.json$/i.test(path)) return null;
      const rel = path.slice(sidecarRoot.length + 1, -".json".length);
      const parts = rel.split("/");
      const windowsHost = /^[A-Za-z]:/.test(sidecarRoot) || /^\\\\/.test(sidecarRoot);
      if (windowsHost && parts[0] && parts[0].length === 1 && /[A-Za-z]/.test(parts[0])) {
        return parts[0].toUpperCase() + ":\\" + parts.slice(1).join("\\");
      }
      if (windowsHost && parts[0] === "unc" && parts.length >= 3) {
        return "\\\\" + parts[1] + "\\" + parts[2] +
          (parts.length > 3 ? "\\" + parts.slice(3).join("\\") : "");
      }
      return "/" + parts.join("/");
    });
  }

  // Write BYTES to a file, returning the fresh stat object. `blob` is a Blob or
  // File — the thing a paste/drop event hands you — and the bytes land on disk
  // unchanged, which writeFile cannot do: it takes UTF-8 text only, so a PNG
  // round-tripped through it comes back mangled.
  //
  // Multipart, not base64 in JSON: base64 inflates the payload by a third,
  // irrelevant for a screenshot and very relevant for a pasted video. The
  // FormData is handed to fetch WITHOUT a Content-Type header on purpose — the
  // browser generates `multipart/form-data; boundary=…`, and setting the header
  // by hand drops the boundary and makes the body unparseable.
  //
  // Like writeFile, a read-only refusal (403 {"error":"readonly"}) becomes
  // `type: "readonly"` so callers branch on the type, not the message. There is
  // no optimistic lock and no `create`: a freshly pasted blob has no prior
  // version to conflict with.
  function uploadFile(path, blob) {
    const form = new FormData();
    form.append("path", path);
    form.append("file", blob);
    return fetch("/api/fs/upload", {
      method: "POST",
      headers: callHeaders({ "X-Fused": "1" }),
      body: form,
    })
      .then((res) => res.json().then((data) => ({ res, data })))
      .then(({ res, data }) => {
        if (res.status === 403 && data && data.error === "readonly") {
          const err = new Error("file is read-only");
          err.type = "readonly";
          throw err;
        }
        if (!res.ok) throw new Error((data && data.error) || "HTTP " + res.status);
        return data;
      });
  }

  // Create a single directory, returning its stat object. Parents are NOT
  // auto-created (the server's contract, not this wrapper's).
  //
  // An existing path is typed `"exists"`, matching writeFile's create-409 and
  // for the same reason: "it is already there" is a different fact from "it
  // changed", and an ensure-this-directory caller wants to treat it as success.
  function mkdir(path) {
    return fetch("/api/fs/mkdir", {
      method: "POST",
      headers: callHeaders({ "Content-Type": "application/json", "X-Fused": "1" }),
      body: JSON.stringify({ path: path }),
    })
      .then((res) => res.json().then((data) => ({ res, data })))
      .then(({ res, data }) => {
        if (res.status === 409) {
          const err = new Error("directory already exists");
          err.type = "exists";
          throw err;
        }
        if (res.status === 403 && data && data.error === "readonly") {
          const err = new Error("directory is read-only");
          err.type = "readonly";
          throw err;
        }
        if (!res.ok) throw new Error((data && data.error) || "HTTP " + res.status);
        return data;
      });
  }

  // Ask an AI model: the shell runs the claude (Claude Code) CLI locally
  // (server.py /api/ai). Resolves with {text, model, usage}; rejects with an
  // Error carrying `.type` ("bad_request" | "ai_unavailable" | "ai_error" |
  // "timeout"), mirroring runPython's rejection style. opts:
  //   { systemPrompt, model, effort: "low"|"medium"|"high"|"xhigh", onChunk }
  // effort defaults to low = no extended thinking (fast, cheap); medium+
  // enables Claude Code's own effort/thinking semantics.
  // onChunk(text) opts the call into streaming: it fires per text delta as
  // the model produces it, and the promise still resolves with the same
  // {text, model, usage} at the end. Without it the request/response is the
  // plain JSON exchange it always was.
  // No latest-wins channel: an AI call is never a scrub, and cancelling a
  // half-billed completion buys nothing — calls run fully concurrent.
  function ai(prompt, opts) {
    opts = opts || {};
    if (typeof prompt !== "string" || !prompt.trim()) {
      const err = new Error("fused.ai(prompt): prompt must be a non-empty string");
      err.type = "bad_request";
      return Promise.reject(err);
    }
    const body = { prompt: prompt };
    if (opts.systemPrompt !== undefined) body.system_prompt = opts.systemPrompt;
    if (opts.model !== undefined) body.model = opts.model;
    if (opts.effort !== undefined) body.effort = opts.effort;
    const onChunk = typeof opts.onChunk === "function" ? opts.onChunk : null;
    if (onChunk) body.stream = true;
    const req = fetch("/api/ai", {
      method: "POST",
      headers: callHeaders({ "Content-Type": "application/json", "X-Fused": "1" }),
      body: JSON.stringify(body),
    });
    function fail(error) {
      const err = new Error(error && error.message);
      err.type = error && error.type;
      throw err;
    }
    if (!onChunk) {
      return req
        .then((res) => res.json())
        .then((data) => {
          if (!data.ok) fail(data.error);
          return data.result;
        });
    }
    // Streaming: the body is NDJSON — {"type":"chunk","text"} lines, then a
    // terminal {"type":"done"}. A chunk may split across read() boundaries,
    // so buffer and cut on newlines. Errors BEFORE the stream starts arrive
    // as ordinary non-200 JSON; after, as an ok:false done frame.
    return req.then((res) => {
      const ct = (res.headers.get("Content-Type") || "").indexOf("x-ndjson");
      if (!res.ok || ct === -1) {
        return res.json().then((data) => fail(data && data.error));
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let finished = null;
      function handleLine(line) {
        if (!line.trim()) return;
        const frame = JSON.parse(line);
        if (frame.type === "chunk") onChunk(frame.text);
        else if (frame.type === "done") finished = frame;
      }
      function pump() {
        return reader.read().then(({ done, value }) => {
          if (done) {
            if (buffer) handleLine(buffer);
            if (!finished) fail({ type: "ai_error", message: "stream ended without a done frame" });
            if (!finished.ok) fail(finished.error);
            return finished.result;
          }
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop();
          lines.forEach(handleLine);
          return pump();
        });
      }
      return pump();
    });
  }

  // --- Auto-reload (SPEC §13.3) ---------------------------------------------
  // This page watches a set of files via the SSE change feed; on any change it
  // reloads THIS frame (honest re-execution — we can't replay what the page did
  // with a python result). All reload logic lives here so every rendered page
  // (view, embed, standalone /render) gets it for free.
  let autoReloadEnabled = true;
  const watched = new Set();
  let es = null;
  let started = false;       // watching begins on DOMContentLoaded (LR-5)
  let resubscribeTimer = null;
  let reloadTimer = null;

  // Root of the mounts dir, fetched once from /api/config at start. Paths under
  // it are mount-backed: their bytes come from a read-only remote bucket that
  // never changes, so watching them for auto-reload buys nothing while every
  // poll is remote traffic. That traffic is exactly what killed a mount in the
  // fs/events stat-storm incident (a preview pane watching its mounted data
  // file, plus a huge .zarr). We drop those from the watch set entirely; the
  // template/py code that CAN change is always local and stays watched.
  // Kept mount-agnostic in the template: the server hands us the prefix.
  let mountsRoot = null;
  function isMountBacked(p) {
    return !!(mountsRoot && p && p.indexOf(mountsRoot + "/") === 0);
  }

  // A call-log file (fused_render/calls.py) is excluded from the watch set for a
  // sharper reason than mount-backed files: viewing one APPENDS TO IT, because
  // reading it is itself a logged API call. A watcher would therefore reload,
  // re-read, append, and reload again — forever, on any viewer that doesn't opt
  // out (log_studio only does with Tail on; duckdb and tree not at all). Killing
  // the watch removes the loop at its source instead of suppressing the records,
  // and costs nothing: the viewers that want live updates poll, and they already
  // turn auto-reload off while doing so precisely so a reload cannot rebuild the
  // frame mid-poll. Prefix + suffix come from /api/config, so generic templates
  // need to know nothing about the call log.
  let callsDir = null;
  let callsSuffix = ".calls.jsonl";
  function isCallLog(p) {
    if (!p) return false;
    if (callsSuffix && p.slice(-callsSuffix.length) === callsSuffix) return true;
    return !!(callsDir && p.indexOf(callsDir + "/") === 0);
  }

  // Root of the per-file sidecar subtree (~/.fused-render/sidecar), fetched
  // once from the SAME /api/config round trip as mountsRoot/callsDir above
  // (see configPromise) rather than a second fetch. sidecarPath/
  // targetPathFromSidecarPath below await this before answering, since there
  // is no way to compute a sidecar's location without it.
  //
  // The assignment lives INSIDE loadConfig's own promise chain, not in
  // startAutoReload's separate .then() below: startAutoReload only begins on
  // DOMContentLoaded (LR-5), but an inline template can call
  // fused.sidecarPath() before that fires. Doing the assignment here means
  // whichever caller reaches loadConfig() first — startAutoReload or a
  // template's own sidecarPath() call — populates sidecarRoot/mountsRoot/
  // callsDir exactly once, since configPromise is memoized.
  let sidecarRoot = null;
  let configPromise = null;
  function loadConfig() {
    if (!configPromise) {
      configPromise = fetch("/api/config").then((res) => res.json()).then((cfg) => {
        if (cfg && typeof cfg.mounts_root === "string") mountsRoot = cfg.mounts_root;
        if (cfg && typeof cfg.calls_dir === "string") callsDir = cfg.calls_dir;
        if (cfg && typeof cfg.calls_suffix === "string") callsSuffix = cfg.calls_suffix;
        if (cfg && typeof cfg.sidecar_root === "string") sidecarRoot = cfg.sidecar_root;
        return cfg;
      }).catch(() => ({}));
    }
    return configPromise;
  }

  function isUnwatchable(p) {
    return isMountBacked(p) || isCallLog(p);
  }

  function resubscribe() {
    // A reconnect timer may be pending (onclose below); a direct call must
    // cancel it or the stale timer would close and reopen the fresh socket.
    clearTimeout(resubscribeTimer);
    resubscribeTimer = null;
    if (es) {
      const old = es;
      es = null; // null first so old.onclose knows the close was deliberate
      old.close();
    }
    if (!autoReloadEnabled || watched.size === 0) return;
    const query = [...watched].map((p) => "path=" + encodeURIComponent(p)).join("&");
    // WebSocket, not EventSource (D74): SSE holds an HTTP/1.1 socket per open
    // pane and Chrome caps those at 6 per origin — a 6-pane panel starved
    // every later fetch (runPython hung forever). WS has its own, much larger
    // connection pool.
    const proto = window.location.protocol === "https:" ? "wss://" : "ws://";
    const sock = new WebSocket(proto + window.location.host + "/api/fs/events?" + query);
    es = sock;
    sock.onmessage = (ev) => {
      let data;
      try {
        data = JSON.parse(ev.data);
      } catch (e) {
        return;
      }
      if (data.keepalive) return;
      // Any change (including deletion, mtime: null → LR-6) reloads after a
      // 300 ms debounce that coalesces bursts.
      clearTimeout(reloadTimer);
      reloadTimer = setTimeout(() => window.location.reload(), 300);
    };
    // Unlike EventSource, a WebSocket doesn't reconnect itself — retry unless
    // this close was deliberate (es already points elsewhere / is null).
    sock.onclose = () => {
      if (es !== sock) return;
      es = null;
      clearTimeout(resubscribeTimer);
      resubscribeTimer = setTimeout(resubscribe, 1000);
    };
  }

  function watchPath(p) {
    if (!p || watched.has(p)) return;
    // Never watch mount-backed data files (see mountsRoot): read-only remote
    // bytes don't change, and the poll traffic is the mount-killing hazard.
    if (isUnwatchable(p)) return;
    watched.add(p);
    if (!autoReloadEnabled || !started) return; // before start, paths just accumulate
    // Debounce resubscribe so a page firing several runPython calls on load
    // reconnects once (LR-4).
    clearTimeout(resubscribeTimer);
    resubscribeTimer = setTimeout(resubscribe, 100);
  }

  function autoReload(enabled) {
    autoReloadEnabled = !!enabled;
    if (!autoReloadEnabled) {
      clearTimeout(resubscribeTimer);
      clearTimeout(reloadTimer);
      if (es) {
        es.close();
        es = null;
      }
    } else if (started) {
      resubscribe();
    }
  }

  function startAutoReload() {
    started = true;
    // Learn the mounts root before opening any socket, so a mount-backed
    // _file is never watched even for the first subscribe. The `path` template
    // and any `_file` are added here (LR-1); watchPath callers (runPython's
    // resolved_py, template code) come later and are always local, so they're
    // safe even if this fetch is still in flight. On fetch failure we keep the
    // prior behavior (watch everything) — the server-side registry (items 1-4)
    // already makes a mount stat non-fatal, so this is defense in depth.
    const begin = () => {
      // Drop anything mount-backed that accumulated before we knew the root.
      for (const p of [...watched]) {
        if (isUnwatchable(p)) watched.delete(p);
      }
      const params = new URLSearchParams(window.location.search);
      const own = params.get("path");
      if (own && !isUnwatchable(own)) watched.add(own);
      const file = params.get("_file");
      if (file && !isUnwatchable(file)) watched.add(file);
      if (autoReloadEnabled) resubscribe();
    };
    // loadConfig's own promise chain does the mountsRoot/callsDir/callsSuffix/
    // sidecarRoot assignment now (so a template calling fused.sidecarPath()
    // before this ever runs still gets it) — this just waits on it. loadConfig
    // never rejects (its own .catch(() => ({})) absorbs a fetch failure).
    loadConfig().then(begin);
  }

  window.fused = {
    // Runtime identity: "local" here (the fused-render app). The hosted/exported
    // runtime sets "hosted", so a page can branch on where it runs (EXPORT.md).
    env: "local",
    runPython,
    rawUrl,
    stat,
    readFile,
    writeFile,
    uploadFile,
    mkdir,
    ai,
    autoReload,
    params: { get, getAll, set, onChange },
  };

  // Internal-only sidecar bridge for built-in templates — deliberately not on
  // `window.fused` (see the file header). Not present in the hosted runtime.
  window._fusedSidecarPath = sidecarPath;
  window._fusedTargetPathFromSidecarPath = targetPathFromSidecarPath;

  // Error overlay: shows for unhandled runPython rejections the page didn't
  // catch itself (identified by carrying a `.traceback`).
  function showOverlay(err) {
    const overlay = document.createElement("div");
    overlay.style.cssText = [
      "position:fixed", "inset:0", "z-index:2147483647",
      "background:rgba(20,0,0,0.92)", "color:#ffdede",
      "font-family:ui-monospace,Menlo,Consolas,monospace",
      "font-size:13px", "padding:24px", "overflow:auto",
      "border:4px solid #c0392b", "box-sizing:border-box",
      "white-space:pre-wrap",
    ].join(";");
    const title = document.createElement("div");
    title.style.cssText = "font-size:16px;font-weight:bold;margin-bottom:12px;color:#ff6b6b;";
    title.textContent = `${err.type || "Error"}: ${err.message || ""}`;
    const pre = document.createElement("pre");
    pre.style.cssText = "margin:0;white-space:pre-wrap;word-break:break-word;";
    pre.textContent = err.traceback || "";
    overlay.appendChild(title);
    overlay.appendChild(pre);
    document.body.appendChild(overlay);
  }

  // ---- page-error records (docs/CALL_LOG_DESIGN.md §9.2a) -------------------
  // The call log's most informative record is the one where NO call happened:
  // a page whose JS threw before it ever reached runPython looks, to anyone
  // reading the log, exactly like a page nobody opened. Reporting page-level
  // errors turns "zero calls, cause unknown" into a message and a line number.
  //
  // Capped per page load — a broken render loop can throw thousands of times,
  // and the first few are the diagnosis; the rest are noise that would spend
  // the store's rate budget. Fire-and-forget with a swallowed rejection: a
  // failed report must never itself trigger the unhandledrejection path.
  const PAGE_ERROR_CAP = 5;
  let pageErrorsSent = 0;

  function reportPageError(fields) {
    if (pageErrorsSent >= PAGE_ERROR_CAP) return;
    pageErrorsSent += 1;
    const page = ownQuery("path");
    if (!page) return; // not a rendered page (no attribution to record it under)
    try {
      fetch("/api/calls/event", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Fused": "1" },
        body: JSON.stringify(
          Object.assign({ kind: "page-error", page: page, target_file: ownQuery("_file") }, fields)
        ),
        keepalive: true,
      }).catch(() => {});
    } catch (e) {
      /* reporting is best-effort; never let it break the page */
    }
  }

  window.addEventListener("error", (event) => {
    // Uncaught synchronous errors. A resource-load failure (a bad <img> src)
    // also fires this event but carries no `error` object and targets an
    // element rather than the window — skip those: they are not the page's
    // code failing, and they would drown the real ones.
    if (!event || event.target !== window) return;
    const err = event.error;
    reportPageError({
      type: (err && err.name) || "Error",
      message: (err && err.message) || event.message || "uncaught error",
      source: event.filename || null,
      line: typeof event.lineno === "number" ? event.lineno : null,
      col: typeof event.colno === "number" ? event.colno : null,
      stack: (err && err.stack) || null,
    });
  });

  window.addEventListener("unhandledrejection", (event) => {
    const err = event.reason;
    // A superseded/aborted runPython (D113) rejects with a benign AbortError:
    // swallow it so a fire-and-forget re-render that lost the race neither shows
    // the overlay nor logs an "uncaught (in promise)" to the console.
    if (err && err.name === "AbortError") {
      event.preventDefault();
      return;
    }
    if (err && err.traceback) {
      showOverlay(err);
      // A runPython failure the page didn't catch: the server already recorded
      // it against the /api/run call (with the real traceback), so recording it
      // again here would double-count the same failure.
      return;
    }
    reportPageError({
      type: (err && err.name) || "UnhandledRejection",
      message: (err && err.message) || String(err),
      stack: (err && err.stack) || null,
    });
  });

  // Start watching after inline page scripts have run, so an opt-out via
  // fused.autoReload(false) (e.g. the code editor) wins the race (LR-5).
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", startAutoReload);
  } else {
    startAutoReload();
  }
})();
