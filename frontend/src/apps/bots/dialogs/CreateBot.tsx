// "+ New bot", after the preset chooser, on shadcn/ui: three things and a bot exists. Name, what it should do,
// model; the effort menu sits behind the cog. Everything else (approvals, builds, browser profile, encryption,
// trusted apps, the phone) has a first-run default that is right and lives in Settings once the bot exists. Enter
// in Name creates; Escape, the backdrop and Cancel dismiss (null). The Settings-sized shell (dialogs/shell.ts) in
// two columns: the avatar (its picker is a Popover on it) and the preset's playbooks on the left, the fields on the right.
import { useEffect, useMemo, useRef, useState } from "react";
import { Settings2Icon } from "lucide-react";
import { cn } from "@platform/lib/utils";
import { Button } from "@platform/shadcn/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@platform/shadcn/ui/dialog";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@platform/shadcn/ui/field";
import { Input } from "@platform/shadcn/ui/input";
import { NativeSelect, NativeSelectOption } from "@platform/shadcn/ui/native-select";
import { RadioGroup, RadioGroupItem } from "@platform/shadcn/ui/radio-group";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { Face } from "../components/Face";
import type { Face as FaceT } from "../lib/api";
import { browserOf, EFFORTS, loginGroups, loginHint, loginLabel, modelsFor } from "../lib/botform";
import { faceOf } from "../lib/face";
import { newBotInit, type NewBotPick } from "../lib/presets";
import { getState } from "../state/store";
import type { BotDialogValue } from "./actions";
import { FacePopover } from "./FacePopover";
import { DIALOG_CLASS, DIALOG_SIZE, FOOTER_CLASS, HEADER_CLASS } from "./shell";

export interface CreateBotProps {
  pick: NewBotPick;
  /** A value creates; null (Escape, the backdrop) closes the whole New-bot flow. */
  onClose: (v: BotDialogValue | null) => void;
  /** The Cancel button: back to the chooser (the pick was one click; the chooser is where a second thought goes). */
  onBack: () => void;
}

