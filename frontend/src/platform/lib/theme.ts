// Appearance — System / Light / Dark (SPEC §30, D134).
//
// One preference, one resolved theme:
//   pref  "system" | "light" | "dark"  — what the user chose (persisted)
//   theme "light" | "dark"             — what is actually painted
//
// The resolved theme lives in ONE place: `data-theme` on the shell document's
// <html>. `shell.css` keys its light palette off it, so a repaint is a single
// attribute write — no re-render, and therefore nothing that could touch a
// live iframe (the standing rule in Panel.tsx / PaneModeMenu.tsx). View
// documents are NOT reached from here: their injected runtime (static/
// runtime.js) resolves the same key itself, so a theme change never re-mounts
// or reloads an iframe.
//
// Persistence is the best-effort localStorage pattern of viewstate.ts /
// sidebarstate.ts: silent on failure, never blocking. A browser profile and
// the desktop app window legitimately hold different choices — this is
// per-browser-profile state, not a server-side preference (there is no
// /api/settings and deliberately none coming).
import { useEffect, useState } from "react";

// Must stay in sync with the pre-paint bootstrap in `frontend/index.html` and
// with `fused_render/static/runtime.js`. Those two can't import this module —
// one is inline in the HTML shell, the other ships into a different document —
// so a test (tests/test_theme.py) pins the three spellings together.
export const THEME_KEY = "fused-render:theme";

const DARK_QUERY = "(prefers-color-scheme: dark)";

// Cross-component "theme changed" signal: the menu that writes the
// preference and any other component reading it are mounted at once, and a
// localStorage write raises no event in its OWN window.
const THEME_EVENT = "fused:themechange";

export type ThemePref = "system" | "light" | "dark";
export type Theme = "light" | "dark";

export const THEME_PREFS: readonly ThemePref[] = ["system", "light", "dark"];

export const THEME_PREF_LABELS: Record<ThemePref, string> = {
  system: "System",
  light: "Light",
  dark: "Dark",
};

function isPref(value: unknown): value is ThemePref {
  return value === "system" || value === "light" || value === "dark";
}

export function loadThemePref(): ThemePref {
  try {
    const raw = localStorage.getItem(THEME_KEY);
    return isPref(raw) ? raw : "system";
  } catch {
    return "system"; // private-mode / quota — behave as the default
  }
}

function saveThemePref(pref: ThemePref): void {
  try {
    localStorage.setItem(THEME_KEY, pref);
  } catch {
    // storage unavailable — the choice is best-effort, so a failed write is
    // fine: this session still honors it, the next one starts from System.
  }
}

// The OS/browser preference. Defaults to dark when matchMedia is unavailable,
// so a browser that can't answer keeps today's appearance.
export function systemTheme(): Theme {
  try {
    return window.matchMedia(DARK_QUERY).matches ? "dark" : "light";
  } catch {
    return "dark";
  }
}

export function resolveTheme(pref: ThemePref): Theme {
  return pref === "system" ? systemTheme() : pref;
}

export function applyTheme(theme: Theme): void {
  document.documentElement.setAttribute("data-theme", theme);
}

// Write the choice, repaint this window, and tell every listener. Other
// same-origin windows (a second tab, each `/embed` pane shell, every view
// document) converge on the localStorage `storage` event instead — see
// `useResolvedTheme` below and runtime.js.
export function setThemePref(pref: ThemePref): void {
  saveThemePref(pref);
  applyTheme(resolveTheme(pref));
  window.dispatchEvent(new Event(THEME_EVENT));
}

// Subscribe to everything that can change the resolved theme:
//   * this window's own setThemePref (THEME_EVENT),
//   * another window's setThemePref (`storage`),
//   * the OS flipping while the preference is System — including macOS's
//     automatic sunset switch (matchMedia change).
// Returns an unsubscribe. The callback receives the live preference; callers
// resolve it themselves so a System-mode OS flip is observable too.
function subscribeThemePref(onChange: () => void): () => void {
  const onStorage = (event: StorageEvent) => {
    // A `storage` clear() reports key === null; treat it as "may have changed".
    if (event.key === null || event.key === THEME_KEY) onChange();
  };
  window.addEventListener(THEME_EVENT, onChange);
  window.addEventListener("storage", onStorage);
  let media: MediaQueryList | null = null;
  try {
    media = window.matchMedia(DARK_QUERY);
    media.addEventListener("change", onChange);
  } catch {
    media = null; // no matchMedia — pinned Light/Dark still works
  }
  return () => {
    window.removeEventListener(THEME_EVENT, onChange);
    window.removeEventListener("storage", onStorage);
    media?.removeEventListener("change", onChange);
  };
}

// The live preference plus a setter, for the settings menu.
export function useThemePref(): [ThemePref, (pref: ThemePref) => void] {
  const [pref, setPref] = useState<ThemePref>(loadThemePref);
  useEffect(() => subscribeThemePref(() => setPref(loadThemePref())), []);
  return [pref, setThemePref];
}

// Keep `data-theme` on <html> in step with the preference for the lifetime of
// the app. The FIRST application already happened in index.html's inline
// bootstrap (before paint) — this only handles later changes, so mounting it
// can never cause a flash.
export function useThemeSync(): void {
  useEffect(
    () =>
      subscribeThemePref(() => {
        applyTheme(resolveTheme(loadThemePref()));
      }),
    []
  );
}
