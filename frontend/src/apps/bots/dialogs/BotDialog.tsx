// #bmodal (OpenBot dialogs.js botDialog): shared by "+ New bot" (create, after the preset chooser: a preset fills in
// name, model, instructions, face and the "Comes with N playbooks" note; a blank starter its name and face) and Settings (edit). The form is a snapshot
// of the bot as the dialog opened (later polls never reset what you typed). Save stays greyed until something
// differs from that snapshot; Create is always live. The backdrop with unsaved edits asks first; Cancel and Escape
// (in Name) are immediate; Enter in Name is OK.
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import { Face } from "../components/Face";
import { api, type AppRow, type Bot, type ChromeProfile, type Face as FaceT } from "../lib/api";
import { faceOf } from "../lib/face";
import { imessageStatus } from "../lib/live";
import { newBotInit, type NewBotPick } from "../lib/presets";
import { act, getState, useBotsSelector } from "../state/store";
import type { BotDialogValue } from "./actions";
import { askConfirm, pickFace } from "./ask";

export const MODELS: [string, string][] = [
  ["haiku", "Haiku · fastest"], ["sonnet", "Sonnet · balanced"], ["opus", "Opus · strongest"], ["fable", "Fable · most capable"],
  ["local-4b", "Gemma 4B · local model"], ["local-9b", "Gemma 12B · local model"],
];
export const EFFORTS: [string, string][] = [["low", "Low · quickest"], ["medium", "Medium"], ["high", "High · careful"], ["xhigh", "Extra high · slowest"]];

export interface BotDialogProps {
  /** The bot being edited; absent for "+ New bot". */
  bot?: Bot;
  /** "+ New bot": what the preset chooser picked (a preset or a blank starter). */
  pick?: NewBotPick;
  onClose: (v: BotDialogValue | null) => void;
}

