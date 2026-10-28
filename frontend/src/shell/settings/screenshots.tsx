// CANVAS SCREENSHOTS (Settings › Chats & tasks › Canvas screenshots), macOS.
// Two rows: the screen permission the native still needs — live status, one
// button to the pane — and the switch for the slow in-canvas fallback, which
// on a Mac is off until turned on here (apps/canvases/canvas-shot-permission.ts
// has the rule; a failed screenshot's refusal line points at both rows).
import { useEffect, useState } from "react";

import { openScreenRecordingSettings } from "@platform/lib/api";
import { captureSources } from "@platform/lib/capture-audio";
import { bundleName } from "@platform/lib/flavor";
import { openScreenRecordingPane, type ScreenshotSource } from "@platform/lib/screen-permission";
import { Button } from "@platform/shadcn/ui/button";
import { setSlowFallbackEnabled, slowFallbackEnabled } from "@apps/canvases/canvas-shot-permission";

import { RowSwitch, SettingRow } from "./SettingRow";

export function CanvasScreenshotsControl() {
  // undefined: checking; null: the server has no screenshot source at all;
  // "unknown": the check itself failed (a transient error is not "not
  // available on this machine").
  const [source, setSource] = useState<ScreenshotSource | null | "unknown" | undefined>(undefined);
  const [opened, setOpened] = useState(false);
  useEffect(() => {
    let alive = true;
    captureSources()
      .then((s) => {
        if (alive) setSource(s.screenshot ?? null);
      })
      .catch(() => {
        if (alive) setSource("unknown");
      });
    return () => {
      alive = false;
    };
  }, []);
  const app = bundleName();
  const checked = source !== undefined && source !== "unknown" ? source : null;
  const granted = checked?.granted === true;
  const unavailable = source === null || checked?.available === false;
  const open = () => {
    setOpened(true);
    void openScreenRecordingPane(openScreenRecordingSettings);
  };
  return (
    <SettingRow
      id="canvas-screenshots"
      status={
        source === undefined ? (
          "Checking…"
        ) : source === "unknown" ? (
          "Could not check — is the app running?"
        ) : unavailable ? (
          "Not available on this machine."
        ) : granted ? (
          <span className="settings-dot settings-dot-ok">Allowed — canvas screenshots take under a second.</span>
        ) : opened ? (
          <>Turn on <b>{app}</b> in the pane, then relaunch {app}.</>
        ) : (
          <>Not allowed yet — canvas screenshots fail until it is (or turn on the fallback below).</>
        )
      }
      control={
        source === undefined || source === "unknown" || unavailable ? undefined : (
          <Button variant={granted ? "outline" : "default"} size="sm" onClick={open}>
            {granted ? "Open System Settings" : opened ? "Open System Settings again" : "Allow in System Settings"}
          </Button>
        )
      }
    />
  );
}

/** Local to this machine (localStorage), read by the canvas page on every
 *  capture — so no publish, and no server round trip. */
export function CanvasScreenshotsFallbackControl() {
  const [on, setOn] = useState(() => slowFallbackEnabled());
  const flip = (next: boolean) => {
    setSlowFallbackEnabled(next);
    setOn(next);
  };
  return (
    <SettingRow
      id="canvas-screenshots-fallback"
      status={on ? "On — a failed native screenshot is redrawn by the workbench, slowly." : "Off — a failed native screenshot says why instead."}
      control={<RowSwitch id="canvas-screenshots-fallback" checked={on} onChange={flip} />}
    />
  );
}
