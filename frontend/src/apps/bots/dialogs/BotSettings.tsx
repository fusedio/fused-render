// Settings (edit an existing bot), on shadcn/ui (owner's ask, 2026-10-07: "the entire settings modal black, use
// shadcn"): a Dialog with a vertical Tabs rail — General · Permissions · Browser · Memory, plus Phone on Super Bot
// (the one bot reachable over iMessage, docs §10) — and shadcn fields with one line of why under each control.
// Every control keeps its backend key. The form is a snapshot of the bot as the dialog opened (later polls never
// reset what you typed); Save stays greyed until something differs from that snapshot. Escape and the backdrop
// close at once when nothing changed and ask first otherwise; Enter in Name is Save.
//
// Base UI only coordinates dialogs through the React tree: the page's own confirm and face picker are siblings in
// the bots portal host, so a click in either reads as an OUTSIDE press here and would fire onOpenChange(false). The
// `busy` ref covers the time one of them is up, and the dialog is non-modal so its focus trap never fights theirs.
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Button } from "@platform/shadcn/ui/button";
import { Checkbox } from "@platform/shadcn/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@platform/shadcn/ui/dialog";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@platform/shadcn/ui/field";
import { Input } from "@platform/shadcn/ui/input";
import { NativeSelect, NativeSelectOption } from "@platform/shadcn/ui/native-select";
import { Tabs, TabsList, TabsTrigger } from "@platform/shadcn/ui/tabs";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { cn } from "@platform/lib/utils";
import { Face } from "../components/Face";
import { api, type AppRow, type Bot, type ChromeProfile, type Face as FaceT } from "../lib/api";
import { EFFORTS, modelsFor, normApp } from "../lib/botform";
import { faceOf } from "../lib/face";
import { act } from "../state/store";
import type { BotDialogValue } from "./actions";
import { askConfirm, pickFace } from "./ask";
import { PhoneSection } from "./PhoneSection";

export type SettingsTab = "general" | "permissions" | "browser" | "memory" | "phone";

/** The dialog's look, shared with CreateBot: black, stock shadcn everything else. */
export const DIALOG_CLASS = "gap-0 overflow-hidden p-0 bg-neutral-950 text-neutral-50 ring-white/10";
export const FOOTER_CLASS = "mx-0 mb-0 rounded-b-xl border-t border-white/10 bg-white/[0.03] px-6 py-4";

export interface BotSettingsProps {
  bot: Bot;
  /** The section to open on (Preferences' Phone row and the seeded greeting ask for "phone"). */
  tab?: string;
  onClose: (v: BotDialogValue | null) => void;
}

