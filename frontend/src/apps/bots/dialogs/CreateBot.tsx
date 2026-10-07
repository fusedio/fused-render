// "+ New bot", after the preset chooser, on shadcn/ui: three things and a bot exists. Name, what it should do,
// model; the effort menu sits behind the cog. Everything else (approvals, builds, browser profile, encryption,
// trusted apps, the phone) has a first-run default that is right and lives in Settings once the bot exists. Enter
// in Name creates; Escape, the backdrop and Cancel dismiss (null). Same black dialog as BotSettings.
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
import { browserOf, EFFORTS, loginGroups, loginLabel, modelsFor } from "../lib/botform";
import { faceOf } from "../lib/face";
import { newBotInit, type NewBotPick } from "../lib/presets";
import { getState } from "../state/store";
import type { BotDialogValue } from "./actions";
import { pickFace } from "./ask";
import { DIALOG_CLASS, FOOTER_CLASS } from "./BotSettings";

export interface CreateBotProps {
  pick: NewBotPick;
  onClose: (v: BotDialogValue | null) => void;
}

export function CreateBot({ pick, onClose }: CreateBotProps) {
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
  const busy = useRef(false);  // the face picker (a sibling modal) is up: its clicks are not outside presses to act on

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
    kind: isSuper ? "super" : "bot", superAccess: "ask", trustedApps: [],
    browserId: share && groups.some((g) => g.id === shareWith) ? shareWith : "",
  });
  const ok = () => { if (name.trim()) onClose(read()); };
  const editAvatar = async () => {
    busy.current = true;
    try { setFace(await pickFace(bm, (d) => setFace(d))); } finally { busy.current = false; }
  };

  return (
    <Dialog open modal={false} onOpenChange={(open) => { if (!open && !busy.current) onClose(null); }}>
      <DialogContent showCloseButton={false} className={cn(DIALOG_CLASS, "sm:max-w-[480px]")}>
        <DialogHeader className="gap-1 px-6 pt-5 pb-4">
          <DialogTitle>{fresh.title}</DialogTitle>
          <DialogDescription className="sr-only">Name it, say what it should do, pick a model.</DialogDescription>
        </DialogHeader>
        <FieldGroup className="px-6 pb-5">
          <button type="button" disabled={isSuper} onClick={() => { void editAvatar(); }}
            className="group flex w-fit cursor-pointer appearance-none flex-col items-center gap-1.5 self-start rounded-lg border-0 bg-transparent p-0 outline-none focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-default"
            title={isSuper ? "Super Bot's avatar is fixed" : "Edit avatar"}>
            <span className="size-12 [&>svg]:block [&>svg]:size-full"><Face b={bm} /></span>
            {isSuper ? null : <span className="text-xs text-muted-foreground group-hover:text-foreground">Edit avatar</span>}
          </button>
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
                    <span className="text-[13px] text-muted-foreground">Shares that browser's sign-ins. Log in once, every bot on it stays in.</span>
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
          <FieldDescription className="-mt-2">
            {fresh.presetNote ? fresh.presetNote + " " : ""}
            {isSuper ? "" : "It asks you before anything it cannot undo; change that and more under Settings once it exists."}
          </FieldDescription>
        </FieldGroup>
        <DialogFooter className={cn(FOOTER_CLASS, "sm:justify-between")}>
          <Button variant="ghost" size="icon" aria-pressed={more} aria-label="More options" title="Thinking effort" onClick={() => setMore((m) => !m)}>
            <Settings2Icon />
          </Button>
          <div className="flex gap-2">
            <Button variant="outline" onClick={() => onClose(null)}>Cancel</Button>
            <Button disabled={!name.trim()} onClick={ok}>{isSuper ? "Make Super Bot" : "Create bot"}</Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