export function BotDialog({ bot, pick, onClose }: BotDialogProps) {
  const editing = !!bot;
  // Super Bot (docs §5): Claude models only, a Mac-access toggle instead of Builds, no local models, and the only bot
  // with an owner phone handle (docs §10: one voice on the phone; it hands work to the others, §11).
  const isSuper = bot ? bot.kind === "super" : pick?.kind === "super";
  const [fresh] = useState(() => (pick ? newBotInit(pick) : null));
  const [title] = useState(() => (bot ? `Settings · ${bot.name}` : fresh?.title || "New bot"));
  // What the dialog opened with (OpenBot's botDialog arguments).
  const [init] = useState(() => bot
    ? { name: bot.name, model: bot.model || "sonnet", effort: bot.effort || "low", instructions: bot.instructions || "", memory: bot.memory || "",
        approval: bot.approval || "ask", buildAccess: bot.build_access || "scoped", encrypt: !!bot.encrypt, imessage: bot.imessage || "", imessageTo: bot.imessage_to || "",
        superAccess: bot.super_access || "ask", trustedApps: bot.trusted_apps || [] }
    : { name: fresh?.name || `Bot ${getState().bots.length + 1}`, model: fresh?.model || "sonnet", effort: "low", instructions: fresh?.instructions || "", memory: "",
        approval: "ask", buildAccess: "scoped", encrypt: false, imessage: "", imessageTo: "", superAccess: "ask", trustedApps: [] as string[] });
  const [name, setName] = useState(init.name);
  const [model, setModel] = useState(init.model);
  const [effort, setEffort] = useState(init.effort);
  const [instructions, setInstructions] = useState(init.instructions);
  const [memory, setMemory] = useState(init.memory);
  const [approval, setApproval] = useState(init.approval);
  const [buildAccess, setBuildAccess] = useState(init.buildAccess);
  const [superAccess, setSuperAccess] = useState(init.superAccess);
  const [encrypt, setEncrypt] = useState(init.encrypt);
  const [imessage, setImessage] = useState(init.imessage);
  const [imessageTo, setImessageTo] = useState(init.imessageTo);
  const [profile, setProfile] = useState("");
  const [profiles, setProfiles] = useState<ChromeProfile[]>([]);
  const [trustedApps, setTrustedApps] = useState<string[]>(init.trustedApps);
  const [appRows, setAppRows] = useState<AppRow[]>([]);
  const [face, setFace] = useState<FaceT | null | undefined>(bot ? bot.face : fresh?.face);
  const imsgState = useBotsSelector((s) => s.imessage);

  // The avatar subject: the face is hashed from the id (or, for a new bot, the name it opened with) until one is picked.
  const bm = useMemo(() => ({ id: bot?.id, name: init.name, face }), [bot?.id, init.name, face]);
  const read = (): BotDialogValue => ({ name: name.trim(), model, effort, instructions, memory, approval, buildAccess, encrypt, profile,
    face: faceOf(bm), imessage: imessage.trim(), imessageTo: imessageTo.trim(), preset: fresh?.preset || "",
    kind: isSuper ? "super" : "bot", superAccess, trustedApps });  // the face shown is the face kept
  const [initial] = useState(() => JSON.stringify(read()));
  const okDisabled = editing && JSON.stringify(read()) === initial;

  // The title tracks the Name field ("Settings · <name>"); falls back to the given title when Name is blank.
  const [prefix] = title.split(" · ");
  const n = name.trim();
  const shownTitle = n && title.includes(" · ") ? `${prefix} · ${n}` : title;

  // Your Chrome's profiles, listed fresh each time; picking one copies it into the bot on save.
  useEffect(() => {
    let live = true;
    void act(() => api.profiles(), true).then((r) => { if (live) setProfiles(r?.profiles || []); });
    void act(() => api.apps(), true).then((r) => { if (live) setAppRows(r?.apps || []); });
    return () => { live = false; };
  }, []);

  // Trusted apps: the backend stores folders normalised (lower, -/_/space → "-"); match the listing the same way so a
  // saved entry ticks its row whatever the folder's spelling on disk.
  const normApp = (f: string) => f.trim().toLowerCase().replace(/[-_\s]+/g, "-");
  const toggleTrusted = (folder: string, on: boolean) => {
    const n = normApp(folder);
    setTrustedApps((cur) => on ? (cur.includes(n) ? cur : [...cur, n]) : cur.filter((x) => x !== n));
  };

  // Instructions and Memory grow with their text (up to the CSS max); refit on open (after layout: a hidden textarea reports 0) and as you type.
  const instrRef = useRef<HTMLTextAreaElement>(null), memRef = useRef<HTMLTextAreaElement>(null), nameRef = useRef<HTMLInputElement>(null);
  const fitBmText = () => { for (const t of [instrRef.current, memRef.current]) if (t) { t.style.height = "0"; t.style.height = t.scrollHeight + 2 + "px"; } };
  useLayoutEffect(() => {
    nameRef.current?.focus(); nameRef.current?.select();
    fitBmText(); const raf = requestAnimationFrame(fitBmText);
    return () => cancelAnimationFrame(raf);
  }, []);

  const ok = () => { if (!okDisabled) onClose(read()); };
  const editAvatar = async () => {
    const f = await pickFace(bm, (d) => setFace(d));
    setFace(f);
  };
  // Click on the backdrop dismisses; with unsaved edits it asks first (Discard, or Cancel to keep editing).
  const onBackdrop = async (e: MouseEvent<HTMLDivElement>) => {
    if (e.target !== e.currentTarget) return;
    if (okDisabled || !editing) { onClose(null); return; }
    if (await askConfirm("Discard changes?", "You have unsaved changes to this bot's settings.", "Discard")) onClose(null);
  };

  return (
    <div id="bmodal" className="modal show" role="dialog" aria-modal="true" aria-labelledby="bmtitle" onClick={(e) => { void onBackdrop(e); }}>
      <div className="box">
        <h3 id="bmtitle">{shownTitle}</h3>
        <div className="body">
          {/* Super Bot's avatar is the fixed Claude mark: no picker (the backend refuses a change too). */}
          <div className={`avwrap${isSuper ? " nopick" : ""}`} id="bmavwrap" title={isSuper ? "Super Bot's avatar is fixed" : "Edit avatar"}
            onClick={isSuper ? undefined : () => { void editAvatar(); }}>
            <span className="av" id="bmface"><Face b={bm} /></span>{isSuper ? null : <span className="hint">Edit avatar</span>}
          </div>
          <label className="field">Name<input id="bmname" ref={nameRef} placeholder="e.g. LinkedIn scout" value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") ok(); if (e.key === "Escape") onClose(null); }} /></label>
          {/* "Comes with N playbooks: …" for a preset bot */}
          <p className="muted preset" id="bmpreset" style={{ display: fresh?.presetNote ? "" : "none" }}>{fresh?.presetNote || ""}</p>
          <div className="pair">
            <label className="field">Model
              <select id="bmmodel" title="Applies from the next task" value={model} onChange={(e) => setModel(e.target.value)}>
                {(isSuper ? MODELS.filter(([v]) => !v.startsWith("local-")) : MODELS).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </label>
            <label className="field">Effort
              <select id="bmeffort" title="How much the bot thinks per step; applies from the next task" value={effort} onChange={(e) => setEffort(e.target.value)}>
                {EFFORTS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </label>
          </div>
          <label className="field">Instructions
            <textarea id="bminstr" ref={instrRef} rows={2} value={instructions} onChange={(e) => { setInstructions(e.target.value); fitBmText(); }}
              placeholder={isSuper ? "Standing rules for every task. Blank keeps Super Bot's own: files in its Inbox folder, say before anything destructive."
                : "Standing rules for every task, e.g. “Browse LinkedIn for me. Answer in bullets. Never like or comment.”"} /></label>
          <label className="field" id="bmmemwrap" style={{ display: editing ? "" : "none" }}>Memory <small>· notes the bot keeps between tasks</small>
            <textarea id="bmmem" ref={memRef} rows={3} value={memory} onChange={(e) => { setMemory(e.target.value); fitBmText(); }}
              placeholder="Empty. The bot adds site quirks and your preferences here as it learns." /></label>
          {/* Advanced always starts collapsed; opening it scrolls the dialog body so every field in it is in view, not just the first one. */}
          <details className="adv" id="bmadv" onToggle={(e) => {
            const d = e.currentTarget; if (!d.open) return;
            const body = d.parentElement; requestAnimationFrame(() => body?.scrollTo({ top: body.scrollHeight, behavior: "smooth" }));
          }}>
            <summary>Advanced <small>{isSuper ? "· Mac access, approvals, trusted apps, browser profile, iMessage, contacts, encryption" : "· approvals, trusted apps, builds, browser profile, contacts, encryption"}</small></summary>
            {isSuper ? (
              <label className="field" title="Super Bot runs Claude Code's own tools. Ask: a card in the chat before every file write, edit and shell command (reading never asks). Unattended: Claude Code's own judgement approves what it considers safe and asks about the rest. Once a task has read the web, every write and command asks either way.">Mac access <small>· files, shell and code on this Mac</small>
                <select id="bmsuper" value={superAccess} onChange={(e) => setSuperAccess(e.target.value)}>
                  <option value="ask">Ask before writes, edits and shell commands</option>
                  <option value="full">Unattended · Claude judges, asks about the rest</option>
                </select>
              </label>
            ) : null}
            <label className="field">Approvals
              <select id="bmapproval" value={approval} onChange={(e) => setApproval(e.target.value)}>
                <option value="ask">Ask before irreversible actions (send, buy, delete, post)</option>
                <option value="auto">Never ask</option>
              </select>
            </label>
            {appRows.length ? (
              <div className="field" title="Tools and scripts of a ticked app run at once, with no approval card, however they read. Everything else (browser actions, texts, builds, other apps) keeps the Approvals rule above. A call the bot itself flags as irreversible still asks.">
                Trusted apps <small>· these apps never ask this bot for approval</small>
                <div className="trusted" id="bmtrusted">
                  {appRows.map((a) => (
                    <label key={a.folder} className="trow">
                      <input type="checkbox" checked={trustedApps.includes(normApp(a.folder))} onChange={(e) => toggleTrusted(a.folder, e.target.checked)} />
                      <span>{a.name || a.folder}</span>{a.name && a.name !== a.folder ? <small>{a.folder}</small> : null}
                    </label>
                  ))}
                </div>
              </div>
            ) : null}
            <label className="field" style={{ display: isSuper ? "none" : "" }} title="The bot's `build` action hands a spec to Claude Code, which creates a fused-render app under ~/Fused/app. Scoped: Claude asks you before risky tools (the build parks under Builds until you answer). Full access: it runs unattended.">Builds <small>· when this bot asks Claude Code to create an app</small>
              <select id="bmbuild" value={buildAccess} onChange={(e) => setBuildAccess(e.target.value)}>
                <option value="scoped">Scoped · Claude asks before risky steps</option>
                <option value="full">Full access · runs unattended</option>
              </select>
            </label>
            <label className="field" title="Copies that Chrome profile (logins, cookies, extensions, history) into this bot's browser, replacing what it has now. Your own Chrome is not touched.">Browser profile <small>· start from one of your Chrome profiles</small>
              <select id="bmprofile" value={profile} onChange={(e) => setProfile(e.target.value)}>
                <option value="">Keep this bot's own profile{bot?.chrome_profile ? ` (copied from ${bot.chrome_profile})` : ""}</option>
                {profiles.map((p) => <option key={p.dir} value={p.dir}>{p.name}{p.email ? ` · ${p.email}` : ""}</option>)}
              </select>
            </label>
            {/* Super Bot is the one door from the phone (docs §10): only it has an owner handle; other bots get phone work through its hand-offs. */}
            <label className="field" style={{ display: isSuper ? "" : "none" }} title="Texts from this number (or Apple ID email) to you on this Mac become tasks for Super Bot. It texts back only replies to those tasks, including what the bots it hands work to send back; nothing else is forwarded. Approvals stay on this Mac. Needs Messages signed in here and Full Disk Access for FusedRender; see imessage.py.">iMessage <small>· phone number or Apple ID that can text Super Bot</small>
              <input id="bmimsg" placeholder="+1 555 123 4567 · blank = off" autoComplete="off" value={imessage} onChange={(e) => setImessage(e.target.value)} />
              <small className="stat" id="bmimsgstat">{imessageStatus(init.imessage, imsgState)}</small>
            </label>
            <label className="field" title={`The only people the bot's \`text\` action can iMessage, one per line: a name and a phone number or Apple ID.${isSuper ? " The number above is always allowed." : ""} Every text goes through the approval gate unless Approvals is “Never ask”.`}>Contacts the bot may text <small>· name + number, one per line</small>
              <textarea id="bmimsgto" rows={2} placeholder={"Ali +1 555 123 4567\nMom mom@icloud.com"} value={imessageTo} onChange={(e) => setImessageTo(e.target.value)} /></label>
            <label className="field check" title="While the browser is closed, the profile is one AES-256 file; the key lives in your macOS Keychain. Lose the Keychain item and saved logins are gone.">
              <input type="checkbox" id="bmencrypt" checked={encrypt} onChange={(e) => setEncrypt(e.target.checked)} /> Encrypt browser profile at rest <small>· key in macOS Keychain</small>
            </label>
          </details>
        </div>
        <div className="row">
          <button id="bmcancel" onClick={() => onClose(null)}>Cancel</button>
          <button id="bmok" className="primary" disabled={okDisabled} onClick={ok}>{editing ? "Save" : "Create"}</button>
        </div>
      </div>
    </div>
  );
}
