/*
 * Annotation overlay (SPEC §17, AN-1…AN-15).
 *
 * Injected by server.py ONLY when the rendered page's URL carries `_annotate=1`
 * (AN-4/AN-15) — normal pages never load this file. Loaded AFTER runtime.js so
 * the shell-URL replaceState channel the runtime establishes already exists;
 * this module deliberately does NOT touch `window.fused` (its public
 * params.set() guard rejecting `_` keys must stay intact, AN-9/PR-6). Instead it
 * reuses the runtime's *internal* mechanics by duplicating the two tiny helpers
 * the runtime is itself forced to duplicate (findTarget, splitSearch) — the
 * runtime is injected standalone and imports nothing, and so is this overlay, so
 * a shared module is impossible; a byte-for-byte copy is the established pattern
 * (see runtime.js's copy of layout-codec's balanced-paren scan).
 *
 * Comments live in the reserved `_comments` shell query param (AN-5): a
 * URL-encoded JSON array of thread objects. Because the key is `_`-prefixed it
 * is invisible to fused.params (PR-6) and segment-local inside `_layout` (LM-2),
 * so per-pane comments in panel/tab mode work with zero codec changes (AN-8).
 * All writes go through the SAME target window the runtime writes params to (the
 * topmost same-origin ancestor below any `_fusedParamBoundary`, D46/TM-3),
 * via history.replaceState + a `fused:urlchange` dispatch, so every listener
 * (the shell's bookmark button, the badge in preview.js, other panes) stays
 * coherent.
 *
 * ADAPTER SEAM (SPEC §17.5, AN-16…AN-23): element anchoring is meaningless in a
 * text editor, so a surface may register a *selection*-anchor adapter via
 * `window.__fusedAnnotate.registerAdapter(adapter)`. Registering disables the
 * element-mode UI (hover highlight, pointer-sequence suppression, click-capture,
 * element pins/anchor resolution, MutationObserver, reposition interval) while
 * keeping the core intact: data layer + URL channel + budget (AN-5/7/9), draft
 * and thread popovers, tray, toast (AN-17). The adapter owns anchor rendering and
 * resolution and calls back into the core (handed to `adapter.init(core)`) for
 * all UI/data operations. See registerAdapter()/buildCore() below.
 */
