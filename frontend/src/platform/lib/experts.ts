// Experts (docs/experts.md): the shared row shape and the list, in
// platform because the Bots page and the task chat both read them and neither
// may import the other.
import { useEffect, useState } from "react";
import { getJson } from "./api";
import { useExpertsEnabled } from "./experts-flag";

export interface Expert {
  id: string;
  name: string;
  about: string;
  folder: string;
  face: Record<string, unknown> | null;
  home: string;
  working: boolean;
  /** When it was made (epoch seconds; 0 unknown): its place in the Bots list until it has a chat. */
  created: number;
  last: { text: string; at: number } | null;
  /** A learning run in progress, and when the last one ended (epoch seconds). */
  updating: boolean;
  last_update: number | null;
  /** A learning run ended since its Learning tab was last opened. */
  new: boolean;
  /** Pinned to the top of the Bots list. */
  pinned?: boolean;
}

export const listExperts = () => getJson<Expert[]>("/api/experts");

/** The experts, read once per mount while the flag is on; empty when off. */
export function useExperts(): Expert[] {
  const on = useExpertsEnabled();
  const [rows, setRows] = useState<Expert[]>([]);
  useEffect(() => {
    if (!on) return setRows([]);
    let live = true;
    listExperts().then((r) => live && setRows(r), () => {});
    return () => {
      live = false;
    };
  }, [on]);
  return rows;
}
