// The list's chat card — its two slots — drawn once in the shell with free
// pins on (rail-list-host-store header): mounted in App.tsx beside the
// favourite's card so a page hop never remounts a pinned list task.
import { useEffect } from "react";
import { registerListHost, useListCards } from "./rail-list-host-store";

export function ListCardHost() {
  useEffect(() => registerListHost(), []);
  const cards = useListCards();
  return (
    <>
      {cards.x}
      {cards.y}
    </>
  );
}
