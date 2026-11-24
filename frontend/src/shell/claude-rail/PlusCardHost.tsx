// The +'s New task card, drawn once in the shell (plus-card-store header):
// mounted in App.tsx beside TasksPeek so a page hop never remounts it.
import { useEffect } from "react";
import { plusHostElement, registerPlusHost, usePlusHostVersion } from "./plus-card-store";

export function PlusCardHost() {
  useEffect(() => registerPlusHost(), []);
  usePlusHostVersion();
  return <>{plusHostElement()}</>;
}
