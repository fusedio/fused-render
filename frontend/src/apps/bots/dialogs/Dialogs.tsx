// The modal host: routes the store's ui.dialog request (newBot → preset chooser → bot dialog, settings, routines,
// skills, usage) to its dialog,
// and always mounts the two imperative layers above them (the face picker and the confirm). Each request mounts a
// fresh dialog, so forms start clean (Advanced collapsed, add forms empty) exactly as OpenBot reset them per open.
//
// For the bot menu / chat header: openDialog({kind: "settings" | "routines" | "skills", id}) or the helpers re-exported
// below (exportBot, deleteBot, cloneBot), askConfirm, pickFace.
import { useEffect, useRef } from "react";
import { closeDialog, getState, openDialog, poll, select, useBotsSelector, type DialogReq } from "../state/store";
import { createBot, saveSettings } from "./actions";
import { BotDialog } from "./BotDialog";
import { Confirm } from "./Confirm";
import { FacePicker } from "./FacePicker";
import { PresetPicker } from "./PresetPicker";
import { RoutinesDialog } from "./Routines";
import { SkillsDialog } from "./Skills";
import { UsageDialog } from "./Usage";

export { cloneBot, createBot, deleteBot, exportBot, saveSettings } from "./actions";
export { askConfirm, pickFace } from "./ask";

/** A counter that moves whenever a new request object arrives (the dialog's React key). */
function useReqKey(req: DialogReq | null): number {
  const last = useRef<{ req: DialogReq | null; n: number }>({ req: null, n: 0 });
  if (req !== last.current.req) last.current = { req, n: last.current.n + 1 };
  return last.current.n;
}

export function Dialogs() {
  const req = useBotsSelector((s) => s.ui.dialog);
  const id = useBotsSelector((s) => s.ui.dialog?.id ?? s.sel);
  const b = useBotsSelector((s) => (id ? s.bots.find((x) => x.id === id) : undefined));
  const key = useReqKey(req);
  // Settings and Skills read the bot's detail (memory, skills), which only the selected bot carries: select it if needed
  // and hold the dialog for the next poll, so a Save can never write back an unloaded (empty) memory.
  const needDetail = !!b && ((req?.kind === "settings" && b.memory == null) || (req?.kind === "skills" && b.skills == null));
  useEffect(() => {
    if (!needDetail || !b) return;
    if (getState().sel !== b.id) select(b.id);
    void poll();
  }, [needDetail, b?.id]);

  let dialog = null;
  // "+ New bot": the preset chooser first; a pick reopens the slot as the bot dialog filled in from it.
  if (req?.kind === "newBot" && !req.pick) dialog = <PresetPicker key={key} onDone={(p) => { if (p) openDialog({ kind: "newBot", pick: p }); else closeDialog(); }} />;
  else if (req?.kind === "newBot" && req.pick) dialog = <BotDialog key={key} pick={req.pick} onClose={(v) => { closeDialog(); if (v) void createBot(v); }} />;
  else if (b && !needDetail) {
    if (req?.kind === "settings") { const bid = b.id; dialog = <BotDialog key={key} bot={b} onClose={(v) => { closeDialog(); if (v) void saveSettings(bid, v); }} />; }
    else if (req?.kind === "routines") dialog = <RoutinesDialog key={key} b={b} onClose={closeDialog} />;
    else if (req?.kind === "skills") dialog = <SkillsDialog key={key} b={b} onClose={closeDialog} />;
  }
  if (req?.kind === "usage") dialog = <UsageDialog key={key} onClose={closeDialog} />;
  return (
    <>
      {dialog}
      <FacePicker />
      <Confirm />
    </>
  );
}
