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
 *     opts.history: prior [{role:"user"|"assistant", content}] turns, for a
 *     caller holding a conversation rather than asking one question.
 *     opts.raw: send the prompt verbatim, with no chat template around it.
 *     opts.temperature / opts.maxTokens / opts.topP: sampling.
 *     All four are LOCAL-MODEL ONLY and are refused (400) rather than dropped
 *     on the Claude path. fused.ai.cancel() stops a local generation mid-flight
 *     without unloading the model.
 *     Ask an AI model via the shell's /api/ai, which runs the local claude
 *     (Claude Code) CLI. Resolves with exactly {text: string, model: full model
 *     id that ran, usage: {input_tokens, output_tokens} | null} — Anthropic-style
 *     usage names, NOT OpenAI's prompt_tokens/completion_tokens. opts:
 *     systemPrompt, model, effort ("low"|"medium"|"high"|"xhigh"),
 *     onChunk. Local-only — not available on hosted/exported pages.
 *   fused.ai.models.list() / catalog() / load(id) / download(id) / unload(id)
 *     Local inference (SPEC §40): what this machine is holding in memory and
 *     what it costs. load/download return {jobId} — a cold load is a multi-GB
 *     download, so nothing waits on it; watch it with fused.watchJob(jobId). To
 *     GENERATE TEXT with a local model there is no new call: pass its repo id
 *     as fused.ai(prompt, {model: "org/name"}).
 *   fused.ai.image({prompt, model, width, height, steps, guidance, seed,
 *                   onProgress}) -> Promise<{path, url, seed, ...}>
 *     Text to image, locally (SPEC AI-9). Resolves with the PNG's path and a
 *     ready-made /api/fs/raw url to point an <img> at, plus the seed that was
 *     used — invented server-side when you don't pass one, so a render is
 *     always repeatable. Minutes long: onProgress fires per denoising step with
 *     the download-manager record, and that row's ✕ really stops it. Rejects
 *     with .type "cancelled" | "ai_error" | "unavailable" (no image runner on
 *     this machine — the reason is in the message).
 *   fused.watchJob(id) -> {get, watch, stop, cancel}
 *     Observe a job this page did NOT create — the server-owned work that
 *     fused.ai.models.load() and image generation start. The read side of the
 *     download manager; trackJob below is the write side. TRACK is "I am doing
 *     this and reporting it" (takes a spec, creates a row); WATCH is "someone
 *     else is doing it and I am looking" (takes an id).
 *   fused.trackJob(spec) -> handle {update, finish, fail, cancelled, cancelRequested}
 *     Report a long-running operation THIS PAGE is running to the shell's
 *     download manager, so it stays visible after the page that started it is
 *     navigated away from (SPEC §36, D244). Model downloads used to be the
 *     motivating example; they are the server's job now (SPEC §40) and a page
 *     observes them with fused.job() instead of reporting them. Every
 *     method is fire-and-forget and never rejects — reporting is decoration and
 *     must not be able to break the work it describes. A no-op stub on a
 *     hosted page (there is no manager there), so a view that reports progress
 *     still exports.
 *   fused.index.* -> the file index, without hand-rolling fetch()
 *     stats({root}) / lookup({q, limit, offset, sort}) / search({root, q, limit})
 *     / query({sql, limit}) / status() / scan({root, full}) / cancel({runId})
 *     / config.get() / config.set({roots, ignore}) / repos()
 *     Every one resolves with the endpoint's own payload PLUS a normalized
 *     `ready: {indexed, scanning, stale, reason}`. That envelope is the point:
 *     an index query answers zero rows both when nothing matches and when no
 *     index was ever built, and rendering the second as the first is a silent
 *     lie. A `ready` field is null only when that response genuinely cannot say
 *     — `search` alone reports `scanning: null`, being the per-keystroke path
 *     that must not double its request count. `reason` is "no-index",
 *     "outdated", "not-covered" or null. Writes (scan/cancel/config.set) carry
 *     the X-Fused header for you. LOCAL ONLY — a hosted page has no index.
 *     Two routes are deliberately NOT wrapped, and both stay reachable by raw
 *     fetch for a caller that truly means it: /api/index/delete, because
 *     wiping the user's whole index is not something a page should do on load,
 *     and /api/index/ask, because it spends AI credits per call and belongs
 *     behind an explicit shell-level action rather than an app's render path.
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
 * It also carries the appearance theme into view documents — as `data-theme`
 * for the ones that opt in, and as `color-scheme` (browser defaults only) for
 * the ones that don't — see the theme block at the top of the IIFE
 * (SPEC §30, D134).
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
    if (!root) return;
    // Two kinds of document, one resolved theme:
    //
    //   OPTED IN (`data-fused-theme`) — the attribute goes on, its own
    //   `:root[data-theme="light"]` block does the rest. Its CSS owns every
    //   colour it paints.
    //
    //   NOT OPTED IN — a user-authored view, or a built-in not yet converted.
    //   Its CSS still stays entirely its own; what it gets is `color-scheme`,
    //   which changes no author colour at all — it only tells the browser
    //   which set of DEFAULTS to use. That matters because the shell paints
    //   its frames on its own backdrop (styles/base.css), so a document that
    //   sets no background of its own is transparent over a dark surface while
    //   its unstyled text stays UA black. Canvas and default text colour are a
    //   pair and have to flip together: with color-scheme set, the browser
    //   supplies a dark canvas AND light text, and the page reads exactly as
    //   it does standalone in a dark-mode browser. A page that DOES set its
    //   own background is unaffected — author colours always win.
    // `color-scheme` goes on EVERY document, opted in or not. It changes no
    // author colour — it only picks which set of browser defaults applies —
    // and it is the only thing that can paint the canvas correctly before the
    // document's own stylesheet has been parsed, which is exactly the window
    // the white flash lived in. (This script is parser-blocking at the top of
    // <head>, so "before" here means before anything else in the document.)
    var optedIn = root.hasAttribute("data-fused-theme");
    var apply = function () {
      var theme = resolvedTheme();
      root.style.colorScheme = theme;
      if (optedIn) root.setAttribute("data-theme", theme);
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

  // --- The pane focus contract (`_nofocus=1`) -------------------------------
  //
  // A page rendered in the explorer's PREVIEW PANE must not take the keyboard.
  // The pane is a same-origin iframe, so an `autofocus` attribute — or any
  // el.focus() in a boot path — pulls document focus out of the shell, and the
  // listing's arrow keys stand down the moment focus leaves it: opening a
  // preview stopped you browsing file to file from the keyboard.
  //
  // This lives HERE, in the script injected into every rendered page, rather
  // than in the one template that happened to surface the bug — that template
  // was not special, and the next one with an input would have re-broken it.
  // The shell marks the frame's URL and every page gets the behaviour for free:
  //
  //   • `autofocus` attributes are stripped as the document parses, before the
  //     browser has finished parsing (and so before it applies the last one);
  //   • el.focus() calls are DROPPED until the reader actually interacts with
  //     the page — the whole boot path, however long its async tail, rather
  //     than some guessed settle window;
  //   • the first real user gesture in the document lifts both, permanently:
  //     from then on focus() works exactly as written. Clicking into the pane
  //     is a deliberate act and the page owns the keyboard after it.
  //
  // `window.__fusedNoAutofocus` is published for pages that would rather ask
  // than be corrected (the claude template gates its own boot focus on it).
  // Deliberately not on `window.fused`: that is the documented portable bridge
  // mirrored by the hosted runtime, and this is local-shell plumbing — same
  // reason `_fusedSidecarPath` is a bare global.
  //
  // The param name is mirrored in frontend/src/apps/explorer/listing/
  // frame-focus.ts, which is where the contract is written down; the shell-side
  // guard there is what covers frames this suppression cannot reach.
  var NO_FOCUS_PARAM = "_nofocus";

  function noFocusRequested(search) {
    try {
      return new URLSearchParams(String(search).replace(/^\?/, "")).get(NO_FOCUS_PARAM) === "1";
    } catch (e) {
      return false;
    }
  }

  function startNoFocus() {
    if (!noFocusRequested(location.search)) return;
    window.__fusedNoAutofocus = true;

    // Anything that manages to take focus anyway gives it straight back. This
    // is the one that catches `autofocus`, which cannot be beaten by stripping
    // the attribute: the browser queues the CANDIDATE when the element is
    // inserted, so removing the attribute afterwards does not dequeue it.
    // Capture, so it runs before the page's own focus handlers.
    //
    // The shell blurs the FRAME as well (its focus guard) — that is what
    // actually returns the keyboard to the listing, since focus on an element
    // in here leaves the embedder's activeElement on the iframe either way.
    // This half is what stops the caret sitting in a composer the reader never
    // put it in.
    var bounceFocus = function (e) {
      var el = e.target;
      if (el && typeof el.blur === "function") el.blur();
    };
    document.addEventListener("focusin", bounceFocus, true);

    // Strip `autofocus` from anything already parsed and anything that arrives
    // while the document streams. Belt and braces beside the blur above — an
    // attribute that never applies is one fewer focus flicker. The observer is
    // disconnected at DOMContentLoaded: after that the attribute has no effect
    // on its own, and the focus() suppression below covers a script that adds
    // one and focuses.
    var strip = function (root) {
      var nodes = root.querySelectorAll ? root.querySelectorAll("[autofocus]") : [];
      for (var i = 0; i < nodes.length; i++) nodes[i].removeAttribute("autofocus");
    };
    strip(document);
    var observer = null;
    try {
      observer = new MutationObserver(function () {
        strip(document);
      });
      observer.observe(document.documentElement, { childList: true, subtree: true });
    } catch (e) {
      /* no MutationObserver — the initial strip and the guard below still hold */
    }

    // Suppress programmatic focus until the reader touches the page. Patched on
    // the prototype rather than per element because the point is to cover code
    // that has not been written yet; restored — not left wrapped — on the first
    // gesture, so nothing keeps paying for this once it stops applying.
    var realFocus = HTMLElement.prototype.focus;
    HTMLElement.prototype.focus = function () {
      /* embedded in the preview pane, and the reader hasn't asked: ignore */
    };
    var released = false;
    var release = function () {
      if (released) return;
      released = true;
      HTMLElement.prototype.focus = realFocus;
      window.__fusedNoAutofocus = false;
      if (observer) observer.disconnect();
      // Every part of the suppression lifts at once, this one included — a
      // focusin bounce left installed would make the page permanently
      // unfocusable for the reader who just clicked into it.
      document.removeEventListener("focusin", bounceFocus, true);
      document.removeEventListener("pointerdown", release, true);
    };
    // POINTER only, and deliberately not keydown: a key reaching an embedded
    // preview means focus LEAKED into it, not that the reader aimed at it, and
    // lifting the suppression there let the page take the keyboard for good on
    // the very keystroke the shell was about to rescue. A reader who really is
    // driving this page with the keyboard got here by Tab, and the shell lifts
    // the suppression for that (window.__fusedReleaseNoFocus below).
    document.addEventListener("pointerdown", release, true);
    // The same release, reachable from the EMBEDDER. Some deliberate acts are
    // invisible in here: a reader tabbing into the pane presses Tab in the
    // SHELL's document, so this page never sees a keydown and would bounce the
    // focus it was just deliberately given — the two halves of the contract
    // contradicting each other. The shell calls this the moment it recognises
    // such an act (see usePaneFocusGuard). An app-internal global, not part of
    // `fused`, for the same reason `__fusedFlushEdits` is.
    window.__fusedReleaseNoFocus = release;
    document.addEventListener("DOMContentLoaded", function () {
      if (observer) observer.disconnect();
    });
  }

  startNoFocus();

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

  // ---- the project-venv install loader (SPEC PY-16, PY-18) ------------------
  //
  // Most .py files run on the app's own interpreter and install nothing. A file
  // whose FOLDER declares dependencies the app doesn't ship (geotiff's
  // imagecodecs, pano's py360convert…) needs a one-time download, and /api/run
  // answers `needs_install` rather than blocking past runPython's ~30s budget.
  // Handled HERE, in the shell, so every template gets it without a line of its
  // own code.
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
  // declaration the app interpreter already satisfies installs NOTHING (see engine.py's
  // `app_satisfies`), the installs that remain are either genuinely long (a real
  // download, where 600ms of delay is imperceptible) or genuinely short (a warm uv
  // cache, where the modal was pure noise). Delay separates the two without having
  // to predict which one this is.
  const INSTALL_MOUNT_DELAY_MS = 600;

  let installUi = null;
  // Live installs, as key -> { row, count }.
  //
  // One ROW per key — its own title, detail, bar and Cancel. Concurrent installs
  // with DISTINCT keys are real: a page can call scripts from two different
  // projects, and the D214 interpreter round reports under its own key. One
  // shared set of nodes made those illegible: the title named whichever install
  // started last, N pollers rewrote one detail line at 2Hz, and one Cancel button
  // carried N listeners, so a single click cancelled every install.
  //
  // The count is what lets the row outlive an individual waiter. It is normally 1
  // now, because `installEnv` dedups by key before `showInstall` is ever reached
  // (SPEC PY-16 makes five scripts in one folder ONE key, and they join one
  // promise rather than each opening a row). It is kept rather than removed
  // because the invariant it encodes — the row goes when the LAST waiter settles,
  // never when the first does — is the one that has to hold if anything ever
  // reaches `showInstall` twice for a key, and it costs a single integer. Living
  // inside the entry means "which row" and "how many waiters" are one piece of
  // state that cannot disagree with itself.
  const installing = new Map();

  // The indeterminate bar (D213). The worker parks at pct 25 for the WHOLE download
  // — `uv sync` runs behind captured output, so there is no
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
  //
  // `installed` belongs to ONE `runPython` call, and that lifetime is the whole
  // correctness argument — a page-scoped set was tried and broke the case this
  // flow exists for. The question here is "did THIS chain already install that key
  // and get told to install it again", which is a loop. Widened to the page it
  // answers a different question badly, because the key is now the project folder
  // (PY-16) and every script in the folder shares it:
  //
  //   * FIVE CONCURRENT SCRIPTS. All five /api/run's are answered from the same
  //     pre-install snapshot. The first response installs and records the key; the
  //     other four then read it as already-attempted, fall through to the
  //     `!data.ok` branch, and reject with the raw "declares dependencies that are
  //     not installed yet" text. The multi-script case failed precisely because it
  //     was multi-script.
  //   * A LATE RESPONSE. A call answered before the install began but delivered
  //     after it finished is not a loop — it has not run at all yet — and must
  //     re-attempt rather than report a stale snapshot as a failure.
  //   * A MANIFEST EDIT. The key used to be derived from the requirement set, so
  //     editing dependencies minted a NEW key and a legitimate second install. It
  //     is stable per project now, so a page-scoped set refused the install for a
  //     user who had just fixed their `pyproject.toml`.
  //
  // Deduplication — the actual "one install per page, not one per script" — lives
  // in `installEnv`'s `installInFlight` registry instead, which is the right
  // mechanism for it: concurrent callers JOIN one promise, so there is one POST,
  // one poller, one row and one download however many scripts wait. What a
  // page-scoped set bought on top of that was only collapsing N error messages
  // into one for a genuinely stuck install, and that is not worth failing the
  // healthy path for.
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
    // Name what is actually being prepared. The environment belongs to the
    // PROJECT (SPEC PY-16), and every script in it waits on this one row, so the
    // row is titled with the project — "Preparing my-app" — rather than with a
    // joined package list that would (a) grow unbounded as a folder gains
    // dependencies and (b) imply the row belongs to one script. The packages are
    // demoted to the detail line, where the poller's own text takes over a beat
    // later anyway.
    //
    // On the interpreter round (D214) the packages are NOT downloading yet, and
    // titling that round with them is the kind of small lie that makes a
    // four-minute wait feel broken — the user watches "Installing tensorflow"
    // and nothing about tensorflow is happening. So that round keeps its own
    // distinct title.
    const requirements = (need.requirements || []).join(", ");
    row.title.textContent = need.python
      ? "Installing Python " + need.python
      : "Preparing " + (need.name || "the environment");
    // Deliberately NOT "starting…" at 0%. `/api/env/install` JOINS an install
    // already in flight rather than duplicating it, so re-opening a page whose
    // download is four minutes old used to paint 0% and then jump to 25% on the
    // first poll — nothing was lost, but a user switching between apps saw
    // 0% → 25% → freeze over and over and concluded it was looping. The initial
    // state therefore asserts no percentage at all: indeterminate until the
    // server's own record arrives (installEnv paints the POST response, which
    // carries it), so the first honest paint is the only paint.
    row.detail.textContent = requirements && !need.python
      ? "contacting the installer… (" + requirements + ")"
      : "contacting the installer…";
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

  // Installs this page has in flight, as key -> Promise. See `installEnv`.
  const installInFlight = new Map();

  // Run the install to completion. Resolves when the venv is ready; rejects with
  // the installer's VERBATIM message otherwise — a resolver failure ("no wheels
  // with a matching platform tag for imagecodecs") is the actual answer the user
  // needs, and rewriting it into something friendlier is what made this opaque
  // in the first place.
  //
  // Deduplicated per key: a page calling five scripts from one project resolves
  // to ONE key (SPEC PY-16), and every caller after the first joins the promise
  // already running instead of starting its own chain. Without this each caller
  // built its own `activeKey`, its own poller and its own cancel listener against
  // a SHARED row — five POSTs to /api/env/install, five pollers hitting
  // /api/env/progress at 2Hz apiece, and five listeners on one Cancel button, so
  // one click fired five cancels and each chain's message overwrote the others'.
  //
  // The server was never at risk of doing the work twice — `start()` claims the
  // key atomically and joins an install already in flight — which is exactly why
  // the fix belongs here: the duplication is N requests and N timers originating
  // on the client, not N `uv sync` runs. A second locking layer server-side would
  // duplicate a mechanism that already works.
  //
  // The entry is removed when the promise SETTLES, not when a caller consumes it,
  // so a later run (a retry after a fixed pyproject.toml, a `watchPath` reload)
  // starts a fresh install rather than replaying a stale result. Rejections are
  // shared too: every waiter gets the installer's verbatim error, and each one's
  // `.catch` attaches before the promise can reject unhandled because the
  // registry is written synchronously with the chain that fills it.
  function installEnv(need, pyPath, ownPath) {
    const joined = installInFlight.get(need.key);
    if (joined) return joined;
    const promise = startInstall(need, pyPath, ownPath);
    installInFlight.set(need.key, promise);
    const forget = () => {
      if (installInFlight.get(need.key) === promise) installInFlight.delete(need.key);
    };
    // `.then(f, f)` rather than `.finally`: `finally` returns a NEW promise that
    // re-raises, and if nothing were attached to that one a shared rejection
    // would surface as an unhandled rejection in the console on top of the error
    // the caller is already showing. This variant settles the bookkeeping without
    // creating a second chain for anyone to have to handle.
    promise.then(forget, forget);
    return promise;
  }

  function startInstall(need, pyPath, ownPath) {
    const row = showInstall(need);
    let cancelled = false;
    // The key to poll and to cancel is the INSTALLER's, not the pre-flight's.
    // /api/env/install re-derives the project from the .py on disk and returns its
    // own key, and editing a folder's pyproject.toml and letting live-reload
    // re-run it is this app's core workflow — so the answer really can change
    // between /api/run's needs_install and this POST. Polling the stale key then reads a null
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
    //
    // The set is created per call, not per page — see `shouldInstall` for the
    // three ways a page-scoped one failed the healthy multi-script path.
    const handle = (data, installed) => {
      if (data.stdout) {
        console.log("[python]", data.stdout);
      }
      // Watch the executed file for auto-reload, even on failure (LR-2): a
      // broken py that gets fixed must still trigger a reload. Read before
      // the ok check so it's recorded either way.
      if (data.resolved_py) watchPath(data.resolved_py);
      // Watch the project's MANIFEST too, so fixing a dependency reloads the page
      // the same way fixing the .py does. Without it the only feedback for "I
      // added the package it asked for" is the same error overlay, still up, with
      // nothing saying a reload is needed. Server-supplied (engine.py puts it in
      // `needs_install`) rather than joined client-side, because the project root
      // is the server's answer and the path separator is the server's platform.
      if (data.needs_install && data.needs_install.pyproject) {
        watchPath(data.needs_install.pyproject);
      }
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
    // Prior turns, for a caller holding a conversation. `prompt` stays the
    // thing being asked NOW, so adding history changes no existing call.
    // Local models only — the Claude path is one invocation with no
    // conversation to resume, and says so rather than answering a follow-up as
    // if it were the first question.
    if (opts.history !== undefined) body.history = opts.history;
    // Raw continuation — the text goes to the model verbatim, with no chat
    // template around it. A base model continuing your paragraph, rather than
    // an assistant answering it. Local models only, for the same reason as
    // history: only something that OWNS the chat template can decline to apply
    // one, so the Claude path refuses this rather than quietly ignoring it.
    if (opts.raw !== undefined) body.raw = opts.raw;
    // Sampling. Local models only, like history and raw — the Claude CLI
    // exposes no sampling knobs, so these are refused there rather than
    // dropped. camelCase in, snake_case on the wire, because the wire shape is
    // the worker's and every other runtime option makes the same trip
    // (systemPrompt -> system_prompt).
    if (opts.temperature !== undefined) body.temperature = opts.temperature;
    if (opts.maxTokens !== undefined) body.max_tokens = opts.maxTokens;
    if (opts.topP !== undefined) body.top_p = opts.topP;
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
      // A local model that is not resident yet answers 409 with the id of the
      // load this call just started (SPEC AI-5). Carrying it through is what
      // lets a caller show that download rather than just reporting a failure.
      if (error && error.jobId) err.jobId = error.jobId;
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

  // ---- background jobs / the download manager (SPEC §36, D244) --------------
  //
  // A page that starts work outliving the call that started it — a model
  // download, a checkpoint pull, a generation running for minutes — reports it
  // here, and the shell draws ONE download manager for every page's work at the
  // foot of the notification stack. Before this, each such page drew its own
  // progress bar inside itself, so an 8GB download became invisible the moment
  // you navigated away from the page that started it.
  //
  //   const job = fused.trackJob({ title: "FLUX.2-klein-4B", kind: "download",
  //                           unit: "bytes", cancellable: true });
  //   job.update({ done: 1.2e9, total: 8.1e9, detail: "transformer.gguf" });
  //   if (job.cancelRequested) stopTheWork();      // set from the manager's ✕
  //   job.finish("Downloaded");                    // or .fail(err) / .cancelled()
  //
  // Reporting is DECORATION, never the work itself: every method here is
  // fire-and-forget and no rejection escapes. A page whose progress reports all
  // fail must still download the model — so a failed POST is swallowed, and
  // `update()` resolves with the last record it managed to store rather than
  // rejecting. The one thing a caller reads back is `cancelRequested`.
  //
  // Cancellation is a REQUEST the page honors, not something the shell can do:
  // only the page knows what "stop" means for its work (the examples call their
  // own `action: "cancel"` data-file entry point). The manager's ✕ sets the
  // flag; the page sees it in the reply to the tick it was going to send
  // anyway, which is why `update()` resolves with the record.
  const JOB_STATE_RUNNING = "running";

  function newJobId() {
    // Page-scoped uniqueness is not enough — two tabs on the same page report
    // into one registry — so this is a random token, not a counter.
    return "j" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
  }

  // Nudge every other same-origin document that the job list moved, so the
  // shell's manager appears at the instant a download starts rather than on its
  // next idle poll. The `storage` event fires in every same-origin browsing
  // context EXCEPT the one that wrote — which is exactly the shape here (an
  // iframe writes, the shell listens), and the same mechanism the appearance
  // theme already converges through. Purely an optimisation: the shell polls
  // /api/jobs regardless, so a browser that drops the event (or a Python worker
  // reporting straight to the API, which runs no JS at all) is only slower to
  // notice, never wrong. Must stay in sync with frontend's lib/jobs.ts.
  const JOB_PING_KEY = "fused-render:jobs-ping";

  function pingJobs() {
    try {
      localStorage.setItem(JOB_PING_KEY, String(Date.now()));
    } catch (e) {
      /* private mode / disabled storage — the shell's poll still covers it */
    }
  }

  function postJob(body, onReject) {
    return fetch("/api/jobs", {
      method: "POST",
      headers: callHeaders({ "Content-Type": "application/json", "X-Fused": "1" }),
      body: JSON.stringify(body),
      // The last report of a page being torn down is the one that matters most
      // (it is what turns a row from "running" into "cancelled" rather than
      // leaving it to time out as stalled), and that report is issued from an
      // unload path where an ordinary fetch is killed with the document.
      keepalive: true,
    })
      .then((res) =>
        res.json().then((data) => {
          if (res.ok) return data;
          // A REJECTED report (a malformed id, a missing title) is an authoring
          // mistake, and silence would leave the author with a page that works
          // and a manager that never shows it. Said once per job — a poll loop
          // would otherwise print the same line every 1.5s — and still not
          // thrown: a bad progress report must not break the download.
          onReject((data && data.error) || "HTTP " + res.status);
          return null;
        })
      )
      // A transport failure (the app quitting under a page mid-report) is not
      // an authoring mistake and stays quiet.
      .catch(() => null);
  }

  function trackJob(spec) {
    spec = spec || {};
    const id = spec.id ? String(spec.id) : newJobId();
    // Mirrors the last record the server confirmed, so `cancelRequested` and
    // `state` can be read synchronously between ticks.
    let last = null;
    let settled = false;
    let warned = false;
    // Reports are SERIALIZED, not just fired: they are deltas (only the keys
    // present are applied), so two in flight at once can land out of order and
    // an older `done` can overwrite a newer one — a bar that walks backwards.
    // Chaining costs nothing at a 1.5s tick and removes the race by
    // construction.
    let chain = Promise.resolve(null);

    function warnOnce(message) {
      if (warned) return;
      warned = true;
      console.warn("fused.trackJob(" + JSON.stringify(id) + "): " + message);
    }

    function send(fields) {
      // A settled job stops reporting. Without this a poll loop that runs one
      // extra tick after it saw "done" would flip the row back to running and
      // the manager would show a finished download as live again.
      if (settled && fields.state === undefined) return chain;
      const body = Object.assign({ id: id }, fields);
      chain = chain
        .then(() => postJob(body, warnOnce))
        .then((record) => {
          if (record && record.id === id) last = record;
          pingJobs();
          return last;
        });
      return chain;
    }

    const handle = {
      id: id,
      // Fire-and-forget by design; awaiting is optional and only ever needed to
      // read `cancelRequested` at a specific point.
      update: function (fields) {
        return send(fields || {});
      },
      finish: function (detail) {
        settled = true;
        return send({ state: "done", detail: detail === undefined ? "" : detail });
      },
      fail: function (message) {
        settled = true;
        // An Error, a rejected runPython (`.message` + `.traceback`), or a
        // plain string all reach here — take the most useful text of each
        // rather than stringifying an object into "[object Object]".
        const text =
          message && message.message
            ? message.message
            : message === undefined || message === null
              ? "failed"
              : String(message);
        return send({ state: "error", message: text });
      },
      cancelled: function () {
        settled = true;
        return send({ state: "cancelled" });
      },
    };

    // Read-only views of the last confirmed record. Properties rather than
    // methods because they are a value the page checks in a poll loop
    // (`if (job.cancelRequested) …`), and a getter cannot go stale the way a
    // copied boolean would.
    Object.defineProperty(handle, "cancelRequested", {
      get: function () {
        return !!(last && last.cancel_requested);
      },
    });
    Object.defineProperty(handle, "state", {
      get: function () {
        return last ? last.state : JOB_STATE_RUNNING;
      },
    });

    // The opening report. Everything the page passed except `id`, which is the
    // handle's own; `title` is required by the registry and a job with no title
    // is a row nobody can read, so name it here rather than let the first tick
    // fail server-side.
    send({
      title: spec.title || "Working…",
      detail: spec.detail || "",
      kind: spec.kind || "task",
      unit: spec.unit || "",
      done: spec.done === undefined ? null : spec.done,
      total: spec.total === undefined ? null : spec.total,
      cancellable: !!spec.cancellable,
      state: JOB_STATE_RUNNING,
    });

    return handle;
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

  // ---------------------------------------------------------------- local models
  //
  // fused.ai.models — the lifecycle half of the AI API (SPEC §40). Generation
  // itself needs nothing new: fused.ai(prompt, {model: "org/name", onChunk})
  // already reaches a local model, because a model id with a slash in it IS a
  // Hugging Face repo id and the server routes on that.
  //
  // What DOES need an API is everything around it — a model is a resident
  // process here, not a request to somebody else's datacentre, so a page can ask
  // what is loaded, put something in memory, and give the memory back.
  //
  // load() and download() return a JOB, not a finished model: a cold load is a
  // multi-GB download and nothing waits on it. Watch it with fused.job(id).
  async function aiPost(path, body) {
    const res = await fetch(path, {
      method: "POST",
      headers: callHeaders({ "Content-Type": "application/json", "X-Fused": "1" }),
      body: JSON.stringify(body || {}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error((data && data.error) || res.statusText);
      err.type = res.status === 409 ? "unavailable" : "bad_request";
      throw err;
    }
    return data;
  }

  // fused.ai.image({prompt, ...}) -> Promise<{path, url, seed, ...}>
  //
  // The one call in this bridge that RESOLVES WITH A FILE. Text streams, so
  // fused.ai hands back words; an image is an artefact, so this hands back
  // somewhere to point an <img> at — `url` is already the /api/fs/raw address,
  // because every page that calls this would otherwise write the same line.
  //
  // Minutes, not seconds. The server answers immediately with a job id, and
  // this waits on it: `onProgress(job)` fires per tick with the record the
  // download manager is drawing (done/total are DENOISING STEPS), and the same
  // row's ✕ really stops the render — the work is the server's, not the page's.
  //
  // The seed comes back whether or not one was passed, so "make that one again"
  // is always a call away.
  function aiImage(opts) {
    opts = opts || {};
    if (typeof opts.prompt !== "string" || !opts.prompt.trim()) {
      const err = new Error("fused.ai.image({prompt}): prompt must be a non-empty string");
      err.type = "bad_request";
      return Promise.reject(err);
    }
    const onProgress = typeof opts.onProgress === "function" ? opts.onProgress : null;
    const body = {};
    for (const key of ["prompt", "model", "width", "height", "steps", "guidance", "seed"]) {
      if (opts[key] !== undefined) body[key] = opts[key];
    }
    return aiPost("/api/ai/image", body).then((started) => {
      const watcher = watchJob(started.jobId);
      const done = () => ({ ...started, url: rawUrl(started.path) });
      return watcher.watch(onProgress).then((record) => {
        if (!record) {
          // The row aged out from under us — a backgrounded tab can sleep past
          // its retention on a render this long. The FILE is the other witness,
          // and the one that actually matters.
          return stat(started.path).then(done, () => {
            const err = new Error("the image job is no longer being reported");
            err.type = "ai_error";
            err.jobId = started.jobId;
            throw err;
          });
        }
        if (record.state === "done") return done();
        const err = new Error(
          record.state === "cancelled"
            ? "the image was cancelled"
            : record.message || "the image failed to render",
        );
        err.type = record.state === "cancelled" ? "cancelled" : "ai_error";
        err.jobId = started.jobId;
        throw err;
      });
    });
  }

  const aiModels = {
    list: () => fetch("/api/ai/runtime", { headers: callHeaders({}) }).then((r) => r.json()),
    catalog: () => fetch("/api/ai/catalog", { headers: callHeaders({}) }).then((r) => r.json()),
    load: (model, opts) =>
      aiPost("/api/ai/runtime/load", { model, ...(opts || {}) }),
    download: (model, opts) =>
      aiPost("/api/ai/runtime/download", { model, ...(opts || {}) }),
    // Either a model id or `{capability}`. A page holding an Unload button
    // usually means "release whatever is resident", and it does NOT reliably
    // know which model that is — the one loaded may not be the one its dropdown
    // is showing (another page, or the AI Models page, can load a different
    // one). Passing the selected id there unloads nothing and leaves the real
    // resident model in memory, so the capability form is the honest one.
    unload: (model) =>
      aiPost("/api/ai/runtime/unload",
             typeof model === "string" || model == null
               ? { model } : { capability: model.capability }),
  };
  ai.models = aiModels;
  ai.image = aiImage;
  // Stop the generation in flight on a local model, keeping it loaded — the
  // next message answers straight away. Resolves false when there was nothing
  // to stop, which is not an error: a Stop pressed as the last token lands
  // should be a no-op.
  ai.cancel = (capability) =>
    aiPost("/api/ai/cancel", capability ? { capability } : {}).then((r) => !!r.cancelled);

  // -------------------------------------------------------------- fused.watchJob
  //
  // The READ side of the download manager, and the half trackJob never had.
  //
  // Named as trackJob's sibling on purpose (D244 named that one `trackJob` and
  // not `job` because a bare `fused.job(...)` reads as the job itself rather
  // than as a handle). The pair is the distinction: TRACK takes a spec and
  // creates a row for work this page runs; WATCH takes an id and observes work
  // it did not start.
  //
  // trackJob is for work THIS PAGE runs: it writes progress and reads back a
  // cancel REQUEST it then honours itself. But a page can now start work the
  // SERVER runs — a model load, an image generation — and it has every reason to
  // show that progress inline and offer a ✕ for it. That job is not the page's
  // to report and its cancel is not advisory: the server owns the process and
  // can really stop it.
  //
  // So: observe by id, poll while it lives, cancel for real.
  function watchJob(id) {
    let stopped = false;
    async function get() {
      const res = await fetch("/api/jobs", { headers: callHeaders({}) });
      const data = await res.json().catch(() => ({}));
      return (data.jobs || []).find((j) => j.id === id) || null;
    }
    return {
      get,
      // Resolves when the job reaches a terminal state; calls back on the way.
      // Polling rather than a socket: the manager is already polling, jobs tick
      // about once a second, and a page that navigates away simply stops.
      //
      // Resolves with NULL when the row is gone — either stop() was called, or
      // it was there and vanished. A finished record is dropped after its
      // retention window (SPEC BG-6), which a backgrounded tab can easily sleep
      // straight through on a render that takes minutes; polling forever for a
      // row that is never coming back is a promise that never settles.
      async watch(onUpdate, intervalMs) {
        const every = Math.max(200, intervalMs || 700);
        let seen = false;
        let missing = 0;
        for (;;) {
          if (stopped) return null;
          const record = await get().catch(() => null);
          if (record) {
            seen = true;
            missing = 0;
            if (typeof onUpdate === "function") onUpdate(record);
            if (record.state !== "running") return record;
          } else if (seen && ++missing >= 5) {
            return null;
          }
          await new Promise((r) => setTimeout(r, every));
        }
      },
      stop() {
        stopped = true;
      },
      // A real stop for a server-owned job; for a page-owned one this is the
      // same request trackJob's own reporter reads back off its next tick.
      cancel: () =>
        fetch("/api/jobs/" + encodeURIComponent(id) + "/cancel", {
          method: "POST",
          headers: callHeaders({ "X-Fused": "1" }),
        }).then((r) => r.ok),
    };
  }

  // ------------------------------------------------------------- fused.index
  //
  // The file index, readable from a page without hand-rolling fetch().
  //
  // Every method resolves with the endpoint's own payload PLUS a normalized
  // `ready` object, and that envelope is the reason this exists rather than a
  // convenience wrapper. `/api/index/query` answers `{ok, columns, rows}`: a
  // page that renders its zero rows as an answer cannot tell "nothing matches"
  // from "no index has ever been built", and rendering the second as the first
  // is a silent lie (routers/git_repos.py's docstring calls that "the original
  // silent lie and the reason any of this exists"). So:
  //
  //   ready = { indexed, scanning, stale, reason }
  //
  // `indexed` — a compacted index exists at all. `scanning` — a scan is in
  // flight; independent of indexed, because a rescan keeps serving the last
  // completed generation. `stale` — the answer may be behind the filesystem
  // (a scan running, or a slice built under superseded ignore rules). `reason`
  // — why not ready: "no-index" (nothing built), "outdated" (the rule that
  // would produce these rows never ran), "not-covered" (the index exists but
  // has not visited this root), else null.
  //
  // A field is `null` when THIS response cannot say, never a guess. Where the
  // endpoint already reports readiness it is used (`stats`/`lookup` answer
  // `empty`, `search` answers `covered`/`fresh`/`age_s`, `/api/git-repos`
  // answers the whole triple including `reason`); the rest piggyback one cheap
  // `/api/index/status` GET in parallel. The single exception is `search`,
  // which is the per-keystroke corpus path: doubling its request count to learn
  // `scanning` is a bad trade, so it is the one method that reports
  // `scanning: null`.
  //
  // fused-index:start
  //   Self-contained on purpose: this block reaches only for `fetch` and
  //   `callHeaders`, so tests/test_index_runtime.py can extract it between
  //   these sentinels and run it under node against a stubbed server. Keep it
  //   that way — a DOM or module reference here breaks those tests, which are
  //   the only ones that check behaviour rather than spelling.
  function indexUrl(path, params) {
    const qs = new URLSearchParams();
    Object.keys(params || {}).forEach((key) => {
      const value = params[key];
      // An omitted option must mean "the server's default", not "the empty
      // string": `root=` on /api/index/search is a 400, and `sort=` would
      // silently fall back through the sort allowlist.
      if (value === undefined || value === null || value === "") return;
      qs.set(key, String(value));
    });
    const search = qs.toString();
    return search ? path + "?" + search : path;
  }

  // Three response shapes reach here, and the shell collapses the same three in
  // frontend/src/platform/lib/index-query.ts (`outcomeFrom`): success, a flat
  // {error: "message"}, and the AI relay's nested {error: {type, message}} —
  // which read naively renders as "[object Object]".
  //
  // Rejection style follows runPython and fused.ai rather than inventing a
  // second one in this file: an Error whose `.message` is the server's sentence
  // verbatim (a duckdb "Binder Error: no such column: nope" is the answer the
  // user needs) and whose `.type` is for programs.
  function indexError(status, data) {
    const raw = data && data.error;
    let message = "";
    let type = "";
    if (typeof raw === "string") {
      message = raw;
    } else if (raw && typeof raw === "object") {
      if (typeof raw.message === "string") message = raw.message;
      if (typeof raw.type === "string") type = raw.type;
    }
    const err = new Error(message || "HTTP " + status);
    err.type = type || (status === 403 ? "forbidden" : "bad_request");
    err.status = status;
    return err;
  }

  function indexJson(res) {
    return res
      .json()
      .catch(() => null) // a 500 with an HTML body still has to reject readably
      .then((data) => {
        if (!res.ok) throw indexError(res.status, data);
        // A 2xx carrying `error` should not happen on these routes, but an
        // error rendered as an empty result is the failure this API exists to
        // prevent, so it is refused rather than returned.
        if (data && data.error) throw indexError(res.status, data);
        return data || {};
      });
  }

  function indexGet(path, params) {
    return fetch(indexUrl(path, params), { headers: callHeaders() }).then(indexJson);
  }

  // X-Fused on EVERY post. `_require_fused` (server/common.py) 403s a POST
  // without it — not authentication, a cross-origin-preflight tripwire — and
  // baking it in here keeps the convention in one place instead of teaching
  // every app author to set it by hand.
  function indexPost(path, body) {
    return fetch(path, {
      method: "POST",
      headers: callHeaders({ "Content-Type": "application/json", "X-Fused": "1" }),
      body: JSON.stringify(body || {}),
    }).then(indexJson);
  }

  function indexReady(indexed, scanning, stale, reason) {
    const bit = (v) => (v === null || v === undefined ? null : !!v);
    return { indexed: bit(indexed), scanning: bit(scanning), stale: bit(stale),
             reason: reason || null };
  }

  // The envelope from a /api/index/status payload — the piggybacked source for
  // every call whose own response cannot say.
  //
  // `stale` is `scanning` and nothing more here: /api/index/status exposes no
  // applied-ignore signature, so an index whose slices were built under
  // superseded rules reads as fresh through it. `repos()` is the one method
  // that can see that (git_repos._fresh), and `search()` reports its own age.
  function indexReadyFromStatus(status) {
    const indexed = !!(status && (status.indexed || status.has_index));
    const scanning = !!(status && status.scanning);
    return indexReady(indexed, scanning, scanning, indexed ? null : "no-index");
  }

  // Run `call` and one status GET together, and hand back the payload with an
  // envelope. The probe failing must neither fail the call it describes nor
  // answer for it — an all-null envelope is exactly "this response cannot say".
  function indexWithStatus(call) {
    return Promise.all([
      call,
      indexGet("/api/index/status").then(indexReadyFromStatus,
                                         () => indexReady(null, null, null, null)),
    ]).then(([data, ready]) => Object.assign({}, data, { ready: ready }));
  }

  // `stats` and `lookup` both answer `empty`, from the same manifest read that
  // produced their numbers — so it outranks the probe for `indexed`.
  function indexRefineEmpty(out) {
    if (typeof out.empty !== "boolean") return out;
    out.ready = indexReady(!out.empty, out.ready.scanning, out.ready.stale,
                           out.empty ? "no-index" : null);
    return out;
  }

  function indexStats(opts) {
    opts = opts || {};
    return indexWithStatus(indexGet("/api/index/stats", { root: opts.root }))
      .then(indexRefineEmpty);
  }

  function indexLookup(opts) {
    opts = opts || {};
    return indexWithStatus(indexGet("/api/index/lookup", {
      q: opts.q, limit: opts.limit, offset: opts.offset, sort: opts.sort,
    })).then(indexRefineEmpty);
  }

  function indexSearch(opts) {
    opts = opts || {};
    return indexGet("/api/index/search", {
      root: opts.root, q: opts.q, limit: opts.limit,
    }).then((data) => {
      // `covered: false` deliberately collapses "no index", "not covered" and
      // "a scan is running" into one false, because a search box treats all
      // three the same way (query.search_under). The envelope un-collapses the
      // one a caller actually has to distinguish: a never-built index answers
      // `updated: null`, while a built index that has simply not visited this
      // root answers with its last compaction time.
      const covered = data.covered === true;
      const built = covered || (data.updated !== null && data.updated !== undefined);
      return Object.assign({}, data, {
        ready: indexReady(built, null, built ? data.fresh !== true : null,
                          built ? (covered ? null : "not-covered") : "no-index"),
      });
    });
  }

  function indexQuery(opts) {
    opts = opts || {};
    const body = { sql: opts.sql };
    if (opts.limit !== undefined) body.limit = opts.limit;
    return indexWithStatus(indexPost("/api/index/query", body));
  }

  function indexStatus() {
    return indexGet("/api/index/status").then((data) =>
      Object.assign({}, data, { ready: indexReadyFromStatus(data) }));
  }

  function indexScan(opts) {
    opts = opts || {};
    const body = { full: !!opts.full };
    // No root means "every configured root" server-side, which is what the
    // shell's own Re-index button asks for.
    if (opts.root) body.root = opts.root;
    return indexWithStatus(indexPost("/api/index/scan", body)).then((out) => {
      // A scan was just started, whatever the parallel probe raced to see.
      // `stale` follows the list: with an index there is one and it is now
      // behind a scan; with none there is nothing to be behind (the same split
      // git_repos._not_ready draws).
      const indexed = out.ready.indexed;
      out.ready = indexReady(indexed, true, indexed, out.ready.reason);
      return out;
    });
  }

  function indexCancel(opts) {
    opts = opts || {};
    // `runId` in JS, `run_id` on the wire — the same camelCase-to-snake_case
    // trip every other option in this bridge makes.
    return indexWithStatus(indexPost("/api/index/cancel", {
      run_id: opts.runId === undefined ? opts.run_id || "" : opts.runId,
    }));
  }

  function indexConfigGet() {
    return indexWithStatus(indexGet("/api/index/config"));
  }

  function indexConfigSet(opts) {
    opts = opts || {};
    const body = {};
    // Sent only when named: the endpoint updates the keys present in the body,
    // so passing `ignore: undefined` as `ignore: null` would be a write.
    if (opts.roots !== undefined) body.roots = opts.roots;
    if (opts.ignore !== undefined) body.ignore = opts.ignore;
    return indexWithStatus(indexPost("/api/index/config", body));
  }

  function indexRepos() {
    return indexGet("/api/git-repos").then((data) =>
      Object.assign({}, data, {
        // Straight through: this endpoint already answers the whole triple, and
        // its `reason` is a distinction a UI renders — "outdated" means the
        // leaf-dir rule never ran and a rebuild is coming, "no-index" means
        // nothing has ever been built.
        ready: indexReady(data.indexed, data.scanning, data.stale, data.reason),
      }));
  }

  const index = {
    stats: indexStats,
    lookup: indexLookup,
    search: indexSearch,
    query: indexQuery,
    status: indexStatus,
    scan: indexScan,
    cancel: indexCancel,
    repos: indexRepos,
    config: { get: indexConfigGet, set: indexConfigSet },
  };
  // fused-index:end

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
    index,
    trackJob,
    watchJob,
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
