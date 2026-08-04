/* Folder picker — a modal "choose a destination folder" dialog shared by any
 * template that has to put something on disk somewhere the user chooses.
 * Served from the /template-shared/ mount (see server.py) like ro-badge.js and
 * graph-canvas.js, and written in the same shape: an IIFE hanging one object
 * off `window`, no module system, no build step.
 *
 * Load:  <script src="/template-shared/folder-picker.js"></script>
 * Use:   const choice = await fusedFolderPicker.open({
 *          start: "/some/dir",        // where browsing begins
 *          title: "Clone into…",
 *          name: "project",           // optional editable name field
 *          confirmLabel: "Clone",
 *        });
 *        // -> { dir, name, path } , or null if the user cancelled.
 *
 * Why this exists at all: the only NATIVE folder picker in the app is an
 * NSOpenPanel living in the menubar process (macOS-only, PyObjC, main-thread
 * bound), and a template is a sandboxed iframe that cannot reach it — nor can
 * it import the shell's React dialogs. So the picker is built here out of the
 * one thing a template already has: the server's own `/api/fs/list`.
 *
 * Listing goes through the SERVER rather than any local scan for the usual
 * reason (a mount-backed directory must never be walked by a kernel scan), and
 * it means the picker works unchanged for whatever the server can list.
 *
 * Colours come from the host document's palette tokens with dark fallbacks, so
 * the dialog follows a template's light/dark theme automatically (§30) without
 * this file knowing anything about themes.
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

  var CSS = [
    ".fp-backdrop {",
    "  position: fixed; inset: 0; z-index: 1000;",
    "  display: flex; align-items: center; justify-content: center;",
    "  background: rgba(0, 0, 0, 0.45);",
    "}",
    ".fp-card {",
    "  display: flex; flex-direction: column; gap: 10px;",
    "  width: min(520px, 92vw); max-height: 82vh; padding: 16px;",
    "  border-radius: 10px;",
    "  border: 1px solid var(--border, #2a2d33);",
    "  background: var(--bg, #131417);",
    "  color: var(--fg, #e8eaed);",
    "  font-family: -apple-system, BlinkMacSystemFont, \"Segoe UI\", Roboto, Helvetica, Arial, sans-serif;",
    "  font-size: 13px;",
    "  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.45);",
    "}",
    ".fp-title { font-size: 14px; font-weight: 600; }",
    ".fp-where {",
    "  color: var(--fg-muted, #9aa0a6); font-size: 12px;",
    "  overflow-wrap: anywhere; word-break: break-word;",
    "}",
    ".fp-list {",
    "  flex: 1 1 auto; min-height: 120px; overflow-y: auto;",
    "  border: 1px solid var(--border, #2a2d33); border-radius: 6px;",
    "}",
    ".fp-row {",
    "  display: flex; align-items: center; gap: 8px;",
    "  padding: 7px 10px; cursor: pointer;",
    "}",
    ".fp-row:hover { background: var(--bg-alt, #1b1d21); }",
    ".fp-row[aria-selected=\"true\"] { background: var(--bg-alt, #1b1d21); }",
    ".fp-empty, .fp-error { padding: 12px 10px; color: var(--fg-muted, #9aa0a6); }",
    ".fp-error { color: var(--error, #ff6b6b); }",
    ".fp-name { display: flex; align-items: center; gap: 8px; }",
    ".fp-name label { color: var(--fg-muted, #9aa0a6); }",
    ".fp-name input {",
    "  flex: 1 1 auto; font: inherit; padding: 6px 8px;",
    "  border: 1px solid var(--border, #2a2d33); border-radius: 6px;",
    "  background: var(--bg-alt, #1b1d21); color: var(--fg, #e8eaed);",
    "}",
    ".fp-actions { display: flex; justify-content: flex-end; gap: 8px; }",
    ".fp-actions button {",
    "  font: inherit; padding: 6px 12px; cursor: pointer;",
    "  border: 1px solid var(--border, #2a2d33); border-radius: 6px;",
    "  background: var(--bg, #131417); color: var(--fg, #e8eaed);",
    "}",
    ".fp-actions button:hover:not(:disabled) { background: var(--bg-alt, #1b1d21); }",
    ".fp-actions button:disabled { opacity: 0.5; cursor: default; }",
  ].join("\n");

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

  function open(options) {
    var opts = options || {};
    ensureCss();
    return new Promise(function (resolve) {
      var current = opts.start || "/";
      var takenNames = [];

      var backdrop = document.createElement("div");
      backdrop.className = "fp-backdrop";
      var card = document.createElement("div");
      card.className = "fp-card";
      backdrop.appendChild(card);

      card.innerHTML =
        '<div class="fp-title"></div>' +
        '<div class="fp-where"></div>' +
        '<div class="fp-list"></div>' +
        (opts.name === undefined ? "" :
          '<div class="fp-name"><label>Name</label><input type="text" /></div>') +
        '<div class="fp-actions">' +
        '<button data-act="up">Up</button>' +
        '<button data-act="cancel">Cancel</button>' +
        '<button data-act="ok"></button></div>';

      var titleEl = card.querySelector(".fp-title");
      var whereEl = card.querySelector(".fp-where");
      var listEl = card.querySelector(".fp-list");
      var nameEl = card.querySelector(".fp-name input");
      var upBtn = card.querySelector('[data-act="up"]');
      var okBtn = card.querySelector('[data-act="ok"]');
      var cancelBtn = card.querySelector('[data-act="cancel"]');

      titleEl.textContent = opts.title || "Choose a folder";
      okBtn.textContent = opts.confirmLabel || "Choose";
      if (nameEl) nameEl.value = opts.name || "";

      function finish(value) {
        document.removeEventListener("keydown", onKey, true);
        if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
        resolve(value);
      }

      /* Cancel resolves null and touches nothing else: the caller has made no
       * change at this point, so there is nothing to undo. */
      function onKey(e) {
        if (e.key === "Escape") { e.stopPropagation(); finish(null); }
        else if (e.key === "Enter" && !okBtn.disabled) { e.stopPropagation(); confirm(); }
      }

      function confirm() {
        var name = nameEl ? nameEl.value.trim() : "";
        if (nameEl && !name) return;
        finish({
          dir: current,
          name: name,
          path: nameEl ? join(current, name) : current,
        });
      }

      function renderList(state) {
        current = state.path;
        whereEl.textContent = current;
        upBtn.disabled = isRoot(current);
        takenNames = state.names;
        // Only re-propose a name when the current one would collide; an edited
        // name the user typed must survive navigating between folders.
        if (nameEl && opts.name !== undefined) {
          var typed = nameEl.value.trim();
          if (!typed) nameEl.value = freeName(opts.name, takenNames);
          else if (takenNames.indexOf(typed) !== -1) {
            nameEl.value = freeName(typed.replace(/ \d+$/, ""), takenNames);
          }
        }
        if (!state.names.length) {
          listEl.innerHTML = '<div class="fp-empty">No sub-folders here — ' +
            "you can still choose this folder.</div>";
          return;
        }
        listEl.innerHTML = state.names.map(function (n) {
          return '<div class="fp-row" data-name="' + esc(n) + '">📁 ' + esc(n) + "</div>";
        }).join("") + (state.truncated
          ? '<div class="fp-empty">…more folders than can be listed here.</div>' : "");
      }

      function go(dir) {
        listEl.innerHTML = '<div class="fp-empty">Loading…</div>';
        okBtn.disabled = true;
        listDirs(dir).then(function (state) {
          renderList(state);
          okBtn.disabled = false;
        }, function (err) {
          // A folder that cannot be listed is a dead end, not a dead dialog:
          // say so and leave Up working so the user can back out of it.
          whereEl.textContent = dir;
          current = dir;
          upBtn.disabled = isRoot(dir);
          listEl.innerHTML = '<div class="fp-error">' +
            esc(err && err.message ? err.message : "Cannot open this folder") + "</div>";
          okBtn.disabled = true;
        });
      }

      listEl.addEventListener("click", function (e) {
        var row = e.target && e.target.closest ? e.target.closest(".fp-row") : null;
        if (row) go(join(current, row.dataset.name));
      });
      upBtn.onclick = function () { go(parent(current)); };
      cancelBtn.onclick = function () { finish(null); };
      okBtn.onclick = confirm;
      backdrop.onclick = function (e) { if (e.target === backdrop) finish(null); };
      document.addEventListener("keydown", onKey, true);

      document.body.appendChild(backdrop);
      go(current);
      if (nameEl) nameEl.focus();
    });
  }

  window.fusedFolderPicker = {
    open: open,
    // The pure path logic, exported for its unit tests (and for a caller that
    // needs to derive the same names without opening a dialog).
    paths: { parent: parent, join: join, isRoot: isRoot, freeName: freeName },
  };
})(typeof window !== "undefined" ? window : this);
