// A specialist's one form (docs/specialists.md), for "+ New bot" › Specialist and for its Settings…: the face,
// the name, what it is an expert on, and an optional folder it helps in by itself. The New bot form's shell and
// layout (CreateBot.tsx). Save closes only when the server took it; a refusal (a name taken) shows in the banner.
import { useMemo, useRef, useState } from "react";
import { cn } from "@platform/lib/utils";
import type { Specialist } from "@platform/lib/specialists";
import { Button } from "@platform/shadcn/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@platform/shadcn/ui/dialog";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@platform/shadcn/ui/field";
import { Input } from "@platform/shadcn/ui/input";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { Face } from "../components/Face";
import { specialistApi, type Face as FaceT } from "../lib/api";
import { faceOf } from "../lib/face";
import { actSpecialist, select } from "../state/store";
import { FacePopover } from "./FacePopover";
import { DIALOG_CLASS, DIALOG_SIZE, FOOTER_CLASS, HEADER_CLASS } from "./shell";

export interface SpecialistDialogProps {
  /** Absent: a new one. */
  s?: Specialist;
  onClose: () => void;
  /** Cancel on a new one: back to the New bot chooser. */
  onBack?: () => void;
}

export function SpecialistDialog({ s, onClose, onBack }: SpecialistDialogProps) {
  const [name, setName] = useState(s?.name || "");
  const [about, setAbout] = useState(s?.about || "");
  const [folder, setFolder] = useState(s?.folder || "");
  const [face, setFace] = useState<FaceT | null>((s?.face as FaceT | null) ?? null);
  const subject = useMemo(() => ({ id: s?.id, name: name || "Specialist", face }), [s?.id, name, face]);
  const ok = !!name.trim() && !!about.trim();
  const save = async () => {
    if (!ok) return;
    const body = { name: name.trim(), about: about.trim(), folder: folder.trim(), face: faceOf(subject) };
    const r = await actSpecialist(() => (s ? specialistApi.patch(s.id, body) : specialistApi.create(body)));
    if (!r) return;
    onClose();
    if (!s) select(r.id);
  };
  // The avatar popover's outside press must not also close the dialog (CreateBot.tsx `busy`).
  const busy = useRef(false);
  const holdFor = (open: boolean) => { if (open) busy.current = true; else window.setTimeout(() => { busy.current = false; }, 0); };

  return (
    <Dialog open modal={false} onOpenChange={(open) => { if (!open && !busy.current) onClose(); }}>
      <DialogContent showCloseButton={false} className={cn(DIALOG_CLASS, DIALOG_SIZE)}>
        <DialogHeader className={HEADER_CLASS}>
          <DialogTitle>{s ? `${s.name} settings` : "New specialist"}</DialogTitle>
          <DialogDescription>A specialist knows one subject, remembers what you teach it in its chat, and helps your tasks with it.</DialogDescription>
        </DialogHeader>
        <div className="flex min-h-0 flex-1 gap-8 px-6 pb-5">
          <aside className="flex w-64 shrink-0 flex-col items-center gap-5 self-start">
            <FacePopover subject={subject} onPick={setFace} onOpenChange={holdFor} className="w-fit flex-col gap-2" title="Edit avatar">
              <span className="size-28 [&>svg]:block [&>svg]:size-full"><Face b={subject} /></span>
              <span className="text-xs text-muted-foreground group-hover:text-foreground">Edit avatar</span>
            </FacePopover>
          </aside>
          <FieldGroup className="min-h-0 min-w-0 flex-1 overflow-y-auto">
            <Field>
              <FieldLabel htmlFor="spname">Name</FieldLabel>
              <Input id="spname" autoFocus placeholder="e.g. Canvas" value={name} onChange={(e) => setName(e.target.value)} />
            </Field>
            <Field>
              <FieldLabel htmlFor="spabout">What it is an expert on</FieldLabel>
              <Textarea id="spabout" rows={3} className="max-h-56" value={about} onChange={(e) => setAbout(e.target.value)}
                placeholder="e.g. Building canvases: layout, the UDFs they call, and how we name things." />
            </Field>
            <Field>
              <FieldLabel htmlFor="spfolder">Folder (optional)</FieldLabel>
              <Input id="spfolder" placeholder="/path/to/project" value={folder} onChange={(e) => setFolder(e.target.value)} />
              <FieldDescription>A task working in this folder gets its guidance by itself.</FieldDescription>
            </Field>
          </FieldGroup>
        </div>
        <DialogFooter className={FOOTER_CLASS}>
          <Button variant="outline" onClick={onBack || onClose}>Cancel</Button>
          <Button disabled={!ok} onClick={() => void save()}>{s ? "Save" : "Create specialist"}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
