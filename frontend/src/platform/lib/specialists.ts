// Specialists (docs/specialists-v2.md): the shared row shape and the list, in
// platform because the Bots page and the task chat both read them and neither
// may import the other.
import { useEffect, useState } from "react";
import { getJson } from "./api";
import { useSpecialistsEnabled } from "./specialists-flag";

export interface Specialist {
  id: string;
  name: string;
  about: string;
  folder: string;
  face: Record<string, unknown> | null;
  home: string;
  working: boolean;
  last: { text: string; at: number } | null;
  /** A learning run in progress, and when the last one ended (epoch seconds). */
  updating: boolean;
  last_update: number | null;
  /** A learning run ended since its Learning tab was last opened. */
  new: boolean;
}

export const listSpecialists = () => getJson<Specialist[]>("/api/specialists");

/** The specialists, read once per mount while the flag is on; empty when off. */
export function useSpecialists(): Specialist[] {
  const on = useSpecialistsEnabled();
  const [rows, setRows] = useState<Specialist[]>([]);
  useEffect(() => {
    if (!on) return setRows([]);
    let live = true;
    listSpecialists().then((r) => live && setRows(r), () => {});
    return () => {
      live = false;
    };
  }, [on]);
  return rows;
}
