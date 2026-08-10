/* Folder picker — "choose a destination folder", shared by any template that
 * has to put something on disk somewhere the user chooses.
 * Served from the /template-shared/ mount (see server.py) like ro-badge.js and
 * graph-canvas.js, and written in the same shape: an IIFE hanging one object
 * off `window`, no module system, no build step.
 *
 * Load:  <script src="/template-shared/folder-picker.js"></script>
 * Use:   const choice = await fusedFolderPicker.open({
 *          start: "/some/dir",        // where browsing begins
 *          title: "Clone into…",
 *          name: "project",           // optional destination NAME to derive
 *          confirmLabel: "Clone",
 *        });
 *        // -> { dir, name, path } , or null if the user cancelled.
 *
 * TWO backends, one call. When the shell can raise a real OS dialog it does —
 * `POST /api/fs/pick-folder` runs NSOpenPanel / IFileDialog / zenity in the
 * server process, which on the desktop app is the same process as the UI. That
 * is what a user asking for a folder picker actually wants: their own file
 * chooser, with its sidebar, favourites, search and New Folder button.
 * `/api/config` advertises the capability as `native_dir_picker`, so a hosted
 * deploy — no GUI session to raise a dialog into — never waits on a dialog
 * nobody can see.
 *
 * The in-page dialog below is the fallback for exactly that case, and for a
 * native dialog that fails for any reason OTHER than the user cancelling: a
 * cancel is an answer and is passed straight through, never re-asked in HTML.
 *
 * Listing goes through the SERVER rather than any local scan for the usual
 * reason (a mount-backed directory must never be walked by a kernel scan), and
 * it means the picker works unchanged for whatever the server can list.
 *
 * A native folder dialog returns an existing directory and nothing else, so
 * `opts.name` is resolved the same way in both backends: the free name is
 * derived from a listing of the chosen folder (see `freeName`), which is also
 * the name the server would have derived by itself. The in-page dialog
 * additionally lets it be edited.
 *
 * Appearance: the CSS paints only through this file's own `--fp-*` tokens,
 * defined twice — once for dark, once under `:root[data-theme="light"]` — and
 * mapped there onto whichever palette the host template declares. Two
 * vocabularies are in use across the built-in templates (`--ink`/`--line`/
 * `--surface` in git/history, `--fg`/`--border`/`--bg-alt` in the other
 * nineteen), which is why each mapping is a var() chain and why it lives in one
 * place instead of at every use site. Reaching for a host token directly — as
 * this file used to — is a colour that resolves in three views and silently
 * falls through to a hardcoded dark hex in the rest. tests/test_theme.py
 * enforces both halves; it did not use to look at this layer at all.
 */
