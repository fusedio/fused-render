// The list's chat card — its two slots — drawn once in the shell with free
// pins on (rail-list-host-store header): mounted in App.tsx beside the
// favourite's card so a page hop never remounts a pinned list task.
//
// WHAT IT MUST NOT DRAW (CodeRabbit on #2349), since a rail that is gone left
// its pinned slot's card here as it last built it, and nobody rebuilds it:
//   - anything at all with the switch off — the rail draws its own card then,
//     and a card kept from free-pin mode would stand beside it;
//   - a pinned card whose pin is gone, on a page with no rail: its × clears
//     window A's mark (`aPinned`), and with no rail to build the slot empty
//     the card stood until the next rail or a reload.
import { useEffect } from "react";
import { useAPinned } from "@platform/lib/last-chat";
import { useRailExtras } from "./rail-extras-flag";
import { registerListHost, useListCards, useListOrphaned } from "./rail-list-host-store";
import { aPinTask } from "./rail-slots-lib";

export function ListCardHost() {
  useEffect(() => registerListHost(), []);
  const cards = useListCards();
  const freePins = useRailExtras();
  const orphaned = useListOrphaned();
  const pinned = aPinTask(useAPinned()) !== null;
  if (!freePins || (orphaned && !pinned)) return null;
  return (
    <>
      {cards.x}
      {cards.y}
    </>
  );
}
