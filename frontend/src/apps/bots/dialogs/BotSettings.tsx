// Settings (edit an existing bot), on shadcn/ui (owner's ask, 2026-10-07: "the entire settings modal black, use
// shadcn"): a Dialog with a rail of sections — General · Permissions · Browser · Skills · Routines · Memory, plus
// Phone on Super Bot (the one bot reachable over iMessage, docs §10) — and one setting per
// row (SettingsRow.tsx): its name and one line of why on the left, the control on the right. Skills and Routines
// (once their own dialogs behind the ☰ menu, folded in 2026-10-09) are lists whose every op posts at once, like the
// page-wide switches on Browser; they never touch the snapshot or Save.
// Every control keeps its backend key. The form is a snapshot of the bot as the dialog opened (later polls never
// reset what you typed); Save stays greyed until something differs from that snapshot. Escape and the backdrop
// close at once when nothing changed and ask first otherwise; Enter in Name is Save.
//
// Base UI only coordinates dialogs through the React tree: the page's own confirm is a sibling in the bots portal
// host, so a click in it reads as an OUTSIDE press here and would fire onOpenChange(false). The `busy` ref covers the
// time it is up (`confirm` below is what the sections call), and the dialog is non-modal so its focus trap never
// fights the confirm's. The avatar picker is a Popover in this React tree, so its swatch presses count as inside; the
// press outside it that closes it is held off the same way (`holdFor`).
import { useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@platform/shadcn/ui/button";
import { Checkbox } from "@platform/shadcn/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@platform/shadcn/ui/dialog";
import { Input } from "@platform/shadcn/ui/input";
import { NativeSelect, NativeSelectOption } from "@platform/shadcn/ui/native-select";
import { Switch } from "@platform/shadcn/ui/switch";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { cn } from "@platform/lib/utils";
import { Face } from "../components/Face";
import { FolderPlusIcon, Trash2Icon } from "lucide-react";
import { ApiError, api, request, type AllowRule, type AppRow, type Bot, type ChromeProfile, type Face as FaceT } from "../lib/api";
import { EFFORTS, loginGroups, loginHint, loginLabel, loginValue, modelsFor, normApp } from "../lib/botform";
import { faceOf } from "../lib/face";
import { act, autoStage, clearContextOn, getState, setAutoStage, setClearContext, setSounds, soundsOn } from "../state/store";
import type { BotDialogValue } from "./actions";
import { askConfirm } from "./ask";
import { FacePopover } from "./FacePopover";
import { PhoneSection } from "./PhoneSection";
import { RoutinesSection } from "./RoutinesSection";
import { Row, Rows } from "./SettingsRow";
import { DIALOG_CLASS, DIALOG_SIZE, FOOTER_CLASS, HEADER_CLASS } from "./shell";
import { SkillsSection } from "./SkillsSection";

export type SettingsTab = "general" | "permissions" | "browser" | "skills" | "routines" | "memory" | "phone";

export interface BotSettingsProps {
  bot: Bot;
  /** The section to open on (the seeded greeting asks for "phone"). */
  tab?: string;
  onClose: (v: BotDialogValue | null) => void;
}

export function BotSettings({ bot, tab: tab0, onClose }: BotSettingsProps) {
  const isSuper = bot.kind === "super";
  const tabs: [SettingsTab, string][] = [["general", "General"], ["permissions", "Permissions"], ["browser", "Browser"], ["skills", "Skills"],
    ["routines", "Routines"], ["memory", "Memory"],
    ...(isSuper ? [["phone", "Phone"] as [SettingsTab, string]] : [])];
  const [tab, setTab] = useState<SettingsTab>(() => (tabs.some(([t]) => t === tab0) ? (tab0 as SettingsTab) : "general"));
  // What the dialog opened with (OpenBot's botDialog arguments).
  const [init] = useState(() => ({
    name: bot.name, model: bot.model || "sonnet", effort: bot.effort || "low", instructions: bot.instructions || "", memory: bot.memory || "",
    approval: bot.approval || "ask", buildAccess: bot.build_access || "scoped", terminal: bot.terminal === true, encrypt: !!bot.encrypt,
    maxSteps: bot.max_steps ? String(bot.max_steps) : "",
    imessage: bot.imessage || "", imessageEnabled: !!bot.imessage_enabled, imessageTo: bot.imessage_to || "",
    superAccess: bot.super_access || "ask", trustedApps: bot.trusted_apps || [], allowRules: bot.allow_rules || [],
    folders: bot.folders || [],
    browserId: isSuper ? "" : loginValue(bot),
  }));
  const [name, setName] = useState(init.name);
  const [model, setModel] = useState(init.model);
  const [effort, setEffort] = useState(init.effort);
  const [maxSteps, setMaxSteps] = useState(init.maxSteps);
  const [instructions, setInstructions] = useState(init.instructions);
  const [memory, setMemory] = useState(init.memory);
  const [approval, setApproval] = useState<string>(init.approval);
  const [buildAccess, setBuildAccess] = useState<string>(init.buildAccess);
  // The Terminal row shows for every bot except Super Bot, which has Bash.
  const terminalFeature = !isSuper;
  const [terminal, setTerminal] = useState(init.terminal);
  const [superAccess, setSuperAccess] = useState<string>(init.superAccess);
  const [encrypt, setEncrypt] = useState(init.encrypt);
  // Page-wide prefs (server prefs `bots_*`, every bot; store.ts hydrateBotsPrefs, D1386): they apply at once, not on Save.
  const [autoOpen, setAutoOpen] = useState(autoStage);
  const [sounds, setSoundsState] = useState(soundsOn);
  const [clearBtn, setClearBtn] = useState(clearContextOn);
  const [imessage, setImessage] = useState(init.imessage);
  const [imessageEnabled, setImessageEnabled] = useState(init.imessageEnabled);
  const [imessageTo, setImessageTo] = useState(init.imessageTo);
  const [profile, setProfile] = useState("");
  const [profiles, setProfiles] = useState<ChromeProfile[]>([]);
  const [trustedApps, setTrustedApps] = useState<string[]>(init.trustedApps);
  const [allowRules, setAllowRules] = useState<AllowRule[]>(init.allowRules);
  // Folders it may read (docs §2 `folders`): the user's own folders, read-only for the bot. The native folder dialog
  // (POST /api/fs/pick-folder, the same one the templates use) when this machine has one; a typed path otherwise.
  const [folders, setFolders] = useState<string[]>(init.folders);
  const [folderTyped, setFolderTyped] = useState<string | null>(null);  // the typed-path fallback, open or not
  const [picking, setPicking] = useState(false);
  const pickingRef = useRef(false);  // the state above is for the button; a double-click lands before the re-render
  const missing = new Set(bot.browser?.folders_missing || []);
  const [appRows, setAppRows] = useState<AppRow[]>([]);
  const [browserId, setBrowserId] = useState(init.browserId);
  // The Logins menu: one option per other browser (bots sharing one collapse into one; Super Bot's browser included),
  // listed as the dialog opened. Super Bot's own row is hidden.
  const [groups] = useState(() => (isSuper ? [] : loginGroups(getState().bots, bot.id)));
  const sharedNames = (bot.shared_with || []).map((o) => o.name).join(", ");
  const appliesToAll = sharedNames ? ` Applies to every bot sharing these logins (${sharedNames}).` : "";
  const [face, setFace] = useState<FaceT | null | undefined>(bot.face);
  const busy = useRef(false);  // the confirm (a sibling modal) or the avatar popover is up: outside presses are theirs
  // Held while the popover is open, released a tick after it closes: the press that closes it must not also close this.
  const holdFor = (open: boolean) => { if (open) busy.current = true; else window.setTimeout(() => { busy.current = false; }, 0); };

  const bm = useMemo(() => ({ id: bot.id, name: init.name, face }), [bot.id, init.name, face]);
  const read = (): BotDialogValue => ({ name: name.trim(), model, effort, instructions, memory, approval, buildAccess, ...(terminalFeature ? { terminal } : {}), encrypt, profile, maxSteps,
    face: faceOf(bm), imessage: imessage.trim(), imessageEnabled, imessageTo: imessageTo.trim(), preset: "",
    kind: isSuper ? "super" : "bot", superAccess, browserId, trustedApps, folders,
    allowRulesRemoved: init.allowRules.filter((r) => !allowRules.some((x) => x.kind === r.kind && x.key === r.key)) });  // the face shown is the face kept
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
  const addFolder = (p: string) => {
    const v = p.trim().replace(/\/+$/, "");
    if (!v) return;
    setFolders((cur) => (cur.includes(v) ? cur : [...cur, v]));
    setFolderTyped(null);
  };
  const pickFolder = async () => {
    if (pickingRef.current) return;
    pickingRef.current = true;
    setPicking(true);
    const held = busy.current;
    busy.current = true;  // the OS dialog is modal over the page: its close must not read as an outside press here
    try {
      const r = await request<{ path: string | null }>("POST", "/api/fs/pick-folder", { title: "Choose a folder the bot may read" }, "pick-folder");
      if (r?.path) addFolder(r.path);
    } catch (e) {
      // 501: no native dialog on this machine (a hosted session); 409: one is already open. Fall back to a typed path.
      if (e instanceof ApiError && (e.status === 501 || e.status === 409)) setFolderTyped("");
      else throw e;
    } finally {
      pickingRef.current = false;
      setPicking(false);
      window.setTimeout(() => { busy.current = held; }, 0);  // restore, never clear: a confirm may hold it meanwhile
    }
  };
  const toggleTrusted = (folder: string, on: boolean) => {
    const k = normApp(folder);
    setTrustedApps((cur) => on ? (cur.includes(k) ? cur : [...cur, k]) : cur.filter((x) => x !== k));
  };

  // Focus by id, not ref: the shadcn Input is a plain function component (no forwardRef on React 18), and the dialog
  // sets its own initial focus on open, so this waits a tick to land after it.
  useEffect(() => {
    if (tab !== "general") return;
    const t = window.setTimeout(() => { const el = document.getElementById("bmname") as HTMLInputElement | null; el?.focus(); el?.select(); }, 0);
    return () => window.clearTimeout(t);
  }, [tab]);

  const ok = () => { if (!okDisabled) onClose(read()); };
  /** askConfirm with the dialog held open while it is up (the sections' deletes, the dirty guard). */
  const confirm = async (title: string, text: string, okLabel?: string, danger?: boolean) => {
    busy.current = true;
    try { return await askConfirm(title, text, okLabel, danger); } finally { busy.current = false; }
  };
  // Escape / backdrop: at once when clean, after a confirm when dirty (Discard, or Cancel to keep editing).
  const tryClose = async () => {
    if (busy.current) return;
    if (okDisabled) { onClose(null); return; }
    if (await confirm("Discard changes?", "You have unsaved changes to this bot's settings.", "Discard")) onClose(null);
  };

  return (
    <Dialog open modal={false} onOpenChange={(open) => { if (!open) void tryClose(); }}>
      <DialogContent showCloseButton={false} className={cn(DIALOG_CLASS, DIALOG_SIZE)}>
        <DialogHeader className={HEADER_CLASS}>
          <DialogTitle>Settings · {n || init.name}</DialogTitle>
          <DialogDescription className="sr-only">How this bot thinks, what it may do without asking, its browser, its playbooks, its routines, its memory{isSuper ? " and your phone" : ""}.</DialogDescription>
        </DialogHeader>
        <div className="flex min-h-0 flex-1 gap-6 px-6 pb-5">
          <nav className="-ml-2.5 flex w-36 shrink-0 flex-col gap-0.5 self-start" aria-label="Settings sections">
            {tabs.map(([t, l]) => (
              <button key={t} type="button" aria-current={tab === t ? "page" : undefined} onClick={() => setTab(t)}
                className={cn("flex h-8 w-full cursor-pointer appearance-none items-center rounded-md border-0 bg-transparent px-2.5 text-sm outline-none transition-colors focus-visible:ring-3 focus-visible:ring-ring/50",
                  tab === t ? "bg-foreground/[0.08] font-medium text-foreground" : "text-muted-foreground hover:bg-foreground/[0.04] hover:text-foreground")}>
                {l}
                {t === "phone" && imessageEnabled && imessage ? <span className="ml-auto size-1.5 rounded-full bg-emerald-400" aria-label="connected" /> : null}
              </button>
            ))}
          </nav>
          <div className="min-h-0 min-w-0 flex-1 overflow-x-hidden overflow-y-auto" data-sec={tab}>
            {tab === "general" ? (
              <Rows>
                <Row title="Name" htmlFor="bmname">
                  <Input id="bmname" placeholder="e.g. LinkedIn scout" value={name} onChange={(e) => setName(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter") ok(); }} />
                </Row>
                {/* Super Bot's avatar is the fixed Claude mark: no picker (the backend refuses a change too). */}
                <Row title="Avatar" text={isSuper ? "Super Bot's mark is fixed." : "Shape and colour; the bot's face in the list and the chat."}>
                  <FacePopover subject={bm} onPick={setFace} onOpenChange={holdFor} disabled={isSuper} title={isSuper ? "Super Bot's avatar is fixed" : "Edit avatar"}>
                    {isSuper ? null : <span className="text-[13px] text-muted-foreground group-hover:text-foreground">Change</span>}
                    <span className="size-9 [&>svg]:block [&>svg]:size-full"><Face b={bm} /></span>
                  </FacePopover>
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
                <Row title="Steps per task" text={`Tool calls one task may take before it is stopped. Blank keeps the default (${isSuper ? 1000 : 200}). Applies from the next task.`} htmlFor="bmsteps">
                  <Input id="bmsteps" type="text" inputMode="numeric" pattern="[0-9]*" maxLength={5} className="w-full" value={maxSteps}
                    placeholder={isSuper ? "1000" : "200"} onChange={(e) => { if (/^\d*$/.test(e.target.value)) setMaxSteps(e.target.value); }} />
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
                {terminalFeature ? (
                  <Row title="Terminal"
                    text={bot.terminal_needs_claude ? "Needs a Claude model: the steps engine cannot use a terminal." : "Lets it run commands in its own terminal (you watch it live beside the browser). Every command asks unless you approved it forever."}
                    htmlFor="bmterminal">
                    <NativeSelect className="w-full" id="bmterminal" value={terminal ? "on" : "off"} disabled={!!bot.terminal_needs_claude}
                      onChange={(e) => setTerminal(e.target.value === "on")}>
                      <NativeSelectOption value="off">Off</NativeSelectOption>
                      <NativeSelectOption value="on">On</NativeSelectOption>
                    </NativeSelect>
                  </Row>
                ) : null}
                {allowRules.length ? (
                  <Row title="Approved forever" text="Calls this bot makes without asking: ones you approved with “Approve, don't ask again” on a card, and ones its preset pre-approved. Remove one and the bot asks again next time." stack>
                    <div className="max-h-48 divide-y divide-foreground/5 overflow-y-auto rounded-lg border border-input" id="bmforever">
                      {allowRules.map((r) => (
                        <div key={`${r.kind}:${r.key}`} className="flex items-center gap-2.5 px-3 py-1.5 text-sm">
                          <span className="min-w-0 flex-1 truncate">{r.label || r.key}</span>
                          {r.source ? <span className="shrink-0 text-xs text-muted-foreground">{`from the ${r.source} preset`}</span> : null}
                          <Button variant="ghost" size="icon-xs" aria-label="Ask again" title="Ask again" className="text-muted-foreground hover:text-destructive"
                            onClick={() => setAllowRules((cur) => cur.filter((x) => !(x.kind === r.kind && x.key === r.key)))}><Trash2Icon /></Button>
                        </div>
                      ))}
                    </div>
                  </Row>
                ) : null}
                <Row title="Folders it may read" stack
                  text={"Files and folders here are open to its ls, find and readfile. It never writes there. Apps it runs read what they read." + (isSuper ? " Its shell is not fenced." : "")}>
                  {folders.length ? (
                    <div className="max-h-48 divide-y divide-foreground/5 overflow-y-auto rounded-lg border border-input" id="bmfolders">
                      {folders.map((f) => (
                        <div key={f} className="flex items-center gap-2.5 px-3 py-1.5 text-sm">
                          <span className="min-w-0 flex-1 truncate font-mono text-[13px]" title={f}>{f}</span>
                          {missing.has(f) ? <span className="text-xs text-muted-foreground">missing</span> : null}
                          <Button variant="ghost" size="icon-xs" aria-label="Remove folder" title="Remove" className="text-muted-foreground hover:text-destructive"
                            onClick={() => setFolders((cur) => cur.filter((x) => x !== f))}><Trash2Icon /></Button>
                        </div>
                      ))}
                    </div>
                  ) : null}
                  {folderTyped !== null ? (
                    <div className="flex items-center gap-2">
                      <Input autoFocus placeholder="/full/path/to/folder" value={folderTyped} onChange={(e) => setFolderTyped(e.target.value)}
                        onKeyDown={(e) => { if (e.key === "Enter") addFolder(folderTyped); if (e.key === "Escape") setFolderTyped(null); }} />
                      <Button type="button" size="sm" disabled={!folderTyped.trim()} onClick={() => addFolder(folderTyped)}>Add</Button>
                      <Button type="button" variant="ghost" size="sm" onClick={() => setFolderTyped(null)}>Cancel</Button>
                    </div>
                  ) : (
                    <div><Button type="button" variant="outline" size="sm" disabled={picking} onClick={() => void pickFolder()}><FolderPlusIcon />Add folder…</Button></div>
                  )}
                </Row>
                {appRows.length ? (
                  <Row title="Apps it may use without asking" text="A ticked app's tools and scripts run at once. Everything else keeps the rule above." stack>
                    <div className="max-h-48 divide-y divide-foreground/5 overflow-y-auto rounded-lg border border-input" id="bmtrusted">
                      {appRows.map((a) => {
                        const on = trustedApps.includes(normApp(a.folder));
                        return (
                          <label key={a.folder} className="flex cursor-pointer items-center gap-2.5 px-3 py-2 text-sm hover:bg-foreground/[0.04]">
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
                {!isSuper && groups.length ? (
                  <Row title="Logins" text={(loginHint(groups.find((g) => g.id === browserId)) + " ").trimStart() + "Bots sharing logins drive one browser, each in its own tabs. Switching removes this bot's current logins unless another bot still uses them."} htmlFor="bmlogins">
                    <NativeSelect className="w-full" id="bmlogins" value={browserId} onChange={(e) => setBrowserId(e.target.value)}>
                      <NativeSelectOption value="">This bot only</NativeSelectOption>
                      {groups.map((g) => <NativeSelectOption key={g.id} value={g.id}>Same as {loginLabel(g)}</NativeSelectOption>)}
                    </NativeSelect>
                  </Row>
                ) : null}
                <Row title="Start from a Chrome profile" text={"Copies that profile's logins, cookies and extensions into the bot's browser, replacing what it has. Your own Chrome is not touched." + appliesToAll} htmlFor="bmprofile">
                  <NativeSelect className="w-full" id="bmprofile" value={profile} onChange={(e) => setProfile(e.target.value)}>
                    <NativeSelectOption value="">Keep this bot's own{bot.chrome_profile ? ` (from ${bot.chrome_profile})` : ""}</NativeSelectOption>
                    {profiles.map((p) => <NativeSelectOption key={p.dir} value={p.dir}>{p.name}{p.email ? ` · ${p.email}` : ""}</NativeSelectOption>)}
                  </NativeSelect>
                </Row>
                <Row title="Encrypt the browser profile at rest" text={"While the browser is closed the profile is one AES-256 file; the key lives in your macOS Keychain. Lose the Keychain item and saved logins are gone." + appliesToAll} htmlFor="bmencrypt">
                  <Switch id="bmencrypt" checked={encrypt} onCheckedChange={(c) => setEncrypt(!!c)} />
                </Row>
                <Row title="Open the browser when a bot needs me" text="When a bot asks you to sign in, its live page opens beside the chat by itself. Applies to every bot on this page." htmlFor="bmautostage">
                  <span title="Page-wide: applies to every bot, saved at once"><Switch id="bmautostage" checked={autoOpen} onCheckedChange={(c) => { setAutoStage(!!c); setAutoOpen(!!c); }} /></span>
                </Row>
                <Row title="Sounds" text="A short chime when a bot hands you its browser. Applies to every bot on this page." htmlFor="bmsounds">
                  <span title="Page-wide: applies to every bot, saved at once"><Switch id="bmsounds" checked={sounds} onCheckedChange={(c) => { setSounds(!!c); setSoundsState(!!c); }} /></span>
                </Row>
                <Row title="Clear context button" text="A button in the chat header that starts the bot over without leaving the conversation; the earlier messages fold under a marker. Off by default. Applies to every bot on this page." htmlFor="bmclear">
                  <span title="Page-wide: applies to every bot, saved at once"><Switch id="bmclear" checked={clearBtn} onCheckedChange={(c) => { setClearContext(!!c); setClearBtn(!!c); }} /></span>
                </Row>
              </Rows>
            ) : null}

            {tab === "skills" ? <SkillsSection b={bot} confirm={confirm} /> : null}
            {tab === "routines" ? <RoutinesSection b={bot} confirm={confirm} /> : null}

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
