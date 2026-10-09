// The New app dialog (OpenBot index.html + builds.js buildDialog), and with an app the "New task · <app>" variant that
// asks for a change to that existing app (name and folder fixed). buildDialog(app?) in builds.ts opens it and resolves
// with the fields (or null). On the bots dialog shell (dialogs/shell.ts), content-height. Esc / backdrop / Cancel
// close; ⌘↩ or Ctrl+Enter starts when allowed.
import { useEffect, useRef, useState } from "react";
import { cn } from "@platform/lib/utils";
import { Button } from "@platform/shadcn/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@platform/shadcn/ui/dialog";
import { Field, FieldGroup, FieldLabel } from "@platform/shadcn/ui/field";
import { Input } from "@platform/shadcn/ui/input";
import { NativeSelect, NativeSelectOption } from "@platform/shadcn/ui/native-select";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { DIALOG_CLASS, FOOTER_CLASS, HEADER_CLASS } from "../dialogs/shell";
import { slugOf, useBuildDialog, useBuildsRoot, type BuildDialogReq, type BuildDialogResult } from "./builds";

function Box({ req }: { req: BuildDialogReq }) {
  const app = req.app, root = useBuildsRoot();
  const [name, setName] = useState(app ? app.name || app.folder || "" : "");
  const [prompt, setPrompt] = useState("");
  const [model, setModel] = useState("opus");
  const [effort, setEffort] = useState("high");
  const [mode, setMode] = useState("default");
  const disabled = (!app && !name.trim()) || !prompt.trim();
  const read = (): BuildDialogResult => ({ name: name.trim(), prompt, model, effort, permissionMode: mode });
  const where = app ? app.dir : `${root}/${name.trim() ? slugOf(name.trim()) : "…"}`;

  // Latest values for the capture-phase key handler.
  const live = useRef({ disabled, read });
  live.current = { disabled, read };
  useEffect(() => {
    // By id, a tick after the dialog's own initial focus (the shadcn Input forwards no ref on React 18).
    const t = window.setTimeout(() => document.getElementById(app ? "bdprompt" : "bdname")?.focus(), 0);
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); req.resolve(null); }
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !live.current.disabled) { e.stopPropagation(); req.resolve(live.current.read()); }
    };
    document.addEventListener("keydown", key, true);
    return () => { window.clearTimeout(t); document.removeEventListener("keydown", key, true); };
  }, [req, app]);

  return (
    <>
      <DialogHeader className={HEADER_CLASS}>
        <DialogTitle id="bdtitle">{app ? `New task · ${app.name || app.folder || "App"}` : "New app"}</DialogTitle>
        <DialogDescription>Folder: <code id="bddir" className="break-all text-foreground">{where}</code></DialogDescription>
      </DialogHeader>
      <FieldGroup className="min-h-0 overflow-y-auto px-6 pb-5">
        {app ? null : (
          <Field id="bdnamefield">
            <FieldLabel htmlFor="bdname">App name</FieldLabel>
            <Input id="bdname" placeholder="e.g. Invoice tracker" value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
        )}
        <Field>
          <FieldLabel htmlFor="bdprompt"><span id="bdask">{app ? "What should change?" : "What should the app do?"}</span></FieldLabel>
          <Textarea id="bdprompt" rows={6} value={prompt} onChange={(e) => setPrompt(e.target.value)}
            placeholder={app ? "Describe the change: what to add, fix or remove. Claude runs inside the app's folder with the fused-render app contract." : "Describe the app: what it shows, what data it reads, what the user can do. Claude gets the fused-render app contract on top of this."} />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field>
            <FieldLabel htmlFor="bdmodel">Model</FieldLabel>
            <NativeSelect className="w-full" id="bdmodel" value={model} onChange={(e) => setModel(e.target.value)}>
              <NativeSelectOption value="sonnet">Sonnet · balanced</NativeSelectOption>
              <NativeSelectOption value="opus">Opus · strongest</NativeSelectOption>
              <NativeSelectOption value="fable">Fable · most capable</NativeSelectOption>
            </NativeSelect>
          </Field>
          <Field>
            <FieldLabel htmlFor="bdeffort">Effort</FieldLabel>
            <NativeSelect className="w-full" id="bdeffort" value={effort} onChange={(e) => setEffort(e.target.value)}>
              <NativeSelectOption value="medium">Medium</NativeSelectOption>
              <NativeSelectOption value="high">High · careful</NativeSelectOption>
              <NativeSelectOption value="xhigh">Extra high · slowest</NativeSelectOption>
            </NativeSelect>
          </Field>
        </div>
        <Field>
          <FieldLabel htmlFor="bdmode">Approvals</FieldLabel>
          <NativeSelect className="w-full" id="bdmode" value={mode} onChange={(e) => setMode(e.target.value)}>
            <NativeSelectOption value="default">Ask before risky tools (answer on the Tasks page)</NativeSelectOption>
            <NativeSelectOption value="auto">Never ask · unattended</NativeSelectOption>
            <NativeSelectOption value="plan">Plan only · no edits</NativeSelectOption>
          </NativeSelect>
        </Field>
      </FieldGroup>
      <DialogFooter className={FOOTER_CLASS}>
        <Button id="bdcancel" variant="outline" onClick={() => req.resolve(null)}>Cancel</Button>
        <Button id="bdok" title="⌘↩ / Ctrl+Enter" disabled={disabled} onClick={() => { if (!disabled) req.resolve(read()); }}>{app ? "Start task" : "Create app"}</Button>
      </DialogFooter>
    </>
  );
}

export function BuildDialog() {
  const req = useBuildDialog();
  return (
    <Dialog open={!!req} modal={false} onOpenChange={(open) => { if (!open && req) req.resolve(null); }}>
      <DialogContent id="bdmodal" showCloseButton={false} className={cn(DIALOG_CLASS, "flex max-h-[90vh] flex-col sm:max-w-[520px]")}>
        {/* Keyed by request: every open starts from OpenBot's defaults (opus · high · default). */}
        {req ? <Box key={req.seq} req={req} /> : null}
      </DialogContent>
    </Dialog>
  );
}