(function (window) {
  "use strict";

  var document = window.document;

  /* Windows paths are rooted at a drive letter ("C:/…"), not at "/" — the same
   * rule the shell's own path helpers follow (frontend/src/lib/fs-actions.ts).
   * The canonical drive root is "C:/": a bare "C:" is cwd-relative to the
   * server's os.stat and must never be sent as a directory path. */
  var DRIVE_RE = /^[A-Za-z]:/;

  function isRoot(p) {
    return p === "/" || /^[A-Za-z]:\/?$/.test(p);
  }

  /* One level up. The root is its own parent, which is what stops the Up
   * button from walking off the top of the filesystem. */
  function parent(p) {
    var norm = String(p || "").replace(/\/+$/, "");
    var drive = DRIVE_RE.test(norm) ? norm.slice(0, 2) : null;
    if (drive && norm.length === drive.length) return drive + "/";
    var i = norm.lastIndexOf("/");
    if (drive) return i === drive.length ? drive + "/" : norm.slice(0, i);
    return i <= 0 ? "/" : norm.slice(0, i);
  }

  function join(dir, name) {
    var d = String(dir || "");
    return d.charAt(d.length - 1) === "/" ? d + name : d + "/" + name;
  }

  /* The last segment of a path — the label a breadcrumb or a shortcut button
   * shows. A root has no last segment, so it answers "" and the caller
   * substitutes the path itself; "C:" (what stripping the trailing slash would
   * leave) is not a thing a user should ever be shown. */
  function basename(p) {
    if (isRoot(p)) return "";
    var norm = String(p || "").replace(/\/+$/, "");
    var i = norm.lastIndexOf("/");
    return i < 0 ? norm : norm.slice(i + 1);
  }

  /* The first name in `base`, "base 2", "base 3"… not already taken. Mirrors
   * reader.py's `_free_dest` numbering, so a name proposed here is the one the
   * server would have derived on its own. Bounded like it too: the server
   * re-checks existence anyway, so giving up after a hundred tries costs
   * nothing worse than a refusal the user can see. */
  function freeName(base, taken) {
    var used = {};
    for (var i = 0; i < (taken || []).length; i++) used[taken[i]] = true;
    if (!used[base]) return base;
    for (var n = 2; n < 100; n++) {
      if (!used[base + " " + n]) return base + " " + n;
    }
    return base + " " + Date.now();
  }

  /* `dir` and every ancestor of it, root first, as {path, label} — the
   * breadcrumb. A path display is not decoration: the one thing a folder
   * chooser must never be is vague about where it is pointing. */
  function crumbs(dir) {
    var out = [];
    var at = String(dir || "/");
    for (var guard = 0; guard < 200; guard++) {
      out.unshift({ path: at, label: basename(at) || at });
      if (isRoot(at)) break;
      at = parent(at);
    }
    return out;
  }

  /* ---------------------------------------------------------------- styles */

  var CSS = `
.fp-backdrop {
  /* PALETTE (dark). Every colour below is either a literal, which belongs in a
     palette block and only here, or a host token this maps onto. Order in each
     chain: the canonical token name, then the older one, then the literal for a
     host template that declares neither. */
  --fp-scrim: rgba(0, 0, 0, 0.5);
  --fp-shadow: rgba(0, 0, 0, 0.5);
  --fp-card: var(--surface, var(--bg, #16181c));
  /* Derived from the card rather than mapped onto a host token, and not for
     tidiness: --surface-2 / --bg-alt are the nearest host names, but a template
     declaring only --bg and --bg-alt leaves the card on --bg, and the list inset
     would then land on the SAME colour — a list with no edge (seen for real in
     the bundle viewer). A mix against the ink is guaranteed to contrast in
     either theme: light ink over a dark card, dark ink over a light one.
     NB no backticks anywhere in this stylesheet — it is a template literal, and
     one in a comment ends it mid-rule. That is a syntax error the whole file
     dies of, which is why a test parses this file rather than trusting it. */
  --fp-inset: color-mix(in srgb, var(--fp-ink) 6%, var(--fp-card));
  --fp-hover: color-mix(in srgb, var(--fp-ink) 13%, var(--fp-card));
  --fp-line: var(--line, var(--border, #2a2d33));
  --fp-line-hover: var(--line-hover, var(--line-strong, var(--border, #454a53)));
  --fp-ink: var(--ink, var(--fg, #e8eaed));
  --fp-ink-2: var(--ink-2, var(--fg-muted, #9aa0a6));
  --fp-ink-3: var(--ink-3, var(--fg-muted, #868d94));
  --fp-accent: var(--accent, #E5FF44);
  --fp-accent-dim: var(--accent-dim, var(--accent, #b8cc36));
  --fp-on-accent: var(--on-accent, var(--bg, #131417));
  --fp-error: var(--error, #ff6b6b);

  position: fixed; inset: 0; z-index: 1000;
  display: flex; align-items: center; justify-content: center;
  padding: 10px;
  background: var(--fp-scrim);
}
:root[data-theme="light"] .fp-backdrop {
  /* PALETTE (light). Identical token set — the host chains are the same, since a
     host that declares a light palette redefines those tokens itself. What
     differs is the last-resort literals and the two roles no host vocabulary has
     a token for: a scrim and a drop shadow. */
  --fp-scrim: rgba(28, 30, 34, 0.32);
  --fp-shadow: rgba(28, 30, 34, 0.22);
  --fp-card: var(--surface, var(--bg, #ffffff));
  --fp-inset: color-mix(in srgb, var(--fp-ink) 5%, var(--fp-card));
  --fp-hover: color-mix(in srgb, var(--fp-ink) 11%, var(--fp-card));
  --fp-line: var(--line, var(--border, #d8dade));
  --fp-line-hover: var(--line-hover, var(--line-strong, var(--border, #b4b9c0)));
  --fp-ink: var(--ink, var(--fg, #1f2023));
  --fp-ink-2: var(--ink-2, var(--fg-muted, #61656c));
  --fp-ink-3: var(--ink-3, var(--fg-muted, #6f747c));
  --fp-accent: var(--accent, #5f7300);
  --fp-accent-dim: var(--accent-dim, var(--accent, #4e5f00));
  --fp-on-accent: var(--on-accent, #ffffff);
  --fp-error: var(--error, #b3261e);
}
.fp-card {
  display: flex; flex-direction: column; gap: 9px;
  /* Fluid in BOTH axes and never wider than what it is inside: this dialog opens
     in a preview iframe that can be ~336px across (a narrow split), where a
     fixed pixel width overflows the pane and clips its own buttons. */
  width: 100%; max-width: 520px; min-width: 0;
  max-height: 100%; padding: 13px;
  box-sizing: border-box;
  border-radius: 10px;
  border: 1px solid var(--fp-line);
  background: var(--fp-card);
  color: var(--fp-ink);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  font-size: 13px;
  box-shadow: 0 14px 44px var(--fp-shadow);
}
.fp-title { font-size: 14px; font-weight: 600; }

/* ---- shortcuts + breadcrumb ----------------------------------------------- */
/* Both wrap: a deep path or a fourth shortcut must push the list down, never
   sideways out of the pane. */
.fp-crumbs, .fp-places {
  display: flex; flex-wrap: wrap; align-items: center; min-width: 0;
}
.fp-crumbs { gap: 1px; }
.fp-places { gap: 6px; }
.fp-crumb, .fp-place {
  font: inherit; padding: 4px 7px; min-height: 26px;
  border: 1px solid transparent; border-radius: 6px;
  background: none; color: var(--fp-ink-2); cursor: pointer;
  max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.fp-place { border-color: var(--fp-line); }
.fp-crumb:hover, .fp-place:hover { background: var(--fp-hover); color: var(--fp-ink); }
.fp-crumb[aria-current="true"] { color: var(--fp-ink); font-weight: 600; }
.fp-sep { color: var(--fp-ink-3); }
.fp-crumb:focus-visible, .fp-place:focus-visible,
.fp-actions button:focus-visible, .fp-name input:focus-visible {
  outline: 2px solid var(--fp-accent); outline-offset: 1px;
}
/* The list gets an accent EDGE rather than the ring: it is a big element, and a
   2px ring around it drowns out the selected row inside it — which is the thing
   the keyboard is actually moving. */
.fp-list:focus-visible { outline: none; border-color: var(--fp-accent); }

/* ---- list ---------------------------------------------------------------- */
.fp-list {
  flex: 1 1 auto; min-height: 128px; overflow-y: auto; overflow-x: hidden;
  border: 1px solid var(--fp-line); border-radius: 8px;
  background: var(--fp-inset);
}
.fp-row {
  display: flex; align-items: center; gap: 8px;
  /* 32px is the smallest row still comfortable to hit with a mouse in a narrow
     pane; the icon is a fixed column so the names line up under each other. */
  padding: 6px 9px; min-height: 32px; cursor: default;
  border-left: 2px solid transparent;
}
.fp-icon { flex: 0 0 auto; color: var(--fp-ink-2); }
.fp-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fp-row:hover { background: var(--fp-hover); }
.fp-row[aria-selected="true"] {
  background: color-mix(in srgb, var(--fp-accent) 16%, var(--fp-card));
  border-left-color: var(--fp-accent);
}
.fp-row[aria-selected="true"] .fp-icon { color: var(--fp-accent); }
.fp-note, .fp-error { padding: 11px 10px; color: var(--fp-ink-2); }
.fp-error { color: var(--fp-error); }
.fp-hint { color: var(--fp-ink-3); font-size: 11px; }

/* ---- what will be written ------------------------------------------------ */
.fp-dest { color: var(--fp-ink-2); font-size: 12px; min-width: 0; }
.fp-dest strong {
  color: var(--fp-ink); font-weight: 600;
  overflow-wrap: anywhere; word-break: break-word;
}

/* ---- name field ---------------------------------------------------------- */
.fp-name { display: flex; align-items: center; gap: 8px; min-width: 0; }
.fp-name label { color: var(--fp-ink-2); flex: 0 0 auto; }
.fp-name input {
  flex: 1 1 auto; min-width: 0; font: inherit; padding: 6px 8px;
  border: 1px solid var(--fp-line); border-radius: 6px;
  background: var(--fp-inset); color: var(--fp-ink);
}
.fp-name input:hover { border-color: var(--fp-line-hover); }

/* ---- actions ------------------------------------------------------------- */
.fp-actions {
  display: flex; flex-wrap: wrap; justify-content: flex-end;
  align-items: center; gap: 8px;
}
.fp-actions .fp-spacer { flex: 1 1 auto; }
.fp-actions button {
  font: inherit; padding: 6px 12px; min-height: 30px; cursor: pointer;
  border: 1px solid var(--fp-line); border-radius: 6px;
  background: var(--fp-card); color: var(--fp-ink);
}
.fp-actions button:hover:not(:disabled) {
  background: var(--fp-hover); border-color: var(--fp-line-hover);
}
.fp-actions button.fp-primary {
  background: var(--fp-accent); border-color: var(--fp-accent);
  color: var(--fp-on-accent); font-weight: 600;
}
.fp-actions button.fp-primary:hover:not(:disabled) {
  background: var(--fp-accent-dim); border-color: var(--fp-accent-dim);
}
.fp-actions button:disabled { color: var(--fp-ink-3); cursor: default; }
.fp-actions button.fp-primary:disabled {
  background: color-mix(in srgb, var(--fp-accent) 32%, transparent);
  border-color: transparent; color: var(--fp-ink-3);
}
`;

  var injected = false;
  function ensureCss() {
    if (injected || !document) return;
    injected = true;
    var style = document.createElement("style");
    style.textContent = CSS;
    document.head.appendChild(style);
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  /* ------------------------------------------------------------- server I/O */

  /* Directories only: this picker chooses a folder, so files are not listed at
   * all rather than listed-and-disabled — a list of things you cannot pick is
   * noise. Hidden and ignored entries are dropped for the same reason the
   * shell hides them by default. */
  function listDirs(dir) {
    var url = "/api/fs/list?path=" + encodeURIComponent(dir);
    return fetch(url).then(function (res) {
      return res.json().then(function (data) {
        if (!res.ok) throw new Error(data && data.error ? data.error : "cannot list folder");
        return {
          path: data.path || dir,
          names: (data.entries || [])
            .filter(function (e) { return e.is_dir && e.name.charAt(0) !== "."; })
            .map(function (e) { return e.name; }),
          truncated: !!data.truncated,
        };
      });
    });
  }

  /* /api/config, fetched at most once per page: it carries the native-picker
   * capability flag plus the two folders worth a shortcut button. Never
   * rejects — a picker that cannot read the config is a picker with no
   * shortcuts and no native backend, not a broken one. */
  var configPromise = null;
  function appConfig() {
    if (!configPromise) {
      configPromise = fetch("/api/config").then(function (res) {
        return res.ok ? res.json() : {};
      }).catch(function () { return {}; });
    }
    return configPromise;
  }

  /* The real OS dialog. Resolves the chosen absolute path, or null when the
   * user cancelled — a cancel is an ANSWER, so it must not be reported as a
   * failure and must not be re-asked in HTML. Anything else rejects, and the
   * caller falls back to the in-page dialog. */
  function pickNative(start, title) {
    return fetch("/api/fs/pick-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Fused": "1" },
      // The caller's title rides along: the OS dialog is the one the user sees,
      // so it has to say what it is FOR ("Clone this bundle into…"), not carry
      // the generic default while the in-page fallback gets the real wording.
      body: JSON.stringify({ start: start || null, title: title || null }),
    }).then(function (res) {
      return res.json().then(function (data) {
        if (!res.ok) {
          throw new Error(
            data && data.error ? data.error : "the native folder chooser failed");
        }
        return data.path || null;
      });
    });
  }

  /* --------------------------------------------------------------- choosing */

  /* A chosen directory turned into the caller's answer. With `opts.name` set the
   * answer names a NEW folder inside it, so the name is derived against a
   * listing of that folder — the same derivation the in-page dialog seeds its
   * name field with. A listing that fails is not fatal: the server re-checks the
   * destination anyway, so the unnumbered name is a fine proposal to be
   * refused, and a refusal the user can read beats a dialog that will not
   * close. */
  function toChoice(dir, opts) {
    if (opts.name === undefined) {
      return Promise.resolve({ dir: dir, name: "", path: dir });
    }
    return listDirs(dir).then(function (state) {
      return state.names;
    }, function () {
      return [];
    }).then(function (taken) {
      var name = freeName(opts.name, taken);
      return { dir: dir, name: name, path: join(dir, name) };
    });
  }

  function open(options) {
    var opts = options || {};
    // Always a promise and never a synchronous throw: a function that rejects on
    // one branch and throws on another skips half its callers' error handling.
    if (opts.native === false) {
      return Promise.resolve().then(function () { return openDialog(opts); });
    }
    return appConfig().then(function (config) {
      if (!config || !config.native_dir_picker) return openDialog(opts);
      return pickNative(opts.start || "/", opts.title).then(function (dir) {
        if (dir === null) return null;  // cancelled: the user's answer, honoured
        return toChoice(dir, opts);
      }, function (err) {
        // Unavailable, busy, or broken — log it once and use the same fallback
        // the hosted case uses. Silence here would look like a dead button.
        if (window.console) window.console.warn("native folder picker:", err);
        return openDialog(opts);
      });
    });
  }

  /* ---------------------------------------------------- the in-page dialog */

  function openDialog(opts) {
    ensureCss();
    return new Promise(function (resolve) {
      var current = opts.start || "/";
      var subdirs = [];        // the folders listed in `current`
      var selected = null;     // highlighted sub-folder name, or null = "this one"

      var backdrop = document.createElement("div");
      backdrop.className = "fp-backdrop";
      var card = document.createElement("div");
      card.className = "fp-card";
      card.setAttribute("role", "dialog");
      card.setAttribute("aria-modal", "true");
      backdrop.appendChild(card);

      card.innerHTML =
        '<div class="fp-title"></div>' +
        '<div class="fp-places"></div>' +
        '<div class="fp-crumbs"></div>' +
        '<div class="fp-list" tabindex="0" role="listbox" aria-label="Folders"></div>' +
        '<div class="fp-hint">Click a folder to choose it, double-click (or →) ' +
        "to go into it. ← goes back up.</div>" +
        '<div class="fp-dest"></div>' +
        (opts.name === undefined ? "" :
          '<div class="fp-name"><label for="fp-name-input">Name</label>' +
          '<input id="fp-name-input" type="text" spellcheck="false" ' +
          'autocapitalize="off" autocomplete="off" /></div>') +
        '<div class="fp-actions">' +
        '<button type="button" data-act="up" title="Go to the enclosing folder">Up</button>' +
        '<span class="fp-spacer"></span>' +
        '<button type="button" data-act="cancel">Cancel</button>' +
        '<button type="button" class="fp-primary" data-act="ok"></button></div>';

      var titleEl = card.querySelector(".fp-title");
      var placesEl = card.querySelector(".fp-places");
      var crumbsEl = card.querySelector(".fp-crumbs");
      var listEl = card.querySelector(".fp-list");
      var destEl = card.querySelector(".fp-dest");
      var nameEl = card.querySelector(".fp-name input");
      var upBtn = card.querySelector('[data-act="up"]');
      var okBtn = card.querySelector('[data-act="ok"]');
      var cancelBtn = card.querySelector('[data-act="cancel"]');

      titleEl.textContent = opts.title || "Choose a folder";
      okBtn.textContent = opts.confirmLabel || "Choose";
      if (nameEl) nameEl.value = opts.name || "";

      /* The folder the buttons act on: the highlighted sub-folder if there is
       * one, else the folder being browsed. NSOpenPanel's own rule, so a click
       * on a row does not force you to enter a folder just to pick it — which
       * is what this dialog used to do. */
      function targetDir() {
        return selected === null ? current : join(current, selected);
      }

      function finish(value) {
        document.removeEventListener("keydown", onKey, true);
        if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
        resolve(value);
      }

      function confirm() {
        if (okBtn.disabled) return;
        var dir = targetDir();
        if (opts.name === undefined) return finish({ dir: dir, name: "", path: dir });
        var name = nameEl ? nameEl.value.trim() : opts.name;
        if (!name) return;
        finish({ dir: dir, name: name, path: join(dir, name) });
      }

      /* Cancel resolves null and touches nothing else: the caller has made no
       * change at this point, so there is nothing to undo. */
      function onKey(e) {
        if (e.defaultPrevented) return;
        var typing = nameEl && e.target === nameEl;
        if (e.key === "Escape") {
          e.stopPropagation(); e.preventDefault(); return finish(null);
        }
        if (e.key === "Enter") {
          e.stopPropagation(); e.preventDefault(); return confirm();
        }
        // Arrows and Backspace belong to the text field while it has focus.
        if (typing) return;
        if (e.key === "ArrowDown") { e.preventDefault(); return moveSelection(1); }
        if (e.key === "ArrowUp") { e.preventDefault(); return moveSelection(-1); }
        if (e.key === "ArrowRight") {
          e.preventDefault();
          if (selected !== null) go(join(current, selected));
          return;
        }
        if (e.key === "ArrowLeft" || e.key === "Backspace") {
          e.preventDefault();
          if (!isRoot(current)) go(parent(current));
          return;
        }
      }

      /* Index -1 is "this folder" — walking off the top of the list returns the
       * choice to the folder being browsed rather than sticking on its first
       * child, so every state the mouse can reach is reachable by keyboard. */
      function moveSelection(delta) {
        if (!subdirs.length) return;
        var at = selected === null ? -1 : subdirs.indexOf(selected);
        var next = at + delta;
        if (next < -1) next = -1;
        if (next >= subdirs.length) next = subdirs.length - 1;
        select(next < 0 ? null : subdirs[next], true);
      }

      function select(name, scroll) {
        selected = name;
        var rows = listEl.querySelectorAll(".fp-row");
        for (var i = 0; i < rows.length; i++) {
          var isIt = rows[i].dataset.name === name;
          rows[i].setAttribute("aria-selected", isIt ? "true" : "false");
          if (isIt && scroll && rows[i].scrollIntoView) {
            rows[i].scrollIntoView({ block: "nearest" });
          }
        }
        renderDest();
      }

      /* What pressing the confirm button will actually write. Shown because the
       * target is not always the folder in the breadcrumb (a highlighted row
       * wins), and a destination the user has to infer is a destination they
       * will get wrong. */
      function renderDest() {
        var dir = targetDir();
        var typed = nameEl ? nameEl.value.trim() : "";
        var full = opts.name === undefined ? dir : join(dir, typed || opts.name);
        destEl.innerHTML = "Destination <strong></strong>";
        destEl.querySelector("strong").textContent = full;
      }

      function renderCrumbs(dir) {
        var parts = crumbs(dir);
        var html = "";
        for (var i = 0; i < parts.length; i++) {
          // No separator after a root: the root's own label IS "/" (or "C:/"),
          // so one would read as "/ / Users".
          if (i && !isRoot(parts[i - 1].path)) html += '<span class="fp-sep">/</span>';
          html += '<button type="button" class="fp-crumb" data-path="' +
            esc(parts[i].path) + '"' +
            (i === parts.length - 1 ? ' aria-current="true"' : "") +
            ">" + esc(parts[i].label) + "</button>";
        }
        crumbsEl.innerHTML = html;
      }

      /* One-click shortcuts to the folders a user actually wants: their home,
       * the Fused workspace, and where this picker was pointed at to begin with
       * (for the bundle viewer, the bundle's own folder) — so browsing back to
       * the obvious answer is never a walk up the tree. */
      function renderPlaces(config) {
        var places = [];
        if (config && config.home) places.push({ label: "Home", path: config.home });
        if (config && config.fused_dir) {
          places.push({ label: "Fused", path: config.fused_dir });
        }
        if (opts.start && !isRoot(opts.start)) {
          places.push({ label: basename(opts.start), path: opts.start });
        }
        for (var j = 0; j < (opts.places || []).length; j++) places.push(opts.places[j]);
        var seen = {}, html = "";
        for (var i = 0; i < places.length; i++) {
          var place = places[i];
          if (!place || !place.path || !place.label || seen[place.path]) continue;
          seen[place.path] = true;
          html += '<button type="button" class="fp-place" data-path="' +
            esc(place.path) + '" title="' + esc(place.path) + '">' +
            esc(place.label) + "</button>";
        }
        placesEl.innerHTML = html;
      }

      function renderList(state) {
        current = state.path;
        selected = null;
        renderCrumbs(current);
        upBtn.disabled = isRoot(current);
        subdirs = state.names;
        // Only re-propose a name when the current one would collide; an edited
        // name the user typed must survive navigating between folders.
        if (nameEl && opts.name !== undefined) {
          var typed = nameEl.value.trim();
          if (!typed) nameEl.value = freeName(opts.name, subdirs);
          else if (subdirs.indexOf(typed) !== -1) {
            nameEl.value = freeName(typed.replace(/ \d+$/, ""), subdirs);
          }
        }
        if (!subdirs.length) {
          listEl.innerHTML = '<div class="fp-note">No sub-folders here — ' +
            "you can still choose this folder.</div>";
        } else {
          listEl.innerHTML = subdirs.map(function (n) {
            return '<div class="fp-row" role="option" aria-selected="false" ' +
              'data-name="' + esc(n) + '"><span class="fp-icon">📁</span>' +
              '<span class="fp-label">' + esc(n) + "</span></div>";
          }).join("") + (state.truncated
            ? '<div class="fp-note">…more folders than can be listed here.</div>' : "");
        }
        renderDest();
      }

      function go(dir) {
        // The breadcrumb moves BEFORE the listing lands, so a slow directory
        // shows where it is going instead of freezing on the previous path.
        current = dir;
        selected = null;
        subdirs = [];
        renderCrumbs(dir);
        renderDest();
        listEl.setAttribute("aria-busy", "true");
        listEl.innerHTML = '<div class="fp-note">Loading…</div>';
        okBtn.disabled = true;
        listDirs(dir).then(function (state) {
          listEl.removeAttribute("aria-busy");
          renderList(state);
          okBtn.disabled = false;
        }, function (err) {
          // A folder that cannot be listed is a dead end, not a dead dialog:
          // say so and leave Up working so the user can back out of it.
          listEl.removeAttribute("aria-busy");
          upBtn.disabled = isRoot(dir);
          listEl.innerHTML = '<div class="fp-error">' +
            esc(err && err.message ? err.message : "Cannot open this folder") + "</div>";
          okBtn.disabled = true;
        });
      }

      function rowOf(e) {
        return e.target && e.target.closest ? e.target.closest(".fp-row") : null;
      }
      listEl.addEventListener("click", function (e) {
        var row = rowOf(e);
        if (row) select(row.dataset.name, false);
      });
      listEl.addEventListener("dblclick", function (e) {
        var row = rowOf(e);
        if (row) go(join(current, row.dataset.name));
      });
      function navClick(e) {
        var btn = e.target && e.target.closest ? e.target.closest("[data-path]") : null;
        if (btn) go(btn.dataset.path);
      }
      crumbsEl.addEventListener("click", navClick);
      placesEl.addEventListener("click", navClick);
      upBtn.onclick = function () { go(parent(current)); };
      cancelBtn.onclick = function () { finish(null); };
      okBtn.onclick = confirm;
      backdrop.onclick = function (e) { if (e.target === backdrop) finish(null); };
      if (nameEl) nameEl.addEventListener("input", renderDest);
      document.addEventListener("keydown", onKey, true);

      document.body.appendChild(backdrop);
      // The shortcut buttons need /api/config; the dialog must not wait for it.
      appConfig().then(renderPlaces);
      go(current);
      if (nameEl) nameEl.focus(); else listEl.focus();
    });
  }

  window.fusedFolderPicker = {
    open: open,
    // The in-page dialog on its own, for a caller that wants the browser UI
    // whatever the shell is capable of (and for the tests of that UI).
    openDialog: openDialog,
    // The pure path logic, exported for its unit tests (and for a caller that
    // needs to derive the same names without opening a dialog).
    paths: {
      parent: parent, join: join, isRoot: isRoot, basename: basename,
      freeName: freeName, crumbs: crumbs,
    },
  };
})(typeof window !== "undefined" ? window : this);
