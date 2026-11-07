// DOES THIS SERVER OFFER BOTS, for a reader with no `config` prop — the
// Claude rail mounts from apps and the shell alike, and only the shell's top
// (App, GlobalSidebar) holds the config. One `/api/config` read per page,
// shared; until it answers the answer is NO, so a server without Bots (Windows)
// never draws the rail's Bots door or polls `/api/bots/pulse` for a frame.
import { useEffect, useState } from "react";
import { botsAvailable, getConfig } from "./api";

let known: boolean | null = null;
let reading: Promise<boolean> | null = null;

function read(): Promise<boolean> {
  if (!reading) {
    reading = getConfig()
      .then((c) => (known = botsAvailable(c)))
      .catch(() => {
        // A failed read is retried by the next mount, not cached as "no".
        reading = null;
        return false;
      });
  }
  return reading;
}

export function useBotsAvailable(): boolean {
  const [on, setOn] = useState<boolean>(known ?? false);
  useEffect(() => {
    if (known !== null) {
      setOn(known);
      return;
    }
    let live = true;
    void read().then((v) => {
      if (live) setOn(v);
    });
    return () => {
      live = false;
    };
  }, []);
  return on;
}

/** Tests: pin the answer (`null` forgets it, so the next mount reads again). */
export function setBotsAvailableForTests(v: boolean | null): void {
  known = v;
  reading = v === null ? null : Promise.resolve(v);
}
