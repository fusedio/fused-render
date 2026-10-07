// Settings (edit an existing bot): a rail of sections instead of one long form with an Advanced fold. General ·
// Permissions · Browser · Memory, plus Phone on Super Bot (the one bot reachable over iMessage, docs §10). Each
// control keeps its backend key; only grouping and copy differ from the old #bmodal, and every select says in one
// line what the choice does. The form is a snapshot of the bot as the dialog opened (later polls never reset what
// you typed). Save stays greyed until something differs from that snapshot. The backdrop with unsaved edits asks
// first; Cancel and Escape (in Name) are immediate; Enter in Name is Save.
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import { Face } from "../components/Face";
import { api, type AppRow, type Bot, type ChromeProfile, type Face as FaceT } from "../lib/api";
import { EFFORTS, modelsFor, normApp } from "../lib/botform";
import { faceOf } from "../lib/face";
import { act } from "../state/store";
import type { BotDialogValue } from "./actions";
import { askConfirm, pickFace } from "./ask";
import { PhoneSection } from "./PhoneSection";

export type SettingsTab = "general" | "permissions" | "browser" | "memory" | "phone";

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

  const bm = useMemo(() => ({ id: bot.id, name: init.name, face }), [bot.id, init.name, face]);
  const read = (): BotDialogValue => ({ name: name.trim(), model, effort, instructions, memory, approval, buildAccess, encrypt, profile,
    face: faceOf(bm), imessage: imessage.trim(), imessageEnabled, imessageTo: imessageTo.trim(), preset: "",
    kind: isSuper ? "super" : "bot", superAccess, trustedApps });  // the face shown is the face kept
  const [initial] = useState(() => JSON.stringify(read()));
  const okDisabled = JSON.stringify(read()) === initial;
  const n = name.trim();
  const shownTitle = `Settings · ${n || init.name}`;

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

  // Instructions and Memory grow with their text (up to the CSS max); refit when their section shows and as you type.
  const instrRef = useRef<HTMLTextAreaElement>(null), memRef = useRef<HTMLTextAreaElement>(null), nameRef = useRef<HTMLInputElement>(null);
  const fitText = () => { for (const t of [instrRef.current, memRef.current]) if (t) { t.style.height = "0"; t.style.height = t.scrollHeight + 2 + "px"; } };
  useLayoutEffect(() => {
    if (tab === "general") { nameRef.current?.focus(); nameRef.current?.select(); }
    fitText(); const raf = requestAnimationFrame(fitText);
    return () => cancelAnimationFrame(raf);
  }, [tab]);

  const ok = () => { if (!okDisabled) onClose(read()); };
  const editAvatar = async () => { setFace(await pickFace(bm, (d) => setFace(d))); };
  // Click on the backdrop dismisses; with unsaved edits it asks first (Discard, or Cancel to keep editing).
  const onBackdrop = async (e: MouseEvent<HTMLDivElement>) => {
    if (e.target !== e.currentTarget) return;
    if (okDisabled) { onClose(null); return; }
    if (await askConfirm("Discard changes?", "You have unsaved changes to this bot's settings.", "Discard")) onClose(null);
  };

  return (
    <div id="bmodal" className="modal show wide" role="dialog" aria-modal="true" aria-labelledby="bmtitle" onClick={(e) => { void onBackdrop(e); }}>
      <div className="box">
        <h3 id="bmtitle">{shownTitle}</h3>
        <div className="rail">
          <nav aria-label="Settings sections">
            {tabs.map(([t, l]) => (
              <button key={t} type="button" className={`railtab${tab === t ? " on" : ""}${t === "phone" && imessageEnabled && imessage ? " live" : ""}`}
                aria-current={tab === t ? "page" : undefined} onClick={() => setTab(t)}>{l}</button>
            ))}
          </nav>
          <div className="body sec" data-sec={tab}>
            {tab === "general" ? (<>
              {/* Super Bot's avatar is the fixed Claude mark: no picker (the backend refuses a change too). */}
              <div className={`avwrap${isSuper ? " nopick" : ""}`} id="bmavwrap" title={isSuper ? "Super Bot's avatar is fixed" : "Edit avatar"}
                onClick={isSuper ? undefined : () => { void editAvatar(); }}>
                <span className="av" id="bmface"><Face b={bm} /></span>{isSuper ? null : <span className="hint">Edit avatar</span>}
              </div>
              <label className="field">Name<input id="bmname" ref={nameRef} placeholder="e.g. LinkedIn scout" value={name}
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") ok(); if (e.key === "Escape") onClose(null); }} /></label>
              <label className="field">What it should do
                <textarea id="bminstr" ref={instrRef} rows={2} value={instructions} onChange={(e) => { setInstructions(e.target.value); fitText(); }}
                  placeholder={isSuper ? "Standing rules for every task. Blank keeps Super Bot's own: files in its Inbox folder, say before anything destructive."
                    : "Standing rules for every task, e.g. “Browse LinkedIn for me. Answer in bullets. Never like or comment.”"} /></label>
              <div className="pair">
                <label className="field">Model
                  <select id="bmmodel" value={model} onChange={(e) => setModel(e.target.value)}>
                    {modelsFor(isSuper).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                  </select>
                </label>
                <label className="field">Thinking
                  <select id="bmeffort" value={effort} onChange={(e) => setEffort(e.target.value)}>
                    {EFFORTS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                  </select>
                </label>
              </div>
              <p className="why">Model and thinking apply from the next task.</p>
            </>) : null}

            {tab === "permissions" ? (<>
              {isSuper ? (
                <label className="field">On this Mac <small>· files, shell and code</small>
                  <select id="bmsuper" value={superAccess} onChange={(e) => setSuperAccess(e.target.value)}>
                    <option value="ask">Ask before writes, edits and shell commands</option>
                    <option value="full">Unattended · Claude judges, asks about the rest</option>
                  </select>
                  <span className="why">Reading never asks. Once a task has read the web, every write and command asks either way.</span>
                </label>
              ) : null}
              <label className="field">{isSuper ? "Before it sends, buys, deletes or posts" : "Before it buys, deletes or posts"}
                <select id="bmapproval" value={approval} onChange={(e) => setApproval(e.target.value)}>
                  <option value="ask">Ask me first</option>
                  <option value="auto">Never ask</option>
                </select>
                <span className="why">“Never ask” lets it finish on its own. A step the bot itself flags as irreversible still shows a card.</span>
              </label>
              {!isSuper ? (
                <label className="field">When it builds an app with Claude Code
                  <select id="bmbuild" value={buildAccess} onChange={(e) => setBuildAccess(e.target.value)}>
                    <option value="scoped">Ask before risky steps</option>
                    <option value="full">Run unattended</option>
                  </select>
                  <span className="why">Builds land under ~/Fused/app and park under Builds while a question waits.</span>
                </label>
              ) : null}
              {appRows.length ? (
                <div className="field">
                  Apps it may use without asking
                  <div className="trusted" id="bmtrusted">
                    {appRows.map((a) => (
                      <label key={a.folder} className="trow">
                        <input type="checkbox" checked={trustedApps.includes(normApp(a.folder))} onChange={(e) => toggleTrusted(a.folder, e.target.checked)} />
                        <span>{a.name || a.folder}</span>{a.name && a.name !== a.folder ? <small>{a.folder}</small> : null}
                      </label>
                    ))}
                  </div>
                  <span className="why">A ticked app's tools and scripts run at once. Everything else keeps the rule above.</span>
                </div>
              ) : null}
            </>) : null}

            {tab === "browser" ? (<>
              <label className="field">Start from a Chrome profile
                <select id="bmprofile" value={profile} onChange={(e) => setProfile(e.target.value)}>
                  <option value="">Keep this bot's own profile{bot.chrome_profile ? ` (copied from ${bot.chrome_profile})` : ""}</option>
                  {profiles.map((p) => <option key={p.dir} value={p.dir}>{p.name}{p.email ? ` · ${p.email}` : ""}</option>)}
                </select>
                <span className="why">Copies that profile's logins, cookies and extensions into the bot's browser, replacing what it has. Your own Chrome is not touched.</span>
              </label>
              <label className="field check">
                <input type="checkbox" id="bmencrypt" checked={encrypt} onChange={(e) => setEncrypt(e.target.checked)} /> Encrypt the browser profile at rest <small>· key in macOS Keychain</small>
              </label>
              <p className="why">While the browser is closed the profile is one AES-256 file. Lose the Keychain item and saved logins are gone.</p>
            </>) : null}

            {tab === "memory" ? (<>
              <label className="field" id="bmmemwrap">Notes it keeps between tasks
                <textarea id="bmmem" ref={memRef} rows={5} value={memory} onChange={(e) => { setMemory(e.target.value); fitText(); }}
                  placeholder="Empty. The bot adds site quirks and your preferences here as it learns." /></label>
              <div className="row left"><button type="button" disabled={!memory} onClick={() => setMemory("")}>Clear</button></div>
            </>) : null}

            {tab === "phone" && isSuper ? (
              <PhoneSection botId={bot.id} handle={imessage} setHandle={setImessage} enabled={imessageEnabled} setEnabled={setImessageEnabled}
                contacts={imessageTo} setContacts={setImessageTo} />
            ) : null}
          </div>
        </div>
        <div className="row">
          <button id="bmcancel" onClick={() => onClose(null)}>Cancel</button>
          <button id="bmok" className="primary" disabled={okDisabled} onClick={ok}>Save</button>
        </div>
      </div>
    </div>
  );
}