export function BotSettings({ bot, tab: tab0, onClose }: BotSettingsProps) {
  const isSuper = bot.kind === "super";
  const tabs: [SettingsTab, string][] = [["general", "General"], ["permissions", "Permissions"], ["browser", "Browser"], ["memory", "Memory"],
    ...(isSuper ? [["phone", "Phone"] as [SettingsTab, string]] : [])];
  const [tab, setTab] = useState<SettingsTab>(() => (tabs.some(([t]) => t === tab0) ? (tab0 as SettingsTab) : "general"));
  // What the dialog opened with (OpenBot's botDialog arguments).
  const [init] = useState(() => ({
    name: bot.name, model: bot.model || "sonnet", effort: bot.effort || "low", instructions: bot.instructions || "", memory: bot.memory || "",
    approval: bot.approval || "ask", buildAccess: bot.build_access || "scoped", encrypt: !!bot.encrypt,
    imessage: bot.imessage || "", imessageEnabled: !!bot.imessage_enabled, imessageTo: bot.imessage_to || "",
    superAccess: bot.super_access || "ask", trustedApps: bot.trusted_apps || [],
  }));
  const [name, setName] = useState(init.name);
  const [model, setModel] = useState(init.model);
  const [effort, setEffort] = useState(init.effort);
  const [instructions, setInstructions] = useState(init.instructions);
  const [memory, setMemory] = useState(init.memory);
  const [approval, setApproval] = useState<string>(init.approval);
  const [buildAccess, setBuildAccess] = useState<string>(init.buildAccess);
  const [superAccess, setSuperAccess] = useState<string>(init.superAccess);
  const [encrypt, setEncrypt] = useState(init.encrypt);
  const [imessage, setImessage] = useState(init.imessage);
  const [imessageEnabled, setImessageEnabled] = useState(init.imessageEnabled);
  const [imessageTo, setImessageTo] = useState(init.imessageTo);
  const [profile, setProfile] = useState("");
  const [profiles, setProfiles] = useState<ChromeProfile[]>([]);
  const [trustedApps, setTrustedApps] = useState<string[]>(init.trustedApps);
  const [appRows, setAppRows] = useState<AppRow[]>([]);
  const [face, setFace] = useState<FaceT | null | undefined>(bot.face);
  const busy = useRef(false);  // a sibling modal (confirm, face picker) is up: outside presses are theirs

  const bm = useMemo(() => ({ id: bot.id, name: init.name, face }), [bot.id, init.name, face]);
  const read = (): BotDialogValue => ({ name: name.trim(), model, effort, instructions, memory, approval, buildAccess, encrypt, profile,
    face: faceOf(bm), imessage: imessage.trim(), imessageEnabled, imessageTo: imessageTo.trim(), preset: "",
    kind: isSuper ? "super" : "bot", superAccess, trustedApps });  // the face shown is the face kept
  const [initial] = useState(() => JSON.stringify(read()));
  const okDisabled = JSON.stringify(read()) === initial;
  const n = name.trim();

  // Your Chrome's profiles and the apps root, listed fresh each time.
  useEffect(() => {
    let live = true;
    void act(() => api.profiles(), true).then((r) => { if (live) setProfiles(r?.profiles || []); });
    void act(() => api.apps(), true).then((r) => { if (live) setAppRows(r?.apps || []); });
    return () => { live = false; };
  }, []);
  const toggleTrusted = (folder: string, on: boolean) => {
    const k = normApp(folder);
    setTrustedApps((cur) => on ? (cur.includes(k) ? cur : [...cur, k]) : cur.filter((x) => x !== k));
  };

  const nameRef = useRef<HTMLInputElement>(null);
  useLayoutEffect(() => { if (tab === "general") { nameRef.current?.focus(); nameRef.current?.select(); } }, [tab]);

  const ok = () => { if (!okDisabled) onClose(read()); };
  const editAvatar = async () => {
    busy.current = true;
    try { setFace(await pickFace(bm, (d) => setFace(d))); } finally { busy.current = false; }
  };
  // Escape / backdrop: at once when clean, after a confirm when dirty (Discard, or Cancel to keep editing).
  const tryClose = async () => {
    if (busy.current) return;
    if (okDisabled) { onClose(null); return; }
    busy.current = true;
    try { if (await askConfirm("Discard changes?", "You have unsaved changes to this bot's settings.", "Discard")) onClose(null); }
    finally { busy.current = false; }
  };

  return (
    <Dialog open modal={false} onOpenChange={(open) => { if (!open) void tryClose(); }}>
      <DialogContent showCloseButton={false} className={cn(DIALOG_CLASS, "flex h-[min(640px,90vh)] flex-col sm:max-w-[720px]")}>
        <DialogHeader className="gap-1 px-6 pt-5 pb-4">
          <DialogTitle>Settings · {n || init.name}</DialogTitle>
          <DialogDescription className="sr-only">How this bot thinks, what it may do without asking, its browser, its memory{isSuper ? " and your phone" : ""}.</DialogDescription>
        </DialogHeader>
        <Tabs value={tab} onValueChange={(v) => setTab(v as SettingsTab)} orientation="vertical" className="min-h-0 flex-1 gap-0 px-6 pb-5">
          <TabsList variant="line" className="w-36 shrink-0 gap-0.5 self-start p-0 pr-3">
            {tabs.map(([t, l]) => (
              <TabsTrigger key={t} value={t} className="h-8 w-full justify-start px-2.5">
                {l}
                {t === "phone" && imessageEnabled && imessage ? <span className="ml-auto size-1.5 rounded-full bg-emerald-400" aria-label="connected" /> : null}
              </TabsTrigger>
            ))}
          </TabsList>
          <div className="min-h-0 flex-1 overflow-y-auto border-l border-white/10 pl-6" data-sec={tab}>
            {tab === "general" ? (
              <FieldGroup>
                {/* Super Bot's avatar is the fixed Claude mark: no picker (the backend refuses a change too). */}
                <button type="button" disabled={isSuper} onClick={() => { void editAvatar(); }}
                  className="group flex w-fit flex-col items-center gap-1.5 self-start rounded-lg outline-none focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-default"
                  title={isSuper ? "Super Bot's avatar is fixed" : "Edit avatar"}>
                  <span className="size-12 [&>svg]:block [&>svg]:size-full"><Face b={bm} /></span>
                  {isSuper ? null : <span className="text-xs text-muted-foreground group-hover:text-foreground">Edit avatar</span>}
                </button>
                <Field>
                  <FieldLabel htmlFor="bmname">Name</FieldLabel>
                  <Input id="bmname" ref={nameRef} placeholder="e.g. LinkedIn scout" value={name} onChange={(e) => setName(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter") ok(); }} />
                </Field>
                <Field>
                  <FieldLabel htmlFor="bminstr">What it should do</FieldLabel>
                  <Textarea id="bminstr" rows={3} value={instructions} onChange={(e) => setInstructions(e.target.value)} className="max-h-64"
                    placeholder={isSuper ? "Standing rules for every task. Blank keeps Super Bot's own: files in its Inbox folder, say before anything destructive."
                      : "Standing rules for every task, e.g. “Browse LinkedIn for me. Answer in bullets. Never like or comment.”"} />
                </Field>
                <div className="grid grid-cols-2 gap-4">
                  <Field>
                    <FieldLabel htmlFor="bmmodel">Model</FieldLabel>
                    <NativeSelect className="w-full" id="bmmodel" value={model} onChange={(e) => setModel(e.target.value)}>
                      {modelsFor(isSuper).map(([v, l]) => <NativeSelectOption key={v} value={v}>{l}</NativeSelectOption>)}
                    </NativeSelect>
                  </Field>
                  <Field>
                    <FieldLabel htmlFor="bmeffort">Thinking</FieldLabel>
                    <NativeSelect className="w-full" id="bmeffort" value={effort} onChange={(e) => setEffort(e.target.value)}>
                      {EFFORTS.map(([v, l]) => <NativeSelectOption key={v} value={v}>{l}</NativeSelectOption>)}
                    </NativeSelect>
                  </Field>
                </div>
                <FieldDescription className="-mt-3">Model and thinking apply from the next task.</FieldDescription>
              </FieldGroup>
            ) : null}

            {tab === "permissions" ? (
              <FieldGroup>
                {isSuper ? (
                  <Field>
                    <FieldLabel htmlFor="bmsuper">On this Mac</FieldLabel>
                    <NativeSelect className="w-full" id="bmsuper" value={superAccess} onChange={(e) => setSuperAccess(e.target.value)}>
                      <NativeSelectOption value="ask">Ask before writes, edits and shell commands</NativeSelectOption>
                      <NativeSelectOption value="full">Unattended · Claude judges, asks about the rest</NativeSelectOption>
                    </NativeSelect>
                    <FieldDescription>Files, shell and code. Reading never asks. Once a task has read the web, every write and command asks either way.</FieldDescription>
                  </Field>
                ) : null}
                <Field>
                  <FieldLabel htmlFor="bmapproval">{isSuper ? "Before it sends, buys, deletes or posts" : "Before it buys, deletes or posts"}</FieldLabel>
                  <NativeSelect className="w-full" id="bmapproval" value={approval} onChange={(e) => setApproval(e.target.value)}>
                    <NativeSelectOption value="ask">Ask me first</NativeSelectOption>
                    <NativeSelectOption value="auto">Never ask</NativeSelectOption>
                  </NativeSelect>
                  <FieldDescription>“Never ask” lets it finish on its own. A step the bot itself flags as irreversible still shows a card.</FieldDescription>
                </Field>
                {!isSuper ? (
                  <Field>
                    <FieldLabel htmlFor="bmbuild">When it builds an app with Claude Code</FieldLabel>
                    <NativeSelect className="w-full" id="bmbuild" value={buildAccess} onChange={(e) => setBuildAccess(e.target.value)}>
                      <NativeSelectOption value="scoped">Ask before risky steps</NativeSelectOption>
                      <NativeSelectOption value="full">Run unattended</NativeSelectOption>
                    </NativeSelect>
                    <FieldDescription>Builds land under ~/Fused/app and park under Builds while a question waits.</FieldDescription>
                  </Field>
                ) : null}
                {appRows.length ? (
                  <Field>
                    <FieldLabel>Apps it may use without asking</FieldLabel>
                    <div className="max-h-48 divide-y divide-white/5 overflow-y-auto rounded-lg border border-input" id="bmtrusted">
                      {appRows.map((a) => {
                        const on = trustedApps.includes(normApp(a.folder));
                        return (
                          <label key={a.folder} className="flex cursor-pointer items-center gap-2.5 px-3 py-2 text-sm hover:bg-white/[0.04]">
                            <Checkbox checked={on} onCheckedChange={(c) => toggleTrusted(a.folder, !!c)} />
                            <span>{a.name || a.folder}</span>
                            {a.name && a.name !== a.folder ? <span className="ml-auto text-xs text-muted-foreground">{a.folder}</span> : null}
                          </label>
                        );
                      })}
                    </div>
                    <FieldDescription>A ticked app's tools and scripts run at once. Everything else keeps the rule above.</FieldDescription>
                  </Field>
                ) : null}
              </FieldGroup>
            ) : null}

            {tab === "browser" ? (
              <FieldGroup>
                <Field>
                  <FieldLabel htmlFor="bmprofile">Start from a Chrome profile</FieldLabel>
                  <NativeSelect className="w-full" id="bmprofile" value={profile} onChange={(e) => setProfile(e.target.value)}>
                    <NativeSelectOption value="">Keep this bot's own profile{bot.chrome_profile ? ` (copied from ${bot.chrome_profile})` : ""}</NativeSelectOption>
                    {profiles.map((p) => <NativeSelectOption key={p.dir} value={p.dir}>{p.name}{p.email ? ` · ${p.email}` : ""}</NativeSelectOption>)}
                  </NativeSelect>
                  <FieldDescription>Copies that profile's logins, cookies and extensions into the bot's browser, replacing what it has. Your own Chrome is not touched.</FieldDescription>
                </Field>
                <Field orientation="horizontal">
                  <Checkbox id="bmencrypt" checked={encrypt} onCheckedChange={(c) => setEncrypt(!!c)} />
                  <div className="flex flex-col gap-0.5">
                    <FieldLabel htmlFor="bmencrypt">Encrypt the browser profile at rest</FieldLabel>
                    <FieldDescription>While the browser is closed the profile is one AES-256 file; the key lives in your macOS Keychain. Lose the Keychain item and saved logins are gone.</FieldDescription>
                  </div>
                </Field>
              </FieldGroup>
            ) : null}

            {tab === "memory" ? (
              <FieldGroup>
                <Field>
                  <FieldLabel htmlFor="bmmem">Notes it keeps between tasks</FieldLabel>
                  <Textarea id="bmmem" rows={8} value={memory} onChange={(e) => setMemory(e.target.value)} className="max-h-80"
                    placeholder="Empty. The bot adds site quirks and your preferences here as it learns." />
                  <FieldDescription>The bot writes here itself; edit or clear what it got wrong.</FieldDescription>
                </Field>
                <div><Button type="button" variant="outline" size="sm" disabled={!memory} onClick={() => setMemory("")}>Clear</Button></div>
              </FieldGroup>
            ) : null}

            {tab === "phone" && isSuper ? (
              <PhoneSection botId={bot.id} handle={imessage} setHandle={setImessage} enabled={imessageEnabled} setEnabled={setImessageEnabled}
                contacts={imessageTo} setContacts={setImessageTo} />
            ) : null}
          </div>
        </Tabs>
        <DialogFooter className={FOOTER_CLASS}>
          <Button variant="outline" onClick={() => onClose(null)}>Cancel</Button>
          <Button disabled={okDisabled} onClick={ok}>Save</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
