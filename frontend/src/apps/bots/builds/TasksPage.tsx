// /bots/tasks (routes.ts; OpenBot's #bpanel): the shell's own Tasks UI in an iframe, filtered down to builds. Mounted
// only while the route is active, so every visit is a fresh frame whose src is THIS component's state, derived from
// the route (`?task=<key>` frames one task's peek, else the list) — never a module-level cache (builds.ts explains the
// blank-page bug that cache produced). Back / Esc go to the selected bot's chat; builds keep running.
import { ArrowLeftIcon } from "lucide-react";
import { useEffect, useState } from "react";
import { setBuildsChip } from "../state/store";
import { applyBuildFilter, closeBuilds, isBuildDialogOpen, setFilterFrame, takeOpenedByCode, tasksPageUrl, useBuildsRoot } from "./builds";

export function TasksPage({ task }: { task: string | null }) {
  const root = useBuildsRoot();
  const [src, setSrc] = useState("");
  // Opened from the footer chip: clear its `fresh` mark. openBuilds() (a new build or task) leaves the mark alone, as OpenBot did.
  useEffect(() => { if (!takeOpenedByCode()) setBuildsChip({ fresh: false }); }, []);
  useEffect(() => {
    let live = true;
    tasksPageUrl(task).then((u) => { if (live) setSrc(u); }).catch((e) => console.warn("builds ui", e));
    return () => { live = false; };
  }, [task]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !isBuildDialogOpen()) closeBuilds(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);
  return (
    <div id="bpanel" className="show">
      <div className="topbar">
        <button id="bback" className="backtxt" aria-label="Back" title="Back to bots (Esc); builds keep running" onClick={closeBuilds}><ArrowLeftIcon /></button>
        <b className="ttl">Tasks</b><small className="muted">Claude tasks that create fused apps{root ? ` under ${root}` : ""}</small>
      </div>
      {/* `src` is set once the Tasks UI URL is known; an iframe with no src paints nothing rather than about:blank's flash. */}
      <iframe id="bframe" ref={setFilterFrame} title="Builds" src={src || undefined} onLoad={() => applyBuildFilter()} />
    </div>
  );
}
