// The list's chat card — its slots — drawn once in the shell with free pins
// on (rail-list-host-store header): mounted in App.tsx beside the
// favourite's card so a page hop never remounts a pinned list task.
//
// WHAT IT MUST NOT DRAW (CodeRabbit on #2349), since a rail that is gone left
// its pinned slots' cards here as it last built them, and nobody rebuilds
// them:
//   - anything at all with the switch off — the rail draws its own card then,
//     and a card kept from free-pin mode would stand beside it;
//   - a pinned card whose pin is gone, on a page with no rail: its × drops
//     its word from section A's pins (`aPins`), and with no rail to build the
//     slot anew the card stood until the next rail or a reload. Each card
//     answers for its own pin (many pins, 2026-11-24).
import { useEffect } from "react";
import { useAPins } from "@platform/lib/last-chat";
import { useRailExtras } from "./rail-extras-flag";
import { registerListHost, useListCards, useListOrphaned } from "./rail-list-host-store";

export function ListCardHost() {
  useEffect(() => registerListHost(), []);
  const cards = useListCards();
  const freePins = useRailExtras();
  const orphaned = useListOrphaned();
  const words = useAPins();
  if (!freePins) return null;
  return <>{cards.filter((c) => !orphaned || (c.pin !== null && words.includes("task:" + c.pin))).map((c) => c.node)}</>;
}
