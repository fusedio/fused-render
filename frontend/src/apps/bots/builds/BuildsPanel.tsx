// #bpanel (OpenBot index.html + builds.js "panel"): the shell's own Tasks UI in an iframe, filtered down to builds,
// shown while ui.panel === "builds". Also hosts #bdmodal (BuildDialog) and boots builds.ts (builds.json, the chip,
// the one shared tasks long poll). Back / Esc close it; builds keep running.
import { ArrowLeftIcon } from "lucide-react";
import { useEffect } from "react";
import { closePanel, setBuildsChip, useBotsSelector } from "../state/store";
import { BuildDialog } from "./BuildDialog";
import { applyBuildFilter, closeBuilds, isBuildDialogOpen, setBuildFrame, showBuildFrame, startBuilds, takeOpenedByCode, useBuildsRoot } from "./builds";

export function BuildsPanel() {
  const open = useBotsSelector((s) => s.ui.panel === "builds");
  const root = useBuildsRoot();
  useEffect(() => startBuilds(), []);
  // Opened from the footer chip (App → openPanel): clear its `fresh` mark and frame the list. openBuilds() (a new
  // build or task) already framed its task and leaves the mark alone, as OpenBot did.
  useEffect(() => {
    if (!open) return;
    if (!takeOpenedByCode()) { setBuildsChip({ fresh: false }); void showBuildFrame(); }
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !isBuildDialogOpen()) closeBuilds(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);
  return (
    <>
      <div id="bpanel" className={open ? "show" : ""}>
        <div className="topbar">
          <button id="bback" className="backtxt" aria-label="Back" title="Back to bots (Esc); builds keep running" onClick={closePanel}><ArrowLeftIcon /></button>
          <b className="ttl">Tasks</b><small className="muted">Claude tasks that create fused apps{root ? ` under ${root}` : ""}</small>
        </div>
        <iframe id="bframe" ref={setBuildFrame} title="Builds" onLoad={applyBuildFilter} />
      </div>
      <BuildDialog />
    </>
  );
}
