/* Read-only badge + instant tooltip — shared by every template with an edit
 * surface (sqlite, duckdb, code, annotate). Extracted (2026-07) from the
 * byte-identical copies sqlite/ and duckdb/ carried; served from the
 * /template-shared/ mount (see server.py) like sciviz.mjs.
 *
 * Load:  <script src="/template-shared/ro-badge.js"></script>
 * Place: <span id="ro" hidden></span> wherever the toolbar wants it.
 * Use:   fusedRoBadge.update(el, message, tooltip)
 *        — non-empty message shows the badge (text + info icon, tooltip on
 *          hover); empty/undefined hides it. Styles inject on first update.
 */
(function () {
  "use strict";

  var CSS = [
    ".ro-badge {",
    "  position: relative;",
    "  display: inline-flex; align-items: center; gap: 5px;",
    "  color: #9aa0a6; font-size: 12px; cursor: help;",
    "}",
    /* display:inline-flex above beats the `hidden` attribute's UA
       display:none, so an editable file would otherwise show a stray icon. */
    ".ro-badge[hidden] { display: none; }",
    ".ro-badge::after {",
    "  content: \"i\"; display: inline-flex; align-items: center; justify-content: center;",
    "  width: 13px; height: 13px; border-radius: 50%; border: 1px solid currentColor;",
    "  font-size: 9px; font-style: italic; font-weight: 700; line-height: 1; opacity: 0.75;",
    "}",
    /* Custom tooltip: appears instantly on hover, unlike the native title's delay. */
    ".ro-tip {",
    "  position: absolute; bottom: calc(100% + 6px); right: 0; z-index: 10;",
    "  width: max-content; max-width: 280px;",
    "  padding: 6px 9px; border-radius: 6px;",
    "  background: #24262b; color: #cfd3d7; border: 1px solid #33363c;",
    "  font-size: 12px; line-height: 1.4; font-style: normal;",
    "  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.45);",
    "  opacity: 0; visibility: hidden; pointer-events: none; transition: opacity 0.06s;",
    "}",
    ".ro-badge:hover .ro-tip { opacity: 1; visibility: visible; }",
  ].join("\n");

  var injected = false;
  function ensureCss() {
    if (injected) return;
    injected = true;
    var style = document.createElement("style");
    style.textContent = CSS;
    document.head.appendChild(style);
  }

  function update(el, message, tooltip) {
    ensureCss();
    el.classList.add("ro-badge");
    if (!message) {
      el.hidden = true;
      return;
    }
    el.hidden = false;
    el.innerHTML = "";
    el.appendChild(document.createTextNode(message));
    var tip = document.createElement("span");
    tip.className = "ro-tip";
    tip.textContent = tooltip || "";
    el.appendChild(tip);
  }

  window.fusedRoBadge = { update: update };
})();