export function CreateBot({ pick, onClose, onBack }: CreateBotProps) {
  const isSuper = pick.kind === "super";
  const [fresh] = useState(() => newBotInit(pick));
  const [name, setName] = useState(fresh.name || `Bot ${getState().bots.length + 1}`);
  const [model, setModel] = useState(fresh.model || "sonnet");
  const [effort, setEffort] = useState("low");
  const [instructions, setInstructions] = useState(fresh.instructions || "");
  const [face, setFace] = useState<FaceT | null | undefined>(fresh.face);
  const [more, setMore] = useState(false);
  // Logins: a fresh browser or another browser's sign-ins, listed per browser. With a Super Bot around, the default is
  // its browser, so a new bot starts signed in wherever Super Bot is (Google, usually). Making Super Bot asks nothing.
  const [groups] = useState(() => (isSuper ? [] : loginGroups(getState().bots)));
  const [superBrowser] = useState(() => { const s = getState().bots.find((b) => b.kind === "super"); return s ? browserOf(s) : ""; });
  const [share, setShare] = useState(() => !!superBrowser && groups.some((g) => g.id === superBrowser));
  const [shareWith, setShareWith] = useState(() => (groups.some((g) => g.id === superBrowser) ? superBrowser : groups[0]?.id || ""));

  // The avatar subject: the face is hashed from the name the dialog opened with until one is picked.
  const bm = useMemo(() => ({ name: fresh.name, face }), [fresh.name, face]);
  // Focus by id, not ref (the shadcn Input forwards no ref on React 18), a tick after the dialog sets its own.
  useEffect(() => {
    const t = window.setTimeout(() => { const el = document.getElementById("bmname") as HTMLInputElement | null; el?.focus(); el?.select(); }, 0);
    return () => window.clearTimeout(t);
  }, []);

  const read = (): BotDialogValue => ({
    name: name.trim(), model, effort, instructions, memory: "", approval: "ask", buildAccess: "scoped", encrypt: false, profile: "",
    face: faceOf(bm), imessage: "", imessageEnabled: false, imessageTo: "", preset: fresh.preset || "",
    kind: isSuper ? "super" : "bot", superAccess: "ask", trustedApps: [], allowRulesRemoved: [],
    browserId: share && groups.some((g) => g.id === shareWith) ? shareWith : "",
  });
  const ok = () => { if (name.trim()) onClose(read()); };
  // The avatar popover is up: the press outside it that closes it must not also drop the whole New-bot flow (held
  // while open, released a tick after close; swatch presses count as inside already, see FacePopover.tsx).
  const busy = useRef(false);
  const holdFor = (open: boolean) => { if (open) busy.current = true; else window.setTimeout(() => { busy.current = false; }, 0); };

  return (
    <Dialog open modal={false} onOpenChange={(open) => { if (!open && !busy.current) onClose(null); }}>
      <DialogContent showCloseButton={false} className={cn(DIALOG_CLASS, DIALOG_SIZE)}>
        <DialogHeader className={HEADER_CLASS}>
          <DialogTitle>{fresh.title}</DialogTitle>
          <DialogDescription className="sr-only">Name it, say what it should do, pick a model.</DialogDescription>
        </DialogHeader>
        <div className="flex min-h-0 flex-1 gap-8 px-6 pb-5">
          <aside className="flex w-64 shrink-0 flex-col items-center gap-5 self-start">
            <FacePopover subject={bm} onPick={setFace} onOpenChange={holdFor} disabled={isSuper} className="w-fit flex-col gap-2" title={isSuper ? "Super Bot's avatar is fixed" : "Edit avatar"}>
              <span className="size-28 [&>svg]:block [&>svg]:size-full"><Face b={bm} /></span>
              {isSuper ? null : <span className="text-xs text-muted-foreground group-hover:text-foreground">Edit avatar</span>}
            </FacePopover>
            {fresh.skills.length ? (
              <div className="w-full text-sm">
                <div className="mb-1.5 font-medium">Comes with {fresh.skills.length} playbooks</div>
                <ul className="m-0 flex list-disc flex-col gap-0.5 pl-5 text-[13px] text-muted-foreground">
                  {fresh.skills.map((sk) => <li key={sk}>{sk}</li>)}
                </ul>
                <p className="mt-2 mb-0 text-[13px] text-muted-foreground">Edit them under Settings › Skills once the bot exists.</p>
              </div>
            ) : fresh.presetNote ? <p className="m-0 w-full text-[13px] text-muted-foreground">{fresh.presetNote}</p> : null}
          </aside>
          <FieldGroup className="min-h-0 min-w-0 flex-1 overflow-y-auto">
          <Field>
            <FieldLabel htmlFor="bmname">Name</FieldLabel>
            <Input id="bmname" placeholder="e.g. LinkedIn scout" value={name} onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") ok(); }} />
          </Field>
          {groups.length ? (
            <Field>
              <FieldLabel>Logins</FieldLabel>
              <RadioGroup value={share ? "share" : "fresh"} onValueChange={(v) => setShare(v === "share")} className="gap-3">
                <label className="flex cursor-pointer items-start gap-2.5">
                  <RadioGroupItem value="fresh" className="mt-0.5" />
                  <span className="flex flex-col gap-0.5">
                    <span className="text-sm">Fresh browser</span>
                    <span className="text-[13px] text-muted-foreground">Starts logged out of everything.</span>
                  </span>
                </label>
                <div className="flex flex-col gap-1.5">
                  <label className="flex cursor-pointer items-center gap-2.5">
                    <RadioGroupItem value="share" />
                    <span className="text-sm">Same logins as</span>
                  </label>
                  <div className="flex flex-col gap-1.5 pl-[26px]">
                    <NativeSelect className="w-full" id="bmshare" aria-label="Browser whose logins to share" value={shareWith}
                      onChange={(e) => { setShareWith(e.target.value); setShare(true); }} onFocus={() => setShare(true)}>
                      {groups.map((g) => <NativeSelectOption key={g.id} value={g.id}>{loginLabel(g)}</NativeSelectOption>)}
                    </NativeSelect>
                    <span className="text-[13px] text-muted-foreground">{loginHint(groups.find((g) => g.id === shareWith)) || "Shares that browser's sign-ins."} Log in once, every bot on it stays in.</span>
                  </div>
                </div>
              </RadioGroup>
            </Field>
          ) : null}
          <Field>
            <FieldLabel htmlFor="bminstr">What it should do</FieldLabel>
            <Textarea id="bminstr" rows={3} value={instructions} onChange={(e) => setInstructions(e.target.value)} className="max-h-56"
              placeholder={isSuper ? "Standing rules for every task. Blank keeps Super Bot's own: files in its Inbox folder, say before anything destructive."
                : "Standing rules for every task, e.g. “Browse LinkedIn for me. Answer in bullets. Never like or comment.”"} />
          </Field>
          <Field>
            <FieldLabel htmlFor="bmmodel">Model</FieldLabel>
            <NativeSelect className="w-full" id="bmmodel" value={model} onChange={(e) => setModel(e.target.value)}>
              {modelsFor(isSuper).map(([v, l]) => <NativeSelectOption key={v} value={v}>{l}</NativeSelectOption>)}
            </NativeSelect>
          </Field>
          {more ? (
            <Field>
              <FieldLabel htmlFor="bmeffort">Thinking</FieldLabel>
              <NativeSelect className="w-full" id="bmeffort" value={effort} onChange={(e) => setEffort(e.target.value)}>
                {EFFORTS.map(([v, l]) => <NativeSelectOption key={v} value={v}>{l}</NativeSelectOption>)}
              </NativeSelect>
              <FieldDescription>How much it thinks per step. Applies from the next task.</FieldDescription>
            </Field>
          ) : null}
          {isSuper ? null : <FieldDescription className="-mt-2">It asks you before anything it cannot undo; change that and more under Settings once it exists.</FieldDescription>}
          </FieldGroup>
        </div>
        <DialogFooter className={cn(FOOTER_CLASS, "sm:justify-between")}>
          <Button variant="outline" size="icon" aria-pressed={more} aria-label="Thinking effort" title={more ? "Hide the thinking setting" : "Thinking effort"}
            className={cn(more && "bg-foreground text-background hover:bg-foreground hover:text-background dark:bg-foreground dark:text-background dark:hover:bg-foreground")} onClick={() => setMore((m) => !m)}>
            <Settings2Icon />
          </Button>
          <div className="flex gap-2">
            <Button variant="outline" onClick={onBack} title="Back to the bot picker">Cancel</Button>
            <Button disabled={!name.trim()} onClick={ok}>{isSuper ? "Make Super Bot" : "Create bot"}</Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
