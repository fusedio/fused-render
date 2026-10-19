// General: appearance, windows, the launcher's shortcuts, updates.
import { useCallback, useEffect, useState } from "react";
import { getConfig, getPrefs, postLauncherSuspend, putAutoDownloadUpdates, putLauncherHotkey, putLauncherRowModifier, putNativeWindowsEnabled } from "@platform/lib/api";
import { THEME_PRESETS, useThemePref, useThemePreset } from "@platform/lib/theme";
import { updateLabel, updateRelevant, useUpdateStatus } from "@platform/lib/update-status";
import { isBot } from "@platform/lib/flavor";
import { NativeSelect, NativeSelectOption } from "@platform/shadcn/ui/native-select";
import { ToggleGroup, ToggleGroupItem } from "@platform/shadcn/ui/toggle-group";
import { Button } from "@platform/shadcn/ui/button";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { labelId, PrefToggleRow, SettingRow, usePrefWrite, type PrefControlProps } from "./SettingRow";

// Appearance is the one control here that is NOT server-backed: it is
// per-browser-profile localStorage by decision (SPEC §30 AP-1 / D134), so a
// browser tab and the desktop window can hold different choices. Writes are
// synchronous, hence no busy/error.
const MODES = [
  { value: "system", label: "System" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
] as const;

export function AppearanceControl() {
  const [pref, setPref] = useThemePref();
  return (
    <SettingRow
      id="appearance"
      control={
        <ToggleGroup
          aria-labelledby={labelId("appearance")}
          value={[pref]}
          // A press on the selected item would empty the group; there is no
          // "no appearance", so that press is ignored.
          onValueChange={(v) => v[0] && setPref(v[0] as (typeof MODES)[number]["value"])}
          variant="outline"
          size="sm"
          spacing={0}
        >
          {MODES.map((m) => (
            <ToggleGroupItem key={m.value} value={m.value} className="px-3">
              {m.label}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
      }
    />
  );
}

export function ThemeControl() {
  const [preset, setPreset] = useThemePreset();
  return (
    <SettingRow
      id="theme"
      control={
        <NativeSelect
          aria-labelledby={labelId("theme")}
          value={preset}
          onChange={(e) => setPreset(e.target.value as typeof preset)}
        >
          {THEME_PRESETS.map((p) => (
            <NativeSelectOption key={p.id} value={p.id}>
              {p.label}
            </NativeSelectOption>
          ))}
        </NativeSelect>
      }
    />
  );
}

// Native windows (macOS mac_window.py; Linux's WebKitGTK window host): on by
// default, this is the only place it turns off, and it applies live. Only
// drawn where the running app can honour it (`native_windows.available`).
export function NativeWindowsControl({ prefs, onChange }: PrefControlProps) {
  return (
    <PrefToggleRow
      id="native-windows"
      checked={!!prefs.native_windows?.enabled}
      put={putNativeWindowsEnabled}
      onChange={onChange}
    />
  );
}

// The macOS launcher (launcher_panel.py). The hotkey is RECORDED, not typed:
// click the keycap, press the combination, and `KeyboardEvent.code` becomes
// the spec — what maps to a Carbon keycode whatever the keyboard layout. The
// bind happens on the app's main thread a tick after the PUT, so the
// response's `bound` is the previous state; the row re-reads shortly after.
// Under Fused Bot the same two controls edit the `bot_launcher_*` prefs; the
// wire keys are the same either way and the server picks the flavor's keys.
const ROW_MODIFIERS: { spec: string; label: string; title: string }[] = [
  { spec: "alt", label: "⌥", title: "Option" },
  { spec: "cmd", label: "⌘", title: "Command" },
  { spec: "ctrl", label: "⌃", title: "Control" },
  { spec: "alt+cmd", label: "⌥⌘", title: "Option-Command" },
  { spec: "ctrl+alt", label: "⌃⌥", title: "Control-Option" },
  // Fused Bot's default (launcher.BOT_DEFAULT_ROW_MODIFIER): one modifier more
  // than Render's ⌥ so both apps' row shortcuts bind side by side. Canonical
  // spelling (hotkey.MODIFIER_ORDER: ctrl, alt, shift, cmd) or it never reads
  // as selected.
  { spec: "alt+shift", label: "⌥⇧", title: "Option-Shift" },
];
const MODIFIER_KEYS = new Set(["Shift", "Control", "Alt", "Meta", "CapsLock", "Fn"]);
const REBIND_REREAD_MS = 400;

function useRebindReread(onChange: PrefControlProps["onChange"]) {
  return useCallback(() => {
    window.setTimeout(() => {
      getPrefs().then(onChange).catch(() => undefined);
    }, REBIND_REREAD_MS);
  }, [onChange]);
}

export function LauncherHotkeyControl({ prefs, onChange }: PrefControlProps) {
  const launcher = prefs.launcher!;
  const [recording, setRecording] = useState(false);
  const [hint, setHint] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const reread = useRebindReread(onChange);

  useEffect(() => {
    if (!recording) return;
    // The live bindings are Carbon's, not the page's: with them up, the
    // combination being recorded would open the panel instead of arriving
    // here. Suspended for the recording, restored however it ends.
    postLauncherSuspend(true).catch(() => undefined);
    const onKey = (e: KeyboardEvent) => {
      e.preventDefault();
      e.stopPropagation();
      if (e.key === "Escape") {
        setRecording(false);
        setHint(null);
        return;
      }
      if (MODIFIER_KEYS.has(e.key)) return; // wait for the key itself
      const mods: string[] = [];
      if (e.ctrlKey) mods.push("ctrl");
      if (e.altKey) mods.push("alt");
      if (e.shiftKey) mods.push("shift");
      if (e.metaKey) mods.push("cmd");
      if (!mods.length) {
        setHint("Add ⌥ ⌘ ⌃ or ⇧…");
        return;
      }
      setRecording(false);
      setHint(null);
      setPending(true);
      setError(null);
      putLauncherHotkey(mods.concat([e.code]).join("+"))
        .then((next) => {
          onChange(next);
          reread();
        })
        .catch((err) => setError((err as Error).message))
        .finally(() => setPending(false));
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      postLauncherSuspend(false).catch(() => undefined);
    };
  }, [recording, onChange, reread]);

  const unbound = launcher.bound === false;
  return (
    <SettingRow
      id="launcher-hotkey"
      status={
        unbound ? (
          <span className="settings-warn">
            Could not bind {launcher.display} — another app may own it. Pick a different shortcut.
          </span>
        ) : undefined
      }
      control={
        <Button
          variant="outline"
          className={recording ? "settings-keycap settings-keycap-rec" : "settings-keycap"}
          aria-labelledby={labelId("launcher-hotkey")}
          disabled={pending}
          onClick={() => {
            setRecording(true);
            setHint("Press keys…");
          }}
        >
          {recording ? (hint ?? "Press keys…") : launcher.display || launcher.hotkey}
        </Button>
      }
    >
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </SettingRow>
  );
}

export function LauncherRowControl({ prefs, onChange }: PrefControlProps) {
  const launcher = prefs.launcher!;
  const { busy, error, run } = usePrefWrite(onChange);
  const reread = useRebindReread(onChange);
  const rowsUnbound = launcher.pinned_bound === false;
  return (
    <SettingRow
      id="launcher-row"
      status={
        rowsUnbound ? (
          <span className="settings-warn">
            Some of {launcher.row_modifier_display}1–9 could not be bound system-wide — another app may own them.
          </span>
        ) : isBot() ? (
          "Opens the Nth bot in the search's list — or, once you type, the Nth result."
        ) : (
          "Your recently opened apps, newest first, then the sidebar's Projects — or, once you type, the Nth result."
        )
      }
      control={
        <ToggleGroup
          aria-labelledby={labelId("launcher-row")}
          value={[launcher.row_modifier]}
          onValueChange={(v) => {
            const spec = v[0] as string | undefined;
            if (!spec || spec === launcher.row_modifier) return;
            void run(() => putLauncherRowModifier(spec), reread);
          }}
          variant="outline"
          size="sm"
          spacing={0}
          disabled={busy}
        >
          {ROW_MODIFIERS.map((m) => (
            <ToggleGroupItem key={m.spec} value={m.spec} title={m.title} aria-label={m.title} className="min-w-9 px-2">
              {m.label}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
      }
    >
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </SettingRow>
  );
}

// Updates: only the background-download switch. "Check now" lives on the
// status bar's Updates chip (shell/UpdatesDock.tsx), which runs the same
// useManualUpdateCheck and is mounted in both flavors — a second button here
// was the same question asked in two places. A found or installing update is
// UpdateNotifier's to announce; this row only says so in its status line.
export function AutoUpdateControl({ prefs, onChange }: PrefControlProps) {
  const status = useUpdateStatus();
  const [version, setVersion] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    void getConfig().then((c) => {
      if (!cancelled) setVersion(c.version);
    });
    return () => {
      cancelled = true;
    };
  }, []);
  // `status === null` is both "no updater on this build" and "the first
  // /api/config poll has not answered yet". The version fetch above doubles
  // as the "have we heard back at all" signal, so the not-managed line never
  // flashes on a packaged build while that first poll is in flight.
  const heard = status !== null || version !== null;
  const hasUpdater = status !== null;
  const running = version ? `Running v${version}.` : "";
  const line = !heard
    ? null
    : !hasUpdater
      ? `${running} Updates aren't managed from inside the app on this build.`.trim()
      : updateRelevant(status)
        ? `${running} ${updateLabel(status)}`.trim()
        : `${running} Check for a new version from the Updates chip in the status bar.`.trim();
  if (heard && !hasUpdater) return <SettingRow id="auto-update" status={line} />;
  return (
    <PrefToggleRow
      id="auto-update"
      // Default OFF: absence (an older server) reads as off.
      checked={prefs.update?.auto_download === true}
      put={putAutoDownloadUpdates}
      disabled={!hasUpdater}
      status={line}
      onChange={onChange}
    />
  );
}
