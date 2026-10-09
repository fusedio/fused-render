// The in-app confirm (OpenBot dialogs.js askConfirm): renders the head of the confirm queue (dialogs/ask.ts) on the
// shadcn shell, content-height, above every page dialog (z-[60]; it is also mounted after the dialog slot). Enter = OK,
// Escape / Cancel / an outside press = no. The key listener runs in the capture phase and stops propagation so nothing
// underneath (the bot dialog's Enter, the live view's Esc) also reacts. Cancel takes focus on open (the first field for
// the auth / prompt variants). Non-modal like every dialog here: the page dialog under it holds its `busy` ref instead.
import { useEffect } from "react";
import { cn } from "@platform/lib/utils";
import { Button } from "@platform/shadcn/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@platform/shadcn/ui/dialog";
import { Input } from "@platform/shadcn/ui/input";
import { settleConfirm, useConfirm } from "./ask";
import { DIALOG_CLASS, FOOTER_CLASS, HEADER_CLASS } from "./shell";

export function Confirm() {
  const c = useConfirm();
  const byId = (id: string) => document.getElementById(id) as HTMLInputElement | null;  // by id: the shadcn Button and Input forward no ref on React 18
  const creds = () => ({ user: byId("cmuser")?.value ?? "", pass: byId("cmpass")?.value ?? "", text: byId("cmtext-in")?.value ?? "" });
  useEffect(() => {
    if (!c) return;
    // A tick later than the dialog's own initial focus, so this lands after it.
    const t = window.setTimeout(() => {
      if (c.fields === "prompt") { const el = byId("cmtext-in"); if (el) { el.value = c.defaultValue ?? ""; el.focus(); el.select(); } }
      else if (c.fields === "auth") byId("cmuser")?.focus();
      else byId("cmcancel")?.focus();
    }, 0);
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); settleConfirm(c.id, false); }
      else if (e.key === "Enter") { e.stopPropagation(); e.preventDefault(); settleConfirm(c.id, true, creds()); }
    };
    document.addEventListener("keydown", key, true);
    return () => { window.clearTimeout(t); document.removeEventListener("keydown", key, true); };
  }, [c]);
  return (
    <Dialog open={!!c} modal={false} onOpenChange={(open) => { if (!open && c) settleConfirm(c.id, false); }}>
      <DialogContent showCloseButton={false} id="cmodal" role="alertdialog" className={cn(DIALOG_CLASS, "z-[60] flex flex-col sm:max-w-[420px]")}>
        <DialogHeader className={HEADER_CLASS}>
          <DialogTitle id="cmtitle">{c?.title ?? "Delete?"}</DialogTitle>
          {/* !: .bots-page [data-slot=dialog-description] in bots.css mutes every description; a confirm's text is the point. */}
          <DialogDescription id="cmtext" className="!text-foreground/90">{c?.text ?? ""}</DialogDescription>
        </DialogHeader>
        {c?.fields === "prompt" ? (
          <div className="flex flex-col gap-2 px-6 pb-5">
            <Input id="cmtext-in" type="text" aria-label="Your answer" autoComplete="off" autoCapitalize="off" spellCheck={false} />
          </div>
        ) : null}
        {c?.fields === "auth" ? (
          <div className="flex flex-col gap-2 px-6 pb-5">
            <Input id="cmuser" type="text" placeholder="User name" autoComplete="username" autoCapitalize="off" spellCheck={false} />
            <Input id="cmpass" type="password" placeholder="Password" autoComplete="current-password" />
          </div>
        ) : null}
        <DialogFooter className={FOOTER_CLASS}>
          <Button id="cmcancel" variant="outline" onClick={() => c && settleConfirm(c.id, false)}>Cancel</Button>
          <Button id="cmok" variant={c && !c.danger ? "default" : "destructive"} onClick={() => c && settleConfirm(c.id, true, creds())}>{c?.okLabel ?? "Delete"}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
