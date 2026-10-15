// ONE ROW above the composer: the Agents & shells bar (grows, truncates).
// Absent tasks render nothing.
import type { ReactNode } from "react";

export function DockRow({ tasks }: { tasks: ReactNode }) {
  if (!tasks) return null;
  return <div className="c-dock-row">{tasks}</div>;
}
