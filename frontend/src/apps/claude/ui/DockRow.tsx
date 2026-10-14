// ONE ROW above the composer: the Agents & shells bar (grows, truncates) and
// the Files pill (fixed, right). Either may be absent; neither renders nothing.
import type { ReactNode } from "react";

export function DockRow({ tasks, files }: { tasks: ReactNode; files: ReactNode }) {
  if (!tasks && !files) return null;
  return (
    <div className="c-dock-row">
      {tasks}
      {files}
    </div>
  );
}