(function () {
  "use strict";

  // ---------------------------------------------------------------------------
  // Activation guard (AN-4). Self-activate only off THIS frame's own flag.
  // ---------------------------------------------------------------------------
  if (new URLSearchParams(window.location.search).get("_annotate") !== "1") {
    return;
  }

  // ===========================================================================
  // 1. Shell-URL channel — duplicated from runtime.js (see header note).
  // ===========================================================================

  // `_comments` is pane-local SHELL state, like `_mode` (LM-3): it lives on the
  // URL of the shell that rendered this preview iframe — the direct parent —
  // NOT on the topmost ancestor the runtime's LM-7 climb targets for user
  // params. In plain view/embed mode parent === top, so behavior is identical;
  // inside panel/tab mode the parent is the pane's embed shell, which keeps
  // each pane's comments segment-local inside `_layout` (AN-8) via the panel's
  // ordinary URL sync. Standalone /render pages have no parent → self.
  function findTarget() {
    try {
      if (window.parent && window.parent !== window) {
        void window.parent.location.href; // throws if cross-origin
        return window.parent;
      }
    } catch (e) {
      /* cross-origin parent; fall back to self */
    }
    return window;
  }

  const target = findTarget();

  // Split a query string, preserving the raw `_layout=(...)` span byte-for-byte
  // (balanced-paren scan, D51). Identical to runtime.js splitSearch() — a layout
  // URL cannot be parsed by plain URLSearchParams because `&` is literal inside
  // the parens, and the span must be reinserted untouched on write.
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

  // Read the raw `_comments` string off the target shell URL (undefined if
  // absent). Reserved params live in the non-layout `rest` of the query.
  function readCommentsParam() {
    const params = new URLSearchParams(splitSearch(target.location.search).rest);
    return params.has("_comments") ? params.get("_comments") : undefined;
  }

  // Write the `_comments` string to the target shell URL via replaceState,
  // preserving the raw `_layout` span (LAST, untouched — D51) and firing
  // `fused:urlchange` so the runtime's onChange diff, the shell bookmark button,
  // and the preview badge all observe it (AN-9). Passing null deletes the key.
  function writeCommentsParam(value) {
    const { layoutSpan, rest } = splitSearch(target.location.search);
    const params = new URLSearchParams(rest);
    if (value === null || value === undefined || value === "") {
      params.delete("_comments");
    } else {
      params.set("_comments", value);
    }
    let search = params.toString();
    if (layoutSpan) search += (search ? "&" : "") + layoutSpan;
    const newUrl = target.location.pathname + (search ? "?" + search : "");
    target.history.replaceState(target.history.state, "", newUrl);
    target.dispatchEvent(new Event("fused:urlchange"));
  }

  // ===========================================================================
  // 2. Data model (AN-5) — parse / serialize / budget (AN-7).
  // ===========================================================================

  const BUDGET = 6144; // ~6 KB soft cap on encodeURIComponent(json).length.

  // In-memory thread array; `lastJson` is the serialized form we last saw on the
  // URL, used to suppress the echo from our own write (mirrors runtime's
  // snapshot diff) and to skip no-op re-renders on unrelated urlchanges.
  let comments = [];
  let lastJson = "";

  function normalizeThread(t) {
    const now = Date.now();
    const thread = {
      id: typeof t.id === "string" ? t.id : uuid(),
      content: typeof t.content === "string" ? t.content : "",
      replies: Array.isArray(t.replies)
        ? t.replies.map((r) => ({
            id: typeof r.id === "string" ? r.id : uuid(),
            content: typeof r.content === "string" ? r.content : "",
            createdAt: Number(r.createdAt) || now,
          }))
        : [],
      status: t.status === "resolved" ? "resolved" : "open",
      createdAt: Number(t.createdAt) || now,
      updatedAt: Number(t.updatedAt) || Number(t.createdAt) || now,
      resolvedAt: Number(t.resolvedAt) || 0,
    };
    // Anchor forms are mutually exclusive with precedence handled at resolve
    // time (AN-6/AN-18: sel > anchorId > anchorPath > x/y); we simply carry
    // whichever fields are present.
    // Selection anchor (AN-18): {line ≥1, ch ≥0} pair + a quote capped at 120
    // chars. Owned/resolved by a registered adapter (AN-17); annotate.js just
    // ferries the fields through the URL channel.
    if (t.selFrom && t.selTo) {
      thread.selFrom = {
        line: Math.max(1, Math.round(Number(t.selFrom.line)) || 1),
        ch: Math.max(0, Math.round(Number(t.selFrom.ch)) || 0),
      };
      thread.selTo = {
        line: Math.max(1, Math.round(Number(t.selTo.line)) || 1),
        ch: Math.max(0, Math.round(Number(t.selTo.ch)) || 0),
      };
      thread.quote = typeof t.quote === "string" ? t.quote.slice(0, 120) : "";
    }
    if (typeof t.anchorId === "string" && t.anchorId) thread.anchorId = t.anchorId;
    if (typeof t.anchorPath === "string" && t.anchorPath) thread.anchorPath = t.anchorPath;
    // Image pixel refinement (AN-24): fractions of the image's displayed
    // content box, riding ALONGSIDE the element anchor (they refine placement,
    // they are not an anchor form of their own).
    if (t.iu !== undefined && t.iv !== undefined) {
      thread.iu = Math.min(1, Math.max(0, Number(t.iu) || 0));
      thread.iv = Math.min(1, Math.max(0, Number(t.iv) || 0));
    }
    if (t.x !== undefined && t.y !== undefined) {
      thread.x = Number(t.x) || 0;
      thread.y = Number(t.y) || 0;
    }
    return thread;
  }

  // Compact serialization: drop default/empty fields to stretch the budget,
  // while staying faithful to the AN-5 schema on parse (normalizeThread fills
  // the defaults back in).
  function compact(thread) {
    const o = { id: thread.id, content: thread.content, createdAt: thread.createdAt };
    if (thread.replies.length) {
      o.replies = thread.replies.map((r) => ({ id: r.id, content: r.content, createdAt: r.createdAt }));
    }
    if (thread.status === "resolved") o.status = "resolved";
    if (thread.updatedAt !== thread.createdAt) o.updatedAt = thread.updatedAt;
    if (thread.resolvedAt) o.resolvedAt = thread.resolvedAt;
    // Precedence sel > anchorId > anchorPath > x/y (AN-18) — emit exactly one form.
    if (thread.selFrom && thread.selTo) {
      o.selFrom = thread.selFrom;
      o.selTo = thread.selTo;
      if (thread.quote) o.quote = thread.quote.slice(0, 120); // cap on write (AN-18)
    } else if (thread.anchorId) o.anchorId = thread.anchorId;
    else if (thread.anchorPath) o.anchorPath = thread.anchorPath;
    else {
      o.x = thread.x || 0;
      o.y = thread.y || 0;
    }
    // iu/iv accompany an element anchor only (AN-26) — meaningless on sel/free.
    if ((o.anchorId || o.anchorPath) && thread.iu !== undefined && thread.iv !== undefined) {
      o.iu = Math.round(thread.iu * 1000) / 1000;
      o.iv = Math.round(thread.iv * 1000) / 1000;
    }
    return o;
  }

  function serialize(arr) {
    return JSON.stringify(arr.map(compact));
  }

  function uuid() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    // Fallback for older engines; not spec-critical, just needs uniqueness.
    return "c-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
  }

  // Load `comments` from the URL. Returns true if it changed since last read.
  function loadFromUrl() {
    const raw = readCommentsParam();
    const json = raw || "";
    if (json === lastJson) return false;
    lastJson = json;
    if (!raw) {
      comments = [];
      return true;
    }
    try {
      const parsed = JSON.parse(raw);
      comments = Array.isArray(parsed) ? parsed.map(normalizeThread) : [];
    } catch (e) {
      console.warn("[fused-annotate] could not parse _comments:", e);
      comments = [];
    }
    return true;
  }

  // Commit a new thread array to the URL under the AN-7 budget. On overflow,
  // drop the OLDEST resolved threads first (open threads are never dropped); if
  // the payload is still over with only open threads left, reject the write and
  // surface a toast. Returns true on success. `arr` is committed to `comments`
  // only on success (possibly minus dropped resolved threads).
  function commit(arr) {
    let working = arr.slice();
    let json = serialize(working);
    let dropped = 0; // resolved threads evicted by the budget — surfaced, not silent (AN-7)
    while (encodeURIComponent(json).length > BUDGET) {
      // Oldest resolved thread = smallest resolvedAt (fallback updatedAt).
      let oldestIdx = -1;
      let oldestAt = Infinity;
      for (let i = 0; i < working.length; i++) {
        const t = working[i];
        if (t.status !== "resolved") continue;
        const at = t.resolvedAt || t.updatedAt || t.createdAt;
        if (at < oldestAt) {
          oldestAt = at;
          oldestIdx = i;
        }
      }
      if (oldestIdx === -1) {
        // All remaining threads are open — cannot shrink further (AN-7).
        showToast("Comment storage full — resolve or delete old comments");
        return false;
      }
      working.splice(oldestIdx, 1);
      dropped++;
      json = serialize(working);
    }
    if (dropped > 0) {
      showToast(
        dropped === 1
          ? "Oldest resolved comment removed — URL size limit"
          : dropped + " oldest resolved comments removed — URL size limit"
      );
    }
    comments = working;
    lastJson = working.length ? json : "";
    writeCommentsParam(working.length ? json : null);
    emitChange(); // notify a registered adapter to rebuild its anchor visuals (AN-17)
    return true;
  }

  // ===========================================================================
  // 2b. Adapter seam (SPEC §17.5, AN-16/AN-17). Element anchoring is meaningless
  // in a text editor, so a surface (code_template.html) registers a selection-
  // anchor adapter. Registering swaps element-mode UI for adapter-owned visuals
  // but keeps the whole core (data layer, URL channel, popovers, tray, toast).
  // ===========================================================================

  let adapter = null; // registered adapter, or null for element mode.
  let coreReady = false; // true once start() has built + wired the core.
  let currentCore = null; // memoized core object handed to the adapter.
  const changeListeners = []; // adapter onChange() callbacks.

  function emitChange() {
    for (const cb of changeListeners) {
      try {
        cb();
      } catch (e) {
        console.warn("[fused-annotate] adapter onChange threw:", e);
      }
    }
  }

  // The core API handed to the adapter (AN-17). The adapter owns anchor
  // rendering/resolution and drives all UI/data through these:
  //  - getComments()                      → live thread array
  //  - onChange(cb)                       → cb() after any data change (local
  //                                          commit or external urlchange reload)
  //  - openDraftAt(x, y, anchorFields)    → draft popover at client coords; on
  //                                          Enter the new thread gets anchorFields
  //                                          spread in (sel anchors: selFrom/selTo/quote)
  //  - openThread(id, x, y)               → thread popover
  //  - closePopover()                     → close draft/thread (safe if none open)
  //  - showToast(msg)                     → transient overlay message
  //  - setDetached(ids)                   → render those threads in the tray (AN-21)
  //  - isDraftOpen()                      → whether a DRAFT popover is currently open
  //                                          (lets the adapter cancel only drafts on
  //                                          selection collapse, AN-19)
  function buildCore() {
    if (currentCore) return currentCore;
    currentCore = {
      getComments: () => comments,
      onChange: (cb) => {
        if (typeof cb === "function") changeListeners.push(cb);
      },
      openDraftAt: (clientX, clientY, anchorFields) => {
        closePopover();
        openDraft(null, 0, 0, clientX, clientY, anchorFields);
      },
      openThread: (id, clientX, clientY) => openThread(id, clientX, clientY),
      closePopover: () => closePopover(),
      showToast: (msg) => showToast(msg),
      setDetached: (ids) => setDetachedByIds(ids),
      isDraftOpen: () => !!(openPopover && openPopover.kind === "draft"),
      focusCard: (id) => focusCard(id), // anchor → sidebar card (AN-31/AN-32)
    };
    return currentCore;
  }

  function initAdapter() {
    if (!adapter || adapter.__faInited) return;
    adapter.__faInited = true;
    if (typeof adapter.init === "function") adapter.init(buildCore());
  }

  // Public registration entry (AN-17). Works whether called before or after
  // start(): before → start() picks it up and skips element wiring; after →
  // we tear down any element-mode wiring here and init immediately. Idempotent.
  function registerAdapter(a) {
    if (!a || adapter === a) return currentCore;
    adapter = a;
    if (coreReady) {
      if (elementModeWired) teardownElementMode();
      initAdapter();
    }
    return currentCore;
  }

  // Render the given thread ids in the existing detached tray (AN-21). The
  // adapter computes detachment (quote no longer resolves); we reuse the same
  // tray + pointerup→openThread wiring element mode uses (AN-14). Also feeds
  // the sidebar's "detached" tags (AN-30) — in adapter mode this call is the
  // only detachment signal the core gets.
  function setDetachedByIds(ids) {
    // Belt-and-braces: strip foreign threads (AN-34) even if an adapter still
    // reports them — cross-surface comments must never read as "detached".
    detachedIds = new Set(
      (ids || []).filter((id) => {
        const t = findThread(id);
        return t && !isForeign(t);
      })
    );
    renderTray(comments.filter((t) => detachedIds.has(t.id)));
    renderSidebar();
  }

  // Exposed synchronously (before start()) so a page whose inline script runs
  // after annotate.js — injected in <head>, ahead of the template's body script
  // — can register during parse. A page that loads AFTER us instead listens for
  // the `fused-annotate:ready` event start() dispatches, and/or pre-sets
  // window.__fusedAnnotateAdapter which start() picks up (AN-17).
  // toggleSidebar is the SHELL's entry (AN-28): the preview header's Comments
  // button reaches into its same-origin iframe and calls it — the sidebar
  // toggle lives next to the Annotate toggle, not floating over page content.
  window.__fusedAnnotate = {
    registerAdapter: registerAdapter,
    toggleSidebar: () => setSidebarOpen(!sidebarOpen),
  };

  // ===========================================================================
  // 3. Anchors (AN-6) — builder, resolver, path codec.
  // ===========================================================================

  // Build the anchorPath of an element: `tag:nth-of-type(n)` segments joined by
  // `>`, from the first body-descendant down to the element (body excluded).
  // nth-of-type is 1-based among same-tag siblings.
  function buildAnchorPath(el) {
    const segs = [];
    let node = el;
    while (node && node !== document.body && node.nodeType === 1) {
      const tag = node.tagName.toLowerCase();
      let n = 1;
      let sib = node.previousElementSibling;
      while (sib) {
        if (sib.tagName === node.tagName) n++;
        sib = sib.previousElementSibling;
      }
      segs.unshift(tag + ":nth-of-type(" + n + ")");
      node = node.parentElement;
    }
    // If we never reached body (element detached from body), the path is not
    // anchorable — caller falls back to a free pin.
    if (node !== document.body) return null;
    return segs.join(">");
  }

  // Resolve an anchorPath back to an element (null if the structure changed and
  // the path no longer matches — a detached anchor, AN-14).
  function resolveAnchorPath(path) {
    if (!path) return null;
    let node = document.body;
    const segs = path.split(">");
    for (const seg of segs) {
      const m = /^([a-z0-9-]+):nth-of-type\((\d+)\)$/i.exec(seg);
      if (!m) return null;
      const tag = m[1].toUpperCase();
      const nth = parseInt(m[2], 10);
      let count = 0;
      let found = null;
      for (const child of node.children) {
        if (child.tagName === tag) {
          count++;
          if (count === nth) {
            found = child;
            break;
          }
        }
      }
      if (!found) return null;
      node = found;
    }
    return node === document.body ? null : node;
  }

  // Build the anchor fields for a freshly-created thread from a click.
  // Precedence: an element with an id → anchorId; otherwise anchorPath; a click
  // on body/html or an unanchorable element → free pin at document coords.
  // A click on an <img> additionally records iu/iv — the click point as
  // fractions of the painted image (AN-24) — so the pin marks the exact spot.
  function buildAnchor(el, pageX, pageY, clientX, clientY) {
    if (el && el !== document.body && el !== document.documentElement) {
      let base = null;
      if (el.id) base = { anchorId: el.id };
      else {
        const path = buildAnchorPath(el);
        if (path) base = { anchorPath: path };
      }
      if (base) {
        // iu/iv only once the intrinsic size is known: before decode,
        // imgContentBox falls back to the layout box, and fractions taken of
        // the wrong box would persist a wrong pixel into the URL (AN-24). An
        // undecoded image gets a plain element anchor instead.
        if (
          el.tagName === "IMG" &&
          el.naturalWidth > 0 &&
          el.naturalHeight > 0 &&
          clientX !== undefined &&
          clientY !== undefined
        ) {
          const b = imgContentBox(el);
          if (b.width > 0 && b.height > 0) {
            // Letterbox clicks clamp to the nearest content edge (AN-24).
            base.iu = Math.round(Math.min(1, Math.max(0, (clientX - b.left) / b.width)) * 1000) / 1000;
            base.iv = Math.round(Math.min(1, Math.max(0, (clientY - b.top) / b.height)) * 1000) / 1000;
          }
        }
        return base;
      }
    }
    return { x: Math.round(pageX), y: Math.round(pageY) };
  }

  // Resolve a thread to a live element (or null for detached/free). Precedence
  // anchorId > anchorPath > x/y (AN-6).
  function resolveElement(thread) {
    if (thread.anchorId) return document.getElementById(thread.anchorId);
    if (thread.anchorPath) return resolveAnchorPath(thread.anchorPath);
    return null; // free pin — positioned by x/y
  }

  // FOREIGN thread (AN-34): `_comments` is shared across a file's preview
  // modes, so a selection comment (made in the code editor) is visible to the
  // element overlay and vice versa. Those are NOT detached — their anchor is
  // fine, it just lives on the other surface. They get no pin/decoration and
  // never enter the tray; the sidebar lists them with a surface tag instead.
  function isForeign(thread) {
    const isSel = !!(thread.selFrom && thread.selTo);
    return adapter ? !isSel : isSel;
  }

  // Is this thread anchored to something that no longer exists? (Detached →
  // tray, AN-14.) Free pins (x/y only) are never "detached". Foreign threads
  // (AN-34) are filtered out before this is asked.
  function isDetached(thread) {
    if (!thread.anchorId && !thread.anchorPath) return false;
    return resolveElement(thread) === null;
  }

  // ===========================================================================
  // 4. Styles + root container.
  // ===========================================================================

  const Z = 2147483000; // high, but below the runtime's error overlay (…647).

  const style = document.createElement("style");
  style.textContent = `
    #__fa_root, #__fa_root * { box-sizing: border-box; }
    #__fa_root {
      --fa-bg: #1b1d21; --fa-bg-alt: #131417; --fa-fg: #e8eaed;
      --fa-muted: #9aa0a6; --fa-border: #2a2d33; --fa-accent: #E5FF44;
      position: absolute; top: 0; left: 0; width: 0; height: 0;
      z-index: ${Z};
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      font-size: 13px; line-height: 1.4;
    }
    @media (prefers-color-scheme: light) {
      #__fa_root {
        --fa-bg: #ffffff; --fa-bg-alt: #f2f3f5; --fa-fg: #1a1c1f;
        --fa-muted: #6b7178; --fa-border: #d7dade; --fa-accent: #8a9a00;
      }
    }
    body.__fa_active { cursor: crosshair; }
    /* Hover highlight — a fixed, non-interactive box tracking getBoundingClientRect,
       so it never mutates the user's layout (AN-10). */
    #__fa_hl {
      position: fixed; pointer-events: none; z-index: ${Z};
      border: 1.5px solid var(--fa-accent);
      background: color-mix(in srgb, var(--fa-accent) 8%, transparent);
      border-radius: 3px; display: none;
      transition: left 90ms cubic-bezier(0.4, 0, 0.2, 1),
                  top 90ms cubic-bezier(0.4, 0, 0.2, 1),
                  width 90ms cubic-bezier(0.4, 0, 0.2, 1),
                  height 90ms cubic-bezier(0.4, 0, 0.2, 1);
    }
    /* Anchor dot: while a draft is open, marks the exact click point so the
       composer is visually tethered to what it annotates. */
    #__fa_dot {
      position: fixed; pointer-events: none; z-index: ${Z};
      width: 10px; height: 10px; border-radius: 50%;
      background: var(--fa-accent); border: 2px solid var(--fa-bg);
      transform: translate(-50%, -50%); display: none;
      box-shadow: 0 0 0 2px color-mix(in srgb, var(--fa-accent) 40%, transparent);
    }
    /* Pins live in document coords (absolute, scroll with content — AN-11). */
    #__fa_pins { position: absolute; top: 0; left: 0; pointer-events: none; }
    .__fa_pin {
      position: absolute; pointer-events: auto; transform: translate(-50%, -50%);
      width: 22px; height: 22px; border-radius: 50% 50% 50% 2px;
      background: var(--fa-accent); color: #10131a;
      border: 1.5px solid var(--fa-bg-alt);
      display: flex; align-items: center; justify-content: center;
      font-size: 11px; font-weight: 700; cursor: pointer;
      box-shadow: 0 1px 4px rgba(0,0,0,0.4); user-select: none;
      animation: __fa_pinin 160ms cubic-bezier(0.34, 1.56, 0.64, 1);
      transition: transform 120ms cubic-bezier(0.16, 1, 0.3, 1);
    }
    @keyframes __fa_pinin {
      from { transform: translate(-50%, -50%) scale(0); }
      to { transform: translate(-50%, -50%) scale(1); }
    }
    /* A 0-reply pin carries a small dot so the teardrop reads as "a comment",
       not a stray blob; numerals appear only once there are replies. */
    .__fa_pin:empty::after {
      content: ""; width: 6px; height: 6px; border-radius: 50%; background: #10131a;
    }
    .__fa_pin.__fa_resolved { background: var(--fa-muted); opacity: 0.7; }
    .__fa_pin:hover { transform: translate(-50%, -50%) scale(1.12); }
    /* Reveal pulse for image pins (AN-30): grow-and-settle beats an accent
       ring, which disappears against light/white imagery. */
    .__fa_pin.__fa_pulse { animation: __fa_pinpulse 700ms cubic-bezier(0.34, 1.56, 0.64, 1); }
    @keyframes __fa_pinpulse {
      0% { transform: translate(-50%, -50%) scale(1); }
      35% { transform: translate(-50%, -50%) scale(1.6); }
      100% { transform: translate(-50%, -50%) scale(1); }
    }
    /* Popovers (draft + thread) — fixed, interactive. Enter decelerating from
       the anchor corner; removal is instant (no exit animation needed). */
    .__fa_pop {
      position: fixed; z-index: ${Z};
      width: 300px; max-width: calc(100vw - 24px);
      background: var(--fa-bg); color: var(--fa-fg);
      border: 1px solid var(--fa-border); border-radius: 10px;
      box-shadow: 0 8px 28px rgba(0,0,0,0.5); overflow: hidden;
      transform-origin: top left;
      animation: __fa_popin 140ms cubic-bezier(0.16, 1, 0.3, 1);
    }
    @keyframes __fa_popin {
      from { opacity: 0; transform: scale(0.96); }
      to { opacity: 1; transform: scale(1); }
    }
    .__fa_msgs { max-height: 300px; overflow-y: auto; padding: 4px 0; }
    .__fa_msg { padding: 8px 12px; }
    .__fa_msg + .__fa_msg { border-top: 1px solid var(--fa-border); }
    .__fa_msg_top { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
    .__fa_time { color: var(--fa-muted); font-size: 11px; white-space: nowrap; }
    /* Edit is a small ghost button — same family as __fa_btn, scaled down. */
    .__fa_edit {
      font: inherit; font-size: 11px; padding: 2px 8px; cursor: pointer;
      border: 1px solid var(--fa-border); border-radius: 5px;
      background: none; color: var(--fa-muted);
    }
    .__fa_edit:hover { border-color: var(--fa-accent); color: var(--fa-accent); }
    .__fa_body { white-space: pre-wrap; word-break: break-word; margin-top: 2px; }
    /* One textarea style for every composer host — popover AND sidebar card
       (reply + inline edit) — so the shared thread body renders identically. */
    .__fa_pop textarea, .__fa_card textarea {
      width: 100%; resize: vertical; min-height: 34px; max-height: 160px;
      font: inherit; padding: 6px 8px; border-radius: 6px;
      border: 1px solid var(--fa-border); background: var(--fa-bg-alt);
      color: var(--fa-fg); outline: none;
    }
    .__fa_pop textarea:focus, .__fa_card textarea:focus { border-color: var(--fa-accent); }
    .__fa_draftwrap, .__fa_replywrap { padding: 8px; }
    .__fa_hint { color: var(--fa-muted); font-size: 11px; margin-top: 4px; }
    .__fa_btn.__fa_primary {
      background: var(--fa-accent); color: #10131a; border-color: var(--fa-accent);
    }
    .__fa_btn.__fa_primary:hover { filter: brightness(1.1); }
    .__fa_footer {
      display: flex; gap: 8px; padding: 8px 12px;
      border-top: 1px solid var(--fa-border); background: var(--fa-bg-alt);
    }
    .__fa_btn {
      font: inherit; font-size: 12px; padding: 4px 10px; cursor: pointer;
      border: 1px solid var(--fa-border); border-radius: 6px;
      background: var(--fa-bg); color: var(--fa-fg);
    }
    .__fa_btn:hover { border-color: var(--fa-accent); }
    .__fa_btn.__fa_danger:hover { border-color: var(--fa-accent); color: var(--fa-accent); }
    .__fa_spacer { flex: 1 1 auto; }
    /* Detached tray (AN-14) — fixed bottom-right list of orphaned threads. */
    #__fa_tray {
      position: fixed; right: 12px; bottom: 12px; z-index: ${Z};
      width: 240px; max-height: 40vh; overflow-y: auto;
      background: var(--fa-bg); color: var(--fa-fg);
      border: 1px solid var(--fa-border); border-radius: 10px;
      box-shadow: 0 8px 28px rgba(0,0,0,0.5); display: none;
    }
    #__fa_tray_title {
      padding: 8px 12px; font-size: 11px; font-weight: 600; color: var(--fa-muted);
      border-bottom: 1px solid var(--fa-border);
    }
    .__fa_trayitem {
      padding: 8px 12px; cursor: pointer; border-bottom: 1px solid var(--fa-border);
      display: flex; gap: 8px; align-items: center;
    }
    .__fa_trayitem:last-child { border-bottom: none; }
    .__fa_trayitem:hover { background: var(--fa-bg-alt); }
    .__fa_traydot {
      flex: 0 0 auto; width: 18px; height: 18px; border-radius: 50% 50% 50% 2px;
      background: var(--fa-muted); color: #10131a; font-size: 10px; font-weight: 700;
      display: flex; align-items: center; justify-content: center;
    }
    .__fa_traytext { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    /* Toast (AN-7 overflow message). */
    #__fa_toast {
      position: fixed; left: 50%; bottom: 20px; transform: translateX(-50%);
      z-index: ${Z + 1};
      background: var(--fa-bg); color: var(--fa-fg);
      border: 1px solid var(--fa-accent); border-radius: 8px;
      padding: 8px 14px; box-shadow: 0 8px 28px rgba(0,0,0,0.5);
      display: none; max-width: calc(100vw - 24px);
    }
    /* Reveal flashes (AN-30) — document coords like pins, so they scroll with
       content and smooth-scroll timing can't strand them. Own container: a
       child of #__fa_pins would break reposition()'s index-parallel walk. */
    #__fa_fx { position: absolute; top: 0; left: 0; pointer-events: none; }
    .__fa_flash {
      position: absolute; pointer-events: none;
      border: 2px solid var(--fa-accent); border-radius: 4px;
      background: color-mix(in srgb, var(--fa-accent) 20%, transparent);
      animation: __fa_flashpulse 900ms ease-out forwards;
    }
    .__fa_flash_dot { border-radius: 50%; }
    /* Pulse in → hold → fade: reads "look here", not "something faded". */
    @keyframes __fa_flashpulse {
      0% { opacity: 0; transform: scale(1.04); }
      15% { opacity: 1; transform: scale(1); }
      60% { opacity: 1; }
      100% { opacity: 0; }
    }
    /* Sidebar toggle (AN-28) — floating pill, top-right; hidden while open. */
    #__fa_sidebtn {
      position: fixed; top: 12px; right: 12px; z-index: ${Z};
      display: flex; align-items: center; gap: 6px; padding: 6px 10px;
      background: var(--fa-bg); color: var(--fa-fg);
      border: 1px solid var(--fa-border); border-radius: 999px;
      cursor: pointer; font: inherit; font-size: 12px;
      box-shadow: 0 2px 10px rgba(0,0,0,0.35);
    }
    #__fa_sidebtn:hover { border-color: var(--fa-accent); }
    #__fa_sidecount { font-weight: 700; }
    /* Comments sidebar (AN-28…AN-31). */
    #__fa_side {
      position: fixed; top: 0; right: 0; bottom: 0; z-index: ${Z};
      width: 320px; max-width: 85vw;
      background: var(--fa-bg); color: var(--fa-fg);
      border-left: 1px solid var(--fa-border);
      box-shadow: -8px 0 28px rgba(0,0,0,0.35);
      transform: translateX(105%);
      transition: transform 240ms cubic-bezier(0.32, 0.72, 0, 1);
      display: flex; flex-direction: column;
    }
    #__fa_side.__fa_open { transform: translateX(0); }
    /* Card list settles in just after the panel (fade + small rise). */
    #__fa_side.__fa_open #__fa_side_list {
      animation: __fa_listin 180ms cubic-bezier(0.16, 1, 0.3, 1) 60ms backwards;
    }
    @keyframes __fa_listin {
      from { opacity: 0; transform: translateY(6px); }
      to { opacity: 1; transform: translateY(0); }
    }
    #__fa_side_head {
      display: flex; align-items: center; gap: 8px; padding: 12px 16px;
      font-size: 14px; font-weight: 600;
      border-bottom: 1px solid var(--fa-border);
    }
    #__fa_side_close {
      display: flex; align-items: center; justify-content: center;
      width: 22px; height: 22px; border-radius: 6px;
      background: none; border: none; color: var(--fa-muted); cursor: pointer; padding: 0;
    }
    #__fa_side_close:hover { background: var(--fa-bg-alt); color: var(--fa-fg); }
    #__fa_side_list { flex: 1 1 auto; overflow-y: auto; }
    .__fa_side_empty { padding: 40px 24px; color: var(--fa-muted); text-align: center; }
    .__fa_side_empty svg { opacity: 0.5; margin-bottom: 12px; }
    .__fa_side_empty_title { font-weight: 600; color: var(--fa-fg); margin-bottom: 4px; }
    /* Section label splitting resolved threads from open ones (AN-29). */
    .__fa_side_sect {
      padding: 10px 16px 4px; font-size: 11px; font-weight: 600;
      color: var(--fa-muted); border-top: 1px solid var(--fa-border);
    }
    .__fa_card {
      padding: 12px 16px; border-bottom: 1px solid var(--fa-border);
      border-left: 3px solid transparent; cursor: pointer;
    }
    .__fa_card:hover { background: var(--fa-bg-alt); }
    .__fa_card.__fa_card_resolved { opacity: 0.72; }
    .__fa_card_open {
      padding: 0; cursor: default; opacity: 1;
      border-left-color: var(--fa-accent); background: var(--fa-bg-alt);
    }
    .__fa_card_top { display: flex; align-items: center; gap: 8px; }
    .__fa_carddot {
      flex: 0 0 auto; width: 16px; height: 16px; border-radius: 50% 50% 50% 2px;
      background: var(--fa-accent); color: #10131a; font-size: 9px; font-weight: 700;
      display: flex; align-items: center; justify-content: center;
    }
    .__fa_carddot_resolved { background: var(--fa-muted); opacity: 0.7; }
    .__fa_tag {
      font-size: 10px; color: var(--fa-muted);
      border: 1px solid var(--fa-border); border-radius: 4px; padding: 0 4px;
    }
    .__fa_card_replies { font-size: 11px; color: var(--fa-muted); }
    .__fa_card_snip {
      margin-top: 4px; word-break: break-word; overflow: hidden;
      display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
    }
    /* Sidebar open shifts the detached tray out from under the panel. */
    #__fa_root.__fa_sideopen #__fa_tray { right: 344px; }
  `;

  const root = document.createElement("div");
  root.id = "__fa_root";
  root.innerHTML = `
    <div id="__fa_hl"></div>
    <div id="__fa_dot"></div>
    <div id="__fa_pins"></div>
    <div id="__fa_fx"></div>
    <div id="__fa_tray"><div id="__fa_tray_title">Detached comments</div><div id="__fa_tray_list"></div></div>
    <button id="__fa_sidebtn" title="All comments">
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/><line x1="8" y1="8" x2="16" y2="8"/><line x1="8" y1="12" x2="13" y2="12"/></svg>
      <span id="__fa_sidecount">0</span>
    </button>
    <div id="__fa_side">
      <div id="__fa_side_head"><span>Comments</span><span class="__fa_spacer"></span><button id="__fa_side_close" title="Close"><svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><line x1="5" y1="5" x2="19" y2="19"/><line x1="19" y1="5" x2="5" y2="19"/></svg></button></div>
      <div id="__fa_side_list"></div>
    </div>
    <div id="__fa_toast"></div>
  `;

  function mount() {
    document.head.appendChild(style);
    document.body.appendChild(root);
    // Crosshair cursor is element-mode only (wireElementMode). In adapter mode
    // the surface owns its own cursor (e.g. text caret in the code editor).
  }

  const pinsEl = () => root.querySelector("#__fa_pins");
  const hlEl = () => root.querySelector("#__fa_hl");
  const trayEl = () => root.querySelector("#__fa_tray");
  const trayListEl = () => root.querySelector("#__fa_tray_list");
  const toastEl = () => root.querySelector("#__fa_toast");
  const fxEl = () => root.querySelector("#__fa_fx");
  const sideEl = () => root.querySelector("#__fa_side");
  const sideListEl = () => root.querySelector("#__fa_side_list");
  const sideBtnEl = () => root.querySelector("#__fa_sidebtn");

  let toastTimer = null;
  function showToast(msg) {
    const el = toastEl();
    el.textContent = msg;
    el.style.display = "block";
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (el.style.display = "none"), 4000);
  }

  // ===========================================================================
  // 5. Utilities — relative time, escaping, page-coord geometry.
  // ===========================================================================

  function relTime(ts) {
    const diff = Date.now() - ts;
    const s = Math.round(diff / 1000);
    if (s < 45) return "just now";
    const m = Math.round(s / 60);
    if (m < 60) return m + "m ago";
    const h = Math.round(m / 60);
    if (h < 24) return h + "h ago";
    const d = Math.round(h / 24);
    if (d < 7) return d + "d ago";
    return new Date(ts).toLocaleDateString();
  }

  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  // Document-coordinate top-right corner of an element (pins scroll with page).
  function pageAnchorPoint(el) {
    const r = el.getBoundingClientRect();
    return { x: r.right + window.scrollX, y: r.top + window.scrollY };
  }

  // Clamp a pin's document coords so it never straddles/overruns the page edge
  // (a full-width element's top-right corner sits exactly on the boundary).
  function clampPin(x, y) {
    const maxX = Math.max(document.documentElement.scrollWidth, window.innerWidth) - 12;
    return { x: Math.max(12, Math.min(x, maxX)), y: Math.max(12, y) };
  }

  // Displayed CONTENT box of an <img> in client coords, object-fit-aware
  // (AN-24): with `contain`/`cover`/`scale-down` the painted image doesn't fill
  // the element box, so pixel fractions must be taken of the painted area, not
  // the layout rect — otherwise letterbox padding shifts the pin between
  // renders at different sizes.
  function imgContentBox(img) {
    const r = img.getBoundingClientRect();
    const natW = img.naturalWidth;
    const natH = img.naturalHeight;
    if (!natW || !natH || !r.width || !r.height) {
      return { left: r.left, top: r.top, width: r.width, height: r.height };
    }
    const fit = (getComputedStyle(img).objectFit || "fill");
    let w, h;
    if (fit === "none") {
      w = natW;
      h = natH;
    } else if (fit === "contain" || fit === "cover" || fit === "scale-down") {
      let s =
        fit === "cover"
          ? Math.max(r.width / natW, r.height / natH)
          : Math.min(r.width / natW, r.height / natH);
      if (fit === "scale-down") s = Math.min(s, 1);
      w = natW * s;
      h = natH * s;
    } else {
      // fill (default): content box === element box.
      w = r.width;
      h = r.height;
    }
    return { left: r.left + (r.width - w) / 2, top: r.top + (r.height - h) / 2, width: w, height: h };
  }

  // Document-coord pin position for a thread on its resolved element: an image
  // thread with iu/iv pins at that fraction of the painted image (AN-25);
  // everything else keeps the top-right corner (AN-11).
  function pinPoint(thread, el) {
    if (thread.iu !== undefined && thread.iv !== undefined && el.tagName === "IMG") {
      const b = imgContentBox(el);
      return {
        x: b.left + thread.iu * b.width + window.scrollX,
        y: b.top + thread.iv * b.height + window.scrollY,
      };
    }
    return pageAnchorPoint(el);
  }

  // ===========================================================================
  // 6. Hover highlight + click-to-create (AN-10).
  // ===========================================================================

  let hovered = null;

  function insideOverlay(node) {
    return !!(node && node.closest && node.closest("#__fa_root"));
  }

  function onMouseMove(e) {
    if (openPopover) {
      hlEl().style.display = "none";
      return;
    }
    const el = e.target;
    if (!el || insideOverlay(el) || el === document.body || el === document.documentElement) {
      hlEl().style.display = "none";
      hovered = null;
      return;
    }
    hovered = el;
    const r = el.getBoundingClientRect();
    const hl = hlEl();
    hl.style.display = "block";
    hl.style.left = r.left + "px";
    hl.style.top = r.top + "px";
    hl.style.width = r.width + "px";
    hl.style.height = r.height + "px";
  }

  // Capture-phase click: a click NOT inside our overlay is a page click → open a
  // draft (and suppress the page's own default, so links/buttons don't fire).
  // Clicks inside the overlay (pins, popover) fall through to their own
  // bubbling handlers.
  function onClickCapture(e) {
    if (insideOverlay(e.target)) return;
    e.preventDefault();
    e.stopPropagation();
    closePopover();
    openDraft(e.target, e.pageX, e.pageY, e.clientX, e.clientY);
  }

  // Interactive elements react before `click` fires — a range input moves its
  // thumb on mousedown, a button takes focus — so annotate mode must swallow
  // the whole pointer sequence at capture, not just the click. Overlay UI
  // (pins, popovers, tray) keeps its events.
  function onPointerCapture(e) {
    if (insideOverlay(e.target)) return;
    e.preventDefault();
    e.stopPropagation();
  }

  // ===========================================================================
  // 7. Draft popover (AN-10).
  // ===========================================================================

  let openPopover = null; // { el, thread? } bookkeeping for reposition/close.

  function positionPopover(pop, clientX, clientY) {
    // Clamp to viewport with a small margin; an open sidebar (AN-28) shrinks
    // the usable width so popovers never slide underneath the panel.
    const margin = 8;
    const usableRight = window.innerWidth - (sidebarOpen ? sideEl().offsetWidth : 0);
    const w = pop.offsetWidth || 300;
    const h = pop.offsetHeight || 120;
    let left = clientX + 12;
    let top = clientY + 12;
    if (left + w + margin > usableRight) left = usableRight - w - margin;
    if (top + h + margin > window.innerHeight) top = window.innerHeight - h - margin;
    if (left < margin) left = margin;
    if (top < margin) top = margin;
    pop.style.left = left + "px";
    pop.style.top = top + "px";
  }

  // anchorFields (AN-17) overrides the element/coord anchor: the adapter passes
  // the selection anchor ({selFrom, selTo, quote}) it computed, spread into the
  // thread on submit. When absent (element mode) we build the anchor from the
  // clicked element/coords (AN-6).
  function openDraft(el, pageX, pageY, clientX, clientY, anchorFields) {
    const anchor = anchorFields || buildAnchor(el, pageX, pageY, clientX, clientY);
    hlEl().style.display = "none";

    // Tether the composer to what it annotates: an anchor dot at the click
    // point, removed when the popover closes.
    const dot = root.querySelector("#__fa_dot");
    dot.style.left = clientX + "px";
    dot.style.top = clientY + "px";
    dot.style.display = "block";

    const pop = document.createElement("div");
    pop.className = "__fa_pop";
    // The composer needs a VISIBLE primary action, not just the keyboard hint
    // — the thread popover already has a button footer; mirror it.
    pop.innerHTML = `
      <div class="__fa_draftwrap">
        <textarea placeholder="Add a comment…"></textarea>
        <div class="__fa_hint">Enter to save · Shift+Enter for newline · Esc to cancel</div>
      </div>
      <div class="__fa_footer">
        <span class="__fa_spacer"></span>
        <button class="__fa_btn" data-act="cancel">Cancel</button>
        <button class="__fa_btn __fa_primary" data-act="save">Comment</button>
      </div>`;
    root.appendChild(pop);
    positionPopover(pop, clientX, clientY);

    const ta = pop.querySelector("textarea");
    ta.focus();

    const submit = () => {
      const content = ta.value.trim();
      if (!content) return; // empty submit = ignore (AN-10)
      submitDraft(anchor, content);
    };
    pop.querySelector('[data-act="save"]').addEventListener("click", submit);
    pop.querySelector('[data-act="cancel"]').addEventListener("click", () => closePopover());

    ta.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && !ev.shiftKey) {
        ev.preventDefault();
        submit();
      } else if (ev.key === "Escape") {
        ev.preventDefault();
        closePopover();
      }
    });

    openPopover = { kind: "draft", el: pop, clientX, clientY };
  }

  function submitDraft(anchor, content) {
    const now = Date.now();
    const thread = normalizeThread(
      Object.assign(
        { id: uuid(), content, replies: [], status: "open", createdAt: now, updatedAt: now },
        anchor
      )
    );
    const next = comments.concat([thread]);
    if (commit(next)) {
      closePopover();
      render();
    }
    // On rejection commit() shows a toast; leave the draft open so the text
    // isn't lost.
  }

  // ===========================================================================
  // 8. Thread popover (AN-12).
  // ===========================================================================

  function findThread(id) {
    return comments.find((t) => t.id === id) || null;
  }

  function openThread(id, clientX, clientY) {
    closePopover();
    const thread = findThread(id);
    if (!thread) return;

    const pop = document.createElement("div");
    pop.className = "__fa_pop";
    root.appendChild(pop);
    renderThread(pop, thread);
    positionPopover(pop, clientX, clientY);
    openPopover = { kind: "thread", el: pop, threadId: id, clientX, clientY };
    focusCard(id); // anchor → list: highlight the card in an open sidebar (AN-31)
  }

  // Root + chronological replies as .__fa_msg markup — shared by the thread
  // popover (AN-12) and the expanded sidebar card (AN-29).
  function threadMessagesHtml(thread) {
    const msgs = [{ id: thread.id, content: thread.content, createdAt: thread.createdAt, root: true }]
      .concat(thread.replies.slice().sort((a, b) => a.createdAt - b.createdAt));
    return msgs
      .map(
        (m) => `
        <div class="__fa_msg" data-msg="${esc(m.id)}">
          <div class="__fa_msg_top">
            <span class="__fa_time">${esc(relTime(m.createdAt))}</span>
            <button class="__fa_edit" data-edit="${esc(m.id)}">Edit</button>
          </div>
          <div class="__fa_body">${esc(m.content)}</div>
        </div>`
      )
      .join("");
  }

  // ONE thread body for BOTH hosts — the popover (AN-12) and the expanded
  // sidebar card (AN-29) render byte-identical markup through here: messages,
  // reply composer (Enter submits — no button), Resolve/Delete footer.
  // `rerender` is the host's restore hook for edit-cancel (null = popover).
  function renderThreadBody(container, thread, rerender) {
    const resolved = thread.status === "resolved";

    container.innerHTML = `
      <div class="__fa_msgs">${threadMessagesHtml(thread)}</div>
      ${
        resolved
          ? ""
          : `<div class="__fa_replywrap"><textarea placeholder="Reply…"></textarea><div class="__fa_hint">Enter to reply · Shift+Enter for newline</div></div>`
      }
      <div class="__fa_footer">
        <button class="__fa_btn" data-act="toggle">${resolved ? "Reopen" : "Resolve"}</button>
        <span class="__fa_spacer"></span>
        <button class="__fa_btn __fa_danger" data-act="delete">Delete</button>
      </div>`;

    // Reply submit — Enter, mirroring the draft composer.
    const replyTa = container.querySelector(".__fa_replywrap textarea");
    if (replyTa) {
      replyTa.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" && !ev.shiftKey) {
          ev.preventDefault();
          const content = replyTa.value.trim();
          if (content) addReply(thread.id, content);
        } else if (ev.key === "Escape" && !rerender) {
          // Popover host only: Escape dismisses it (the sidebar card stays).
          ev.preventDefault();
          closePopover();
        }
      });
    }

    // Inline edit per message (AN-12).
    container.querySelectorAll("[data-edit]").forEach((btn) => {
      btn.addEventListener("click", () => beginEdit(container, thread, btn.getAttribute("data-edit"), rerender));
    });

    // Footer actions.
    container.querySelector('[data-act="toggle"]').addEventListener("click", () => toggleResolved(thread.id));
    container.querySelector('[data-act="delete"]').addEventListener("click", () => deleteThread(thread.id));
  }

  // (Re)render a thread popover's contents from the current thread state.
  function renderThread(pop, thread) {
    renderThreadBody(pop, thread, null);
  }

  // Swap a message body for an edit textarea; Enter/blur saves, Escape cancels.
  // `rerender` restores the container on cancel/no-change: the popover re-runs
  // renderThread on itself (default), the sidebar card re-runs renderSidebar
  // (AN-29) — same edit path, two hosts.
  function beginEdit(container, thread, msgId, rerender) {
    const msgEl = container.querySelector(`.__fa_msg[data-msg="${CSS.escape(msgId)}"]`);
    if (!msgEl) return;
    const bodyEl = msgEl.querySelector(".__fa_body");
    const current = msgId === thread.id ? thread.content : (thread.replies.find((r) => r.id === msgId) || {}).content || "";

    const restore = () => {
      const t = findThread(thread.id);
      if (!t) return;
      if (rerender) rerender();
      else if (openPopover && openPopover.el === container) renderThread(container, t);
    };

    const ta = document.createElement("textarea");
    ta.value = current;
    bodyEl.replaceWith(ta);
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);

    let done = false;
    const save = () => {
      if (done) return;
      done = true;
      const content = ta.value.trim();
      if (content && content !== current) {
        editMessage(thread.id, msgId, content);
      } else {
        restore(); // no change / empty → restore the original body
      }
    };
    ta.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && !ev.shiftKey) {
        ev.preventDefault();
        save();
      } else if (ev.key === "Escape") {
        ev.preventDefault();
        done = true;
        restore();
      }
    });
    ta.addEventListener("blur", save);
  }

  // ===========================================================================
  // 9. Mutations (all route through commit() + render()).
  // ===========================================================================

  function mutateThread(id, fn) {
    const next = comments.map((t) => (t.id === id ? fn(Object.assign({}, t)) : t));
    return next;
  }

  function addReply(id, content) {
    const now = Date.now();
    const next = mutateThread(id, (t) => {
      t.replies = t.replies.concat([{ id: uuid(), content, createdAt: now }]);
      t.updatedAt = now;
      return t;
    });
    if (commit(next)) refreshOpenThread(id), render();
  }

  function editMessage(id, msgId, content) {
    const now = Date.now();
    const next = mutateThread(id, (t) => {
      if (msgId === id) {
        t.content = content;
      } else {
        t.replies = t.replies.map((r) => (r.id === msgId ? Object.assign({}, r, { content }) : r));
      }
      t.updatedAt = now;
      return t;
    });
    if (commit(next)) refreshOpenThread(id), render();
  }

  function toggleResolved(id) {
    const now = Date.now();
    const next = mutateThread(id, (t) => {
      if (t.status === "resolved") {
        t.status = "open";
        t.resolvedAt = 0;
      } else {
        t.status = "resolved";
        t.resolvedAt = now;
      }
      t.updatedAt = now;
      return t;
    });
    if (commit(next)) refreshOpenThread(id), render();
  }

  function deleteThread(id) {
    const next = comments.filter((t) => t.id !== id);
    if (commit(next)) {
      closePopover();
      render();
    }
  }

  // Re-render an open thread popover in place after a mutation (keeps it open).
  function refreshOpenThread(id) {
    if (openPopover && openPopover.kind === "thread" && openPopover.threadId === id) {
      const t = findThread(id);
      if (t) {
        renderThread(openPopover.el, t);
        positionPopover(openPopover.el, openPopover.clientX, openPopover.clientY);
      } else {
        closePopover();
      }
    }
  }

  function closePopover() {
    if (openPopover && openPopover.el) openPopover.el.remove();
    openPopover = null;
    const dot = root.querySelector("#__fa_dot");
    if (dot) dot.style.display = "none";
  }

  // ===========================================================================
  // 10. Pin + tray rendering / repositioning.
  // ===========================================================================

  function pinGlyph(thread) {
    if (thread.status === "resolved") return "✓"; // ✓
    // Zero replies: empty — the teardrop pin shape itself is the comment
    // marker ("•" read as a stray bullet). Reply count carries thread size.
    return thread.replies.length > 0 ? String(thread.replies.length) : "";
  }

  // Full render: place a pin for every attached/free thread, dock detached ones
  // into the tray (AN-11/AN-14).
  function render() {
    renderSidebar(); // sidebar mirrors the same data in BOTH modes (AN-29)
    // Adapter mode owns anchor visuals (decorations + its own detachment via
    // core.setDetached); element pins/tray are not drawn here (AN-17).
    if (adapter) return;
    const pins = pinsEl();
    pins.innerHTML = "";
    const detached = [];

    for (const thread of comments) {
      if (isForeign(thread)) continue; // other surface — sidebar lists it (AN-34)
      if (isDetached(thread)) {
        detached.push(thread);
        continue;
      }
      const el = resolveElement(thread);
      let x, y;
      if (el) {
        const p = pinPoint(thread, el); // iu/iv-aware for images (AN-25)
        x = p.x;
        y = p.y;
      } else {
        // Free pin at stored document coords.
        x = thread.x || 0;
        y = thread.y || 0;
      }
      const pin = document.createElement("div");
      pin.className = "__fa_pin" + (thread.status === "resolved" ? " __fa_resolved" : "");
      pin.setAttribute("data-thread", thread.id); // reveal pulse lookup (AN-30)
      const cp = clampPin(x, y);
      pin.style.left = cp.x + "px";
      pin.style.top = cp.y + "px";
      pin.textContent = pinGlyph(thread);
      pin.title = thread.content;
      // pointerup, not click: if an async page mutation rebuilds pins between
      // mousedown and mouseup, no click is ever synthesized — pointerup still
      // fires on whichever pin node is under the pointer at release.
      pin.addEventListener("pointerup", (ev) => {
        ev.stopPropagation();
        openThread(thread.id, ev.clientX, ev.clientY);
      });
      pins.appendChild(pin);
    }

    detachedIds = new Set(detached.map((t) => t.id)); // sidebar "detached" tags (AN-30)
    renderTray(detached);
  }

  function renderTray(detached) {
    const tray = trayEl();
    const list = trayListEl();
    list.innerHTML = "";
    if (detached.length === 0) {
      tray.style.display = "none";
      return;
    }
    tray.style.display = "block";
    for (const thread of detached) {
      const item = document.createElement("div");
      item.className = "__fa_trayitem";
      const snippet = (thread.content || "").slice(0, 60) || "(empty)";
      item.innerHTML = `<span class="__fa_traydot">${esc(pinGlyph(thread))}</span><span class="__fa_traytext">${esc(snippet)}</span>`;
      // pointerup for the same node-swap reason as pins.
      item.addEventListener("pointerup", (ev) => {
        ev.stopPropagation();
        // Anchor the popover to the tray item (AN-14).
        const r = item.getBoundingClientRect();
        openThread(thread.id, r.left, r.top);
      });
      list.appendChild(item);
    }
  }

  // ===========================================================================
  // 10b. Comments sidebar (SPEC §17.7, AN-28…AN-33) — a Google-Docs-style
  // review panel over the SAME thread data. View only: every mutation routes
  // through the existing commit()/render() path, which re-renders the sidebar.
  // ===========================================================================

  let sidebarOpen = false; // ephemeral, per pane — deliberately NOT URL state (AN-28)
  let expandedCardId = null; // the one card showing the full thread (AN-29)
  let detachedIds = new Set(); // last-known detachment set, both modes (AN-30)

  // The sidebar is the annotate-mode home for comments: it AUTO-OPENS with the
  // mode (AN-28) — no header button, no hunting. The floating pill exists only
  // as the reopen affordance after an explicit close (×/Escape).
  function setSidebarOpen(open) {
    sidebarOpen = open;
    root.classList.toggle("__fa_sideopen", open); // shifts the tray (CSS)
    sideEl().classList.toggle("__fa_open", open);
    sideBtnEl().style.display = open ? "none" : "flex";
    // Google-Docs behavior: the panel RESERVES space instead of covering
    // content — push the page over by the panel's real width (85vw-capped),
    // restore on close. Inline margin beats a class here because the width is
    // computed; the transition is set once in start() so both directions ease.
    document.body.style.marginRight = open ? sideEl().offsetWidth + "px" : "";
    if (open) renderSidebar();
  }

  // Rebuild the toggle count + (when open) the card list. Open threads first,
  // newest-first within each group (AN-29).
  function renderSidebar() {
    const openCount = comments.filter((t) => t.status !== "resolved").length;
    sideBtnEl().querySelector("#__fa_sidecount").textContent = String(openCount);
    if (!sidebarOpen) return;
    const list = sideListEl();
    list.innerHTML = "";
    if (comments.length === 0) {
      const hint = adapter
        ? "Select some text in the editor to leave a comment."
        : "Click any element on the page to leave a comment.";
      list.innerHTML = `
        <div class="__fa_side_empty">
          <svg viewBox="0 0 24 24" width="40" height="40" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
          <div class="__fa_side_empty_title">No comments yet</div>
          <div>${hint}</div>
        </div>`;
      return;
    }
    const sorted = comments.slice().sort((a, b) => {
      const ra = a.status === "resolved" ? 1 : 0;
      const rb = b.status === "resolved" ? 1 : 0;
      if (ra !== rb) return ra - rb;
      return b.createdAt - a.createdAt;
    });
    let sectAdded = false;
    for (const t of sorted) {
      // Label the resolved group so the open→resolved break is scannable.
      if (!sectAdded && t.status === "resolved") {
        sectAdded = true;
        const n = sorted.filter((x) => x.status === "resolved").length;
        const sect = document.createElement("div");
        sect.className = "__fa_side_sect";
        sect.textContent = "Resolved (" + n + ")";
        list.appendChild(sect);
      }
      list.appendChild(t.id === expandedCardId ? expandedCard(t) : collapsedCard(t));
    }
  }

  // Collapsed-card header row (the expanded card renders the shared thread
  // body instead — no header, so timestamps never duplicate).
  function cardTopHtml(thread) {
    const replies = thread.replies.length;
    // Foreign threads (AN-34) are tagged with the surface they belong to —
    // "detached" is reserved for anchors that truly no longer resolve.
    const tag = isForeign(thread)
      ? adapter
        ? "preview"
        : "code"
      : detachedIds.has(thread.id)
        ? "detached"
        : "";
    return `
      <div class="__fa_card_top">
        <span class="__fa_carddot${thread.status === "resolved" ? " __fa_carddot_resolved" : ""}">${esc(pinGlyph(thread))}</span>
        <span class="__fa_time">${esc(relTime(thread.updatedAt || thread.createdAt))}</span>
        ${tag ? `<span class="__fa_tag">${tag}</span>` : ""}
        <span class="__fa_spacer"></span>
        ${replies ? `<span class="__fa_card_replies">${replies} ${replies === 1 ? "reply" : "replies"}</span>` : ""}
      </div>`;
  }

  function collapsedCard(thread) {
    const card = document.createElement("div");
    card.className = "__fa_card" + (thread.status === "resolved" ? " __fa_card_resolved" : "");
    card.setAttribute("data-card", thread.id);
    card.innerHTML = cardTopHtml(thread) + `<div class="__fa_card_snip">${esc((thread.content || "").slice(0, 120))}</div>`;
    card.addEventListener("click", () => {
      expandedCardId = thread.id;
      renderSidebar();
      revealAnchor(thread.id); // list → anchor (AN-30)
      scrollCardIntoView(thread.id);
    });
    return card;
  }

  // Expanded card = the SAME thread body the popover renders (AN-12/AN-29),
  // hosted inline in the sidebar. Clicking the message area collapses it;
  // controls keep their own clicks.
  function expandedCard(thread) {
    const card = document.createElement("div");
    card.className = "__fa_card __fa_card_open";
    card.setAttribute("data-card", thread.id);
    renderThreadBody(card, thread, renderSidebar);
    card.addEventListener("click", (e) => {
      if (e.target.closest("textarea, button")) return;
      expandedCardId = null;
      renderSidebar();
    });
    return card;
  }

  function scrollCardIntoView(id) {
    const card = sideListEl().querySelector(`[data-card="${CSS.escape(id)}"]`);
    if (card) card.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  // Anchor → list (AN-31): opening a thread from its pin/range highlights its
  // card in an ALREADY-open sidebar. Never force-opens the panel.
  function focusCard(id) {
    if (!sidebarOpen) return;
    expandedCardId = id;
    renderSidebar();
    scrollCardIntoView(id);
  }

  // List → anchor (AN-30): scroll the thread's anchor into view and flash it.
  // Adapters own their surfaces — delegate when they implement reveal (AN-32).
  function revealAnchor(id) {
    const t = findThread(id);
    if (!t) return;
    if (isForeign(t)) {
      // Anchor lives on the other surface (AN-34) — say where, don't guess.
      showToast("This comment is on the " + (adapter ? "rendered view" : "code view") + " — switch modes to jump to it");
      return;
    }
    if (adapter) {
      if (typeof adapter.reveal === "function") adapter.reveal(id);
      return;
    }
    if (isDetached(t)) return; // tagged "detached" in the card; nowhere to go
    const el = resolveElement(t);
    if (el) {
      el.scrollIntoView({ block: "center", behavior: "smooth" });
      if (t.iu !== undefined && t.iv !== undefined && el.tagName === "IMG") {
        // Image pins: pulse the PIN itself instead of drawing an accent ring
        // — a lime outline vanishes on light/white imagery, the pin's own
        // scale change reads on any background.
        if (!pulsePin(t.id)) {
          const p = pinPoint(t, el);
          flashPoint(p.x, p.y);
        }
      } else {
        flashRect(el);
      }
    } else {
      // Free pin: center it vertically, flash at the stored point.
      const cp = clampPin(t.x || 0, t.y || 0);
      window.scrollTo({ top: Math.max(0, cp.y - window.innerHeight / 2), behavior: "smooth" });
      flashPoint(cp.x, cp.y);
    }
  }

  // Flashes live in #__fa_fx at DOCUMENT coords: they scroll with the content,
  // so smooth-scroll timing can't strand them mid-viewport (AN-30).
  function flashRect(el) {
    const r = el.getBoundingClientRect();
    const f = document.createElement("div");
    f.className = "__fa_flash";
    f.style.left = r.left + window.scrollX + "px";
    f.style.top = r.top + window.scrollY + "px";
    f.style.width = r.width + "px";
    f.style.height = r.height + "px";
    fxEl().appendChild(f);
    setTimeout(() => f.remove(), 1500);
  }

  // Scale-pulse an existing pin (image reveals, AN-30). Returns false when the
  // pin isn't rendered (caller falls back to a coordinate flash).
  function pulsePin(id) {
    const pin = pinsEl().querySelector(`[data-thread="${CSS.escape(id)}"]`);
    if (!pin) return false;
    pin.classList.remove("__fa_pulse");
    void pin.offsetWidth; // restart the animation on repeat reveals
    pin.classList.add("__fa_pulse");
    setTimeout(() => pin.classList.remove("__fa_pulse"), 800);
    return true;
  }

  function flashPoint(x, y) {
    const f = document.createElement("div");
    f.className = "__fa_flash __fa_flash_dot";
    f.style.left = x - 16 + "px";
    f.style.top = y - 16 + "px";
    f.style.width = "32px";
    f.style.height = "32px";
    fxEl().appendChild(f);
    setTimeout(() => f.remove(), 1500);
  }

  // Lightweight reposition: only moves existing pins to follow their anchors and
  // re-checks detachment; used on scroll/resize/RAF. A structural DOM change
  // (MutationObserver) triggers a full render() instead, so attach/detach
  // transitions and new anchor matches are picked up (AN-14).
  function reposition() {
    // If detachment set changed, a full render is required.
    const pins = pinsEl().children;
    let i = 0;
    for (const thread of comments) {
      if (isForeign(thread)) continue; // no pin slot — mirror render() (AN-34)
      if (isDetached(thread)) {
        // A previously-attached thread went detached (or vice versa) → structure
        // changed under us; do a full render to rebuild pins + tray.
        return render();
      }
      const pin = pins[i++];
      if (!pin) return render();
      const el = resolveElement(thread);
      let x = thread.x || 0;
      let y = thread.y || 0;
      if (el) {
        const p = pinPoint(thread, el); // iu/iv-aware for images (AN-25)
        x = p.x;
        y = p.y;
      }
      const cp = clampPin(x, y);
      pin.style.left = cp.x + "px";
      pin.style.top = cp.y + "px";
    }
    if (i !== pins.length) render(); // count mismatch → rebuild
    // Keep an open thread popover glued to its anchor while scrolling. Glue to
    // the PIN's point (pinPoint is iu/iv-aware), not the element's top-right —
    // an image thread's popover must track the pixel pin the user clicked.
    if (openPopover && openPopover.kind === "thread") {
      const t = findThread(openPopover.threadId);
      const el = t ? resolveElement(t) : null;
      if (el) {
        const p = pinPoint(t, el);
        const cx = p.x - window.scrollX;
        const cy = p.y - window.scrollY;
        openPopover.clientX = cx;
        openPopover.clientY = cy;
        positionPopover(openPopover.el, cx, cy);
      }
    }
  }

  // ===========================================================================
  // 11. Wiring — listeners, observers, URL sync.
  // ===========================================================================

  function onKeyDown(e) {
    if (e.key !== "Escape") return;
    // Textareas inside the overlay own their Escape (draft cancel, edit
    // cancel) — this capture-phase handler must not close the sidebar/popover
    // over their heads.
    if (e.target && insideOverlay(e.target) && e.target.tagName === "TEXTAREA") return;
    e.preventDefault();
    // Escape order (AN-33): close an open popover/draft, then the sidebar,
    // then exit annotate (AN-12) by deleting the reserved `_annotate` key on
    // the target shell URL through the same replaceState channel `_comments`
    // uses — the shell's toggle derives its state from the URL
    // (fused:urlchange), so it re-renders the plain iframe.
    if (openPopover) {
      closePopover();
      return;
    }
    if (sidebarOpen) {
      setSidebarOpen(false);
      return;
    }
    const { layoutSpan, rest } = splitSearch(target.location.search);
    const params = new URLSearchParams(rest);
    params.delete("_annotate");
    let search = params.toString();
    if (layoutSpan) search += (search ? "&" : "") + layoutSpan;
    const newUrl = target.location.pathname + (search ? "?" + search : "");
    target.history.replaceState(target.history.state, "", newUrl);
    target.dispatchEvent(new Event("fused:urlchange"));
  }

  // Click-outside closes the popover (AN-12). Registered in the bubble phase so
  // the capture-phase page-click handler (which opens a draft) runs first for
  // genuine page clicks; here we only handle "clicked outside an open popover".
  function onDocClick(e) {
    if (!openPopover) return;
    if (openPopover.el.contains(e.target)) return;
    // A click on a pin opens a different thread (its own handler ran already);
    // don't double-close in that case — the pin handler called openThread which
    // closed+reopened. Detect by checking the target is a pin.
    if (e.target.closest && e.target.closest(".__fa_pin, .__fa_trayitem")) return;
    closePopover();
  }

  // Re-read comments when the shell URL changes from another source (a sibling
  // pane, back/forward, bookmark open). Our own writes set lastJson first, so
  // the diff in loadFromUrl() suppresses the echo.
  function onUrlChange() {
    if (loadFromUrl()) {
      refreshOpenThreadFromUrl();
      render(); // no-op in adapter mode
      emitChange(); // adapter re-resolves + re-renders anchors from new data (AN-17)
    }
  }

  function refreshOpenThreadFromUrl() {
    if (openPopover && openPopover.kind === "thread") {
      const t = findThread(openPopover.threadId);
      if (t) renderThread(openPopover.el, t);
      else closePopover();
    }
  }

  let mutationTimer = null;
  function onMutation(records) {
    // Only PAGE mutations matter (async runPython content re-resolving anchors,
    // AN-14). Our own overlay mutates constantly (highlight tracking, pin
    // repositioning) — reacting to those re-renders pins in a loop, and a pin
    // swapped between mousedown and mouseup means the browser never synthesizes
    // the click. Skip batches that touch only #__fa_root.
    let external = false;
    for (const rec of records) {
      const t = rec.target.nodeType === 1 ? rec.target : rec.target.parentNode;
      if (t && (t === root || root.contains(t))) continue;
      external = true;
      break;
    }
    if (!external) return;
    // Debounced full render: runPython pages inject content asynchronously, so
    // anchors may resolve (re-attach) or vanish (detach) after load (AN-14).
    clearTimeout(mutationTimer);
    mutationTimer = setTimeout(render, 120);
  }

  // Element-mode wiring lives in its own attach/detach pair so an adapter (AN-17)
  // can be registered AFTER start() and cleanly tear it down. mo/repositionInterval
  // are held at module scope so teardown can disconnect them.
  let elementModeWired = false;
  let mo = null;
  let repositionInterval = null;

  function wireElementMode() {
    if (elementModeWired) return;
    elementModeWired = true;
    document.body.classList.add("__fa_active"); // crosshair cursor (AN-10)
    render();

    // First-run guidance: with zero comments the mode is just a crosshair —
    // say what clicking does. (Editor surfaces show their own status line.)
    if (comments.length === 0) {
      showToast("Click any element to leave a comment");
    }

    document.addEventListener("mousemove", onMouseMove, true);
    window.addEventListener("click", onClickCapture, true); // capture: page clicks
    window.addEventListener("pointerdown", onPointerCapture, true);
    window.addEventListener("mousedown", onPointerCapture, true);
    window.addEventListener("mouseup", onPointerCapture, true);
    window.addEventListener("scroll", reposition, true);
    window.addEventListener("resize", reposition);

    // Dynamic pages (async runPython content) — re-resolve anchors when the DOM
    // mutates (AN-14).
    mo = new MutationObserver(onMutation);
    mo.observe(document.body, { childList: true, subtree: true, attributes: true, characterData: false });

    // Safety net for animated/layout-shifting pages the observers miss.
    repositionInterval = setInterval(reposition, 500);
  }

  // Detach everything wireElementMode() attached, so an adapter registered after
  // start() takes over without leftover hover/pins/observers (AN-17).
  function teardownElementMode() {
    if (!elementModeWired) return;
    elementModeWired = false;
    document.body.classList.remove("__fa_active");
    document.removeEventListener("mousemove", onMouseMove, true);
    window.removeEventListener("click", onClickCapture, true);
    window.removeEventListener("pointerdown", onPointerCapture, true);
    window.removeEventListener("mousedown", onPointerCapture, true);
    window.removeEventListener("mouseup", onPointerCapture, true);
    window.removeEventListener("scroll", reposition, true);
    window.removeEventListener("resize", reposition);
    if (mo) {
      mo.disconnect();
      mo = null;
    }
    if (repositionInterval) {
      clearInterval(repositionInterval);
      repositionInterval = null;
    }
    hlEl().style.display = "none";
    pinsEl().innerHTML = "";
    trayEl().style.display = "none";
    // A first-run toast fired before a late adapter registration must not
    // linger over the editor (the element-mode hint is wrong there).
    clearTimeout(toastTimer);
    toastEl().style.display = "none";
  }

  function start() {
    mount();
    loadFromUrl();
    coreReady = true;

    // Sidebar chrome (AN-28) exists in both modes and opens WITH the mode —
    // entering annotate always shows the comment list, pushing content over
    // rather than covering it (margin transition matches the panel slide).
    document.body.style.transition = "margin-right 240ms cubic-bezier(0.32, 0.72, 0, 1)";
    sideBtnEl().addEventListener("click", () => setSidebarOpen(true));
    root.querySelector("#__fa_side_close").addEventListener("click", () => setSidebarOpen(false));
    // The panel is 85vw-capped, so its real width changes with the viewport —
    // keep the pushed margin in sync (both modes; element mode's reposition
    // only moves pins).
    window.addEventListener("resize", () => {
      if (sidebarOpen) document.body.style.marginRight = sideEl().offsetWidth + "px";
    });
    setSidebarOpen(true);

    // Core listeners kept in BOTH modes (AN-17): Escape closes popovers,
    // click-outside dismisses them, urlchange re-reads shared comments.
    document.addEventListener("click", onDocClick, false); // bubble: click-outside
    document.addEventListener("keydown", onKeyDown, true);
    // React to comments written elsewhere (sibling pane, bookmark) on the shared
    // shell URL. Listen on both this window and the target (they differ in
    // layout/tab mode; the runtime dispatches on target).
    target.addEventListener("fused:urlchange", onUrlChange);
    if (target !== window) window.addEventListener("fused:urlchange", onUrlChange);

    // Pre-set global adapter fallback (AN-17): a page whose script ran before us
    // can stash its adapter on window.__fusedAnnotateAdapter.
    if (!adapter && window.__fusedAnnotateAdapter) adapter = window.__fusedAnnotateAdapter;

    if (adapter) {
      initAdapter(); // adapter owns anchor visuals; element mode stays off.
    } else {
      wireElementMode();
    }

    // A page that loads AFTER us (e.g. the code editor's async load()) registers
    // in this event; registerAdapter() also works directly now coreReady is set.
    window.dispatchEvent(new Event("fused-annotate:ready"));
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
