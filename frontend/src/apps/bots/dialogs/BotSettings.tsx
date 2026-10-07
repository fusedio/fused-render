// Settings (edit an existing bot), on shadcn/ui (owner's ask, 2026-10-07: "the entire settings modal black, use
// shadcn"): a Dialog with a rail of sections — General · Permissions · Browser · Memory, plus Phone on Super Bot
// (the one bot reachable over iMessage, docs §10) — and one setting per row (SettingsRow.tsx): its name and one line of why on the left, the control on the right.
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
import { Input } from "@platform/shadcn/ui/input";
import { NativeSelect, NativeSelectOption } from "@platform/shadcn/ui/native-select";
import { Switch } from "@platform/shadcn/ui/switch";
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
import { Row, Rows } from "./SettingsRow";

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
      <DialogContent showCloseButton={false} className={cn(DIALOG_CLASS, "flex h-[min(760px,90vh)] flex-col sm:max-w-[880px]")}>
        <DialogHeader className="gap-1 px-6 pt-5 pb-4">
          <DialogTitle>Settings · {n || init.name}</DialogTitle>
          <DialogDescription className="sr-only">How this bot thinks, what it may do without asking, its browser, its memory{isSuper ? " and your phone" : ""}.</DialogDescription>
        </DialogHeader>
        <div className="flex min-h-0 flex-1 gap-6 px-6 pb-5">
          <nav className="-ml-2.5 flex w-36 shrink-0 flex-col gap-0.5 self-start" aria-label="Settings sections">
            {tabs.map(([t, l]) => (
              <button key={t} type="button" aria-current={tab === t ? "page" : undefined} onClick={() => setTab(t)}
                className={cn("flex h-8 w-full cursor-pointer appearance-none items-center rounded-md border-0 bg-transparent px-2.5 text-sm outline-none transition-colors focus-visible:ring-3 focus-visible:ring-ring/50",
                  tab === t ? "bg-white/[0.08] font-medium text-foreground" : "text-muted-foreground hover:bg-white/[0.04] hover:text-foreground")}>
                {l}
                {t === "phone" && imessageEnabled && imessage ? <span className="ml-auto size-1.5 rounded-full bg-emerald-400" aria-label="connected" /> : null}
              </button>
            ))}
          </nav>
          <div className="min-h-0 min-w-0 flex-1 overflow-x-hidden overflow-y-auto" data-sec={tab}>
            {tab === "general" ? (
              <Rows>
                <Row title="Name" htmlFor="bmname">
                  <Input id="bmname" ref={nameRef} placeholder="e.g. LinkedIn scout" value={name} onChange={(e) => setName(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter") ok(); }} />
                </Row>
                {/* Super Bot's avatar is the fixed Claude mark: no picker (the backend refuses a change too). */}
                <Row title="Avatar" text={isSuper ? "Super Bot's mark is fixed." : "Shape and colour; the bot's face in the list and the chat."}>
                  <button type="button" disabled={isSuper} onClick={() => { void editAvatar(); }}
                    className="group flex cursor-pointer appearance-none items-center gap-2.5 rounded-lg border-0 bg-transparent p-0 outline-none focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-default"
                    title={isSuper ? "Super Bot's avatar is fixed" : "Edit avatar"}>
                    {isSuper ? null : <span className="text-[13px] text-muted-foreground group-hover:text-foreground">Change</span>}
                    <span className="size-9 [&>svg]:block [&>svg]:size-full"><Face b={bm} /></span>
                  </button>
                </Row>
                <Row title="What it should do" text="Standing rules for every task." htmlFor="bminstr" stack>
                  <Textarea id="bminstr" rows={3} value={instructions} onChange={(e) => setInstructions(e.target.value)} className="max-h-64"
                    placeholder={isSuper ? "Blank keeps Super Bot's own: files in its Inbox folder, say before anything destructive."
                      : "e.g. “Browse LinkedIn for me. Answer in bullets. Never like or comment.”"} />
                </Row>
                <Row title="Model" text="Applies from the next task." htmlFor="bmmodel">
                  <NativeSelect className="w-full" id="bmmodel" value={model} onChange={(e) => setModel(e.target.value)}>
                    {modelsFor(isSuper).map(([v, l]) => <NativeSelectOption key={v} value={v}>{l}</NativeSelectOption>)}
                  </NativeSelect>
                </Row>
                <Row title="Thinking" text="How much it thinks per step." htmlFor="bmeffort">
                  <NativeSelect className="w-full" id="bmeffort" value={effort} onChange={(e) => setEffort(e.target.value)}>
                    {EFFORTS.map(([v, l]) => <NativeSelectOption key={v} value={v}>{l}</NativeSelectOption>)}
                  </NativeSelect>
                </Row>
              </Rows>
            ) : null}

            {tab === "permissions" ? (
              <Rows>
                {isSuper ? (
                  <Row title="On this Mac" text="Files, shell and code. Reading never asks. Once a task has read the web, every write and command asks either way." htmlFor="bmsuper">
                    <NativeSelect className="w-full" id="bmsuper" value={superAccess} onChange={(e) => setSuperAccess(e.target.value)}>
                      <NativeSelectOption value="ask">Ask me first</NativeSelectOption>
                      <NativeSelectOption value="full">Unattended · Claude judges</NativeSelectOption>
                    </NativeSelect>
                  </Row>
                ) : null}
                <Row title={isSuper ? "Before it sends, buys, deletes or posts" : "Before it buys, deletes or posts"}
                  text="“Never ask” lets it finish on its own. A step the bot itself flags as irreversible still shows a card." htmlFor="bmapproval">
                  <NativeSelect className="w-full" id="bmapproval" value={approval} onChange={(e) => setApproval(e.target.value)}>
                    <NativeSelectOption value="ask">Ask me first</NativeSelectOption>
                    <NativeSelectOption value="auto">Never ask</NativeSelectOption>
                  </NativeSelect>
                </Row>
                {!isSuper ? (
                  <Row title="When it builds an app with Claude Code" text="Builds land under ~/Fused/app and park under Builds while a question waits." htmlFor="bmbuild">
                    <NativeSelect className="w-full" id="bmbuild" value={buildAccess} onChange={(e) => setBuildAccess(e.target.value)}>
                      <NativeSelectOption value="scoped">Ask before risky steps</NativeSelectOption>
                      <NativeSelectOption value="full">Run unattended</NativeSelectOption>
                    </NativeSelect>
                  </Row>
                ) : null}
                {appRows.length ? (
                  <Row title="Apps it may use without asking" text="A ticked app's tools and scripts run at once. Everything else keeps the rule above." stack>
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
                  </Row>
                ) : null}
              </Rows>
            ) : null}

            {tab === "browser" ? (
              <Rows>
                <Row title="Start from a Chrome profile" text="Copies that profile's logins, cookies and extensions into the bot's browser, replacing what it has. Your own Chrome is not touched." htmlFor="bmprofile">
                  <NativeSelect className="w-full" id="bmprofile" value={profile} onChange={(e) => setProfile(e.target.value)}>
                    <NativeSelectOption value="">Keep this bot's own{bot.chrome_profile ? ` (from ${bot.chrome_profile})` : ""}</NativeSelectOption>
                    {profiles.map((p) => <NativeSelectOption key={p.dir} value={p.dir}>{p.name}{p.email ? ` · ${p.email}` : ""}</NativeSelectOption>)}
                  </NativeSelect>
                </Row>
                <Row title="Encrypt the browser profile at rest" text="While the browser is closed the profile is one AES-256 file; the key lives in your macOS Keychain. Lose the Keychain item and saved logins are gone." htmlFor="bmencrypt">
                  <Switch id="bmencrypt" checked={encrypt} onCheckedChange={(c) => setEncrypt(!!c)} />
                </Row>
              </Rows>
            ) : null}

            {tab === "memory" ? (
              <Rows>
                <Row title="Notes it keeps between tasks" text="The bot writes here itself as it learns site quirks and your preferences; edit or clear what it got wrong." htmlFor="bmmem" stack>
                  <Textarea id="bmmem" rows={8} value={memory} onChange={(e) => setMemory(e.target.value)} className="max-h-80"
                    placeholder="Empty so far." />
                  <div><Button type="button" variant="outline" size="sm" disabled={!memory} onClick={() => setMemory("")}>Clear</Button></div>
                </Row>
              </Rows>
            ) : null}

            {tab === "phone" && isSuper ? (
              <PhoneSection botId={bot.id} handle={imessage} setHandle={setImessage} enabled={imessageEnabled} setEnabled={setImessageEnabled}
                contacts={imessageTo} setContacts={setImessageTo} />
            ) : null}
          </div>
        </div>
        <DialogFooter className={FOOTER_CLASS}>
          <Button variant="outline" onClick={() => onClose(null)}>Cancel</Button>
          <Button disabled={okDisabled} onClick={ok}>Save</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
