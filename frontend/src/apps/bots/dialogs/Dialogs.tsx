// The modal host: routes the store's ui.dialog request (newBot → preset chooser → create form, settings, usage,
// browsers) to its dialog, and always mounts the confirm above them (an imperative promise queue, dialogs/ask.ts; it
// is mounted after the slot so it paints on top). Each request mounts a fresh dialog, so forms start clean exactly as
// OpenBot reset them per open. Every dialog wears the one shell (dialogs/shell.ts).
//
// For the bot menu / chat header: openDialog({kind: "settings", id, tab?}) or the helpers re-exported below
// (exportBot, deleteBot, cloneBot) and askConfirm. `tab` names a Settings section: "phone" from Preferences' Phone row
// and the seeded greeting, "skills" / "routines" from the bot menu and the preview pane (their dialogs were folded
// into Settings, 2026-10-09).
import { useEffect, useRef } from "react";
import { closeDialog, getState, openDialog, poll, select, useBotsSelector, type DialogReq } from "../state/store";
import { createBot, saveSettings } from "./actions";
import { BotSettings } from "./BotSettings";
import { BrowsersDialog } from "./Browsers";
import { Confirm } from "./Confirm";
import { CreateBot } from "./CreateBot";
import { PresetPicker } from "./PresetPicker";
import { UsageDialog } from "./Usage";

export { cloneBot, createBot, deleteBot, exportBot, saveSettings } from "./actions";
export { askConfirm } from "./ask";

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
  // Settings reads the bot's detail (memory, skills), which only the selected bot carries: select it if needed and
  // hold the dialog for the next poll, so a Save can never write back an unloaded (empty) memory.
  const needDetail = !!b && req?.kind === "settings" && (b.memory == null || b.skills == null);
  useEffect(() => {
    if (!needDetail || !b) return;
    if (getState().sel !== b.id) select(b.id);
    void poll();
  }, [needDetail, b?.id]);

  let dialog = null;
  // "+ New bot": the preset chooser first; a pick reopens the slot as the create form filled in from it. In the form,
  // Cancel steps BACK to the chooser (the pick was one click; the chooser is where a second thought goes), while
  // Escape and a press outside leave the whole flow. The chooser's X / outside press close.
  if (req?.kind === "newBot" && !req.pick) dialog = <PresetPicker key={key} onDone={(p) => { if (p) openDialog({ kind: "newBot", pick: p }); else closeDialog(); }} />;
  else if (req?.kind === "newBot" && req.pick) {
    dialog = <CreateBot key={key} pick={req.pick} onBack={() => openDialog({ kind: "newBot" })}
      onClose={(v) => { closeDialog(); if (v) void createBot(v); }} />;
  }
  else if (b && !needDetail && req?.kind === "settings") {
    const bid = b.id, tab = typeof req.tab === "string" ? req.tab : undefined;
    dialog = <BotSettings key={key} bot={b} tab={tab} onClose={(v) => { closeDialog(); if (v) void saveSettings(bid, v); }} />;
  }
  if (req?.kind === "usage") dialog = <UsageDialog key={key} onClose={closeDialog} />;
  if (req?.kind === "browsers") dialog = <BrowsersDialog key={key} onClose={closeDialog} />;
  return (
    <>
      {dialog}
      <Confirm />
    </>
  );
}
