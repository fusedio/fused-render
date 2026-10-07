// Settings > Phone on Super Bot (docs §10): the switch "Text Super Bot from your phone", then a four-step checklist
// until the link works (Full Disk Access · Messages signed in · your number · a test text), then the connected view
// (status line, change number, people it may text). The step is DERIVED from the switch, the shell's one Full Disk
// Access store (platform/lib/fda.ts, never a second probe), the bridge's state (own handles learned from chat.db,
// its error) and whether a text ever went out; only the switch, the handle and the contacts are written, through
// the dialog's Save. "Send a test text" saves the pending switch and handle first (the backend texts what is on
// disk), so the handshake never needs a Save before it.
import { useEffect, useState } from "react";
import { openFdaSettings } from "@platform/lib/api";
import { fdaCopy, pokeFda, relaunchHref, useFda } from "@platform/lib/fda";
import { api, type ImessageState } from "../lib/api";
import { imessageStatus } from "../lib/live";
import { act } from "../state/store";

export interface PhoneSectionProps {
  botId: string;
  handle: string; setHandle: (h: string) => void;
  enabled: boolean; setEnabled: (on: boolean) => void;
  contacts: string; setContacts: (c: string) => void;
}

const POLL_MS = 3000;

export function PhoneSection({ botId, handle, setHandle, enabled, setEnabled, contacts, setContacts }: PhoneSectionProps) {
  const fda = useFda();
  const [st, setSt] = useState<ImessageState | null>(null);
  const [other, setOther] = useState(false);          // "Another number…" input open
  const [testing, setTesting] = useState(false);
  const [tested, setTested] = useState<{ ok: boolean; text: string } | null>(null);
  const [opened, setOpened] = useState(false);        // System Settings was opened from here

  // The bridge's state, fresh while the section shows: the switch turning on makes the bridge read chat.db and learn
  // this Mac's own handles a poll or two later.
  useEffect(() => {
    let live = true, t: number | null = null;
    const tick = async () => {
      const r = await act(() => api.imessage(), true);
      if (!live) return;
      if (r) setSt(r);
      t = window.setTimeout(() => { void tick(); }, POLL_MS);
    };
    void tick();
    return () => { live = false; if (t !== null) window.clearTimeout(t); };
  }, []);

  const own = st?.own_handles || [];
  const fdaOk = fda === null ? null : !!fda?.granted;   // null: not offered (dev server, not macOS) → the bridge's error is the only word
  const fdaPending = !!fda?.pending_relaunch;
  const bridgeNoFda = /full disk access/i.test(st?.error || "");
  const signedIn = own.length > 0;
  const saved = !!st && st.enabled === enabled && (st.handle || "") === handle.trim();
  const sentOnce = !!st?.last_out || !!tested?.ok;
  const connected = enabled && !!handle.trim() && saved && !!st?.running && sentOnce;

  const openSettings = () => { setOpened(true); openFdaSettings().catch(() => {}).finally(pokeFda); };
  const sendTest = async () => {
    setTesting(true); setTested(null);
    try {
      if (!saved) await act(() => api.settings(botId, { imessage_handle: handle.trim(), imessage_enabled: enabled }));
      const r = await act(() => api.imessageTest(), true);
      if (r?.ok) setTested({ ok: true, text: `Sent to ${r.handle}. Check your phone.` });
      else setTested({ ok: false, text: "The text did not go out." });
    } catch (e) {
      setTested({ ok: false, text: (e as Error).message });
    } finally {
      setTesting(false);
    }
  };

  const step1 = fdaOk === true ? "ok" : fdaOk === null ? (bridgeNoFda ? "need" : "ok") : "need";
  const step2 = step1 !== "ok" ? "wait" : signedIn ? "ok" : "need";
  const step3 = step2 !== "ok" ? "wait" : handle.trim() ? "ok" : "need";
  const step4 = step3 !== "ok" ? "wait" : sentOnce ? "ok" : "need";
  const Dot = ({ s, n }: { s: string; n: number }) => <span className={`dot ${s}`}>{s === "ok" ? "✓" : n}</span>;

  return (
    <div className="phone">
      <label className="switchrow">
        <span>
          <b>Text Super Bot from your phone</b>
          <span className="why">Texts from your number become tasks. Replies come back as texts. Approvals stay on this Mac.</span>
        </span>
        <input type="checkbox" role="switch" id="bmphone" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
      </label>
      {!enabled ? (
        <p className="why">{handle.trim() ? `Off. Your number (${handle.trim()}) is remembered; turn the switch on to use it again.` : "Off. Turn it on to set your number up."}</p>
      ) : connected ? (
        <>
          <p className="stat">{imessageStatus(handle.trim(), st)}</p>
          <label className="field">Your number
            <input id="bmimsg" placeholder="+1 555 123 4567 or an Apple ID" autoComplete="off" value={handle} onChange={(e) => setHandle(e.target.value)} />
            <span className="why">{own.includes(handle.trim()) ? "Your own number: Super Bot's replies start with “@Super Bot” so you can tell them from your notes." : "A separate Apple ID: replies come as plain texts."}</span>
          </label>
          <label className="field">People Super Bot may text for you <small>· name + number, one per line</small>
            <textarea id="bmimsgto" rows={2} placeholder={"Ali +1 555 123 4567\nMom mom@icloud.com"} value={contacts} onChange={(e) => setContacts(e.target.value)} />
            <span className="why">Every text goes through the approval card unless Permissions says never ask. Your own number is always allowed.</span>
          </label>
          <p className="why">Trouble? <button type="button" className="link" disabled={testing} onClick={() => { void sendTest(); }}>Send a test text</button>
            {tested ? <span className={tested.ok ? "" : "bad"}> · {tested.text}</span> : null}</p>
        </>
      ) : (
        <ol className="steps">
          <li className={step1}><Dot s={step1} n={1} />
            <div><b>Full Disk Access</b>
              <span className="why">{step1 === "ok" ? "Granted. It lets Super Bot read Messages."
                : fdaPending ? fdaCopy().pending
                : opened ? fdaCopy().waiting
                : "Lets Super Bot read Messages. Granted once in System Settings; survives upgrades."}</span>
            </div>
            {step1 === "ok" ? null : fdaPending ? <a className="btn" href={relaunchHref()}>{fdaCopy().relaunch}</a>
              : <button type="button" onClick={openSettings}>{opened ? fdaCopy().reopen : fdaCopy().open}</button>}
          </li>
          <li className={step2}><Dot s={step2} n={2} />
            <div><b>Messages is signed in</b>
              <span className="why">{step2 === "ok" ? `Found ${own.join(", ")} on this Mac.`
                : step2 === "wait" ? "Checked after step 1."
                : st?.error && !bridgeNoFda ? st.error
                : "Open Messages, sign in with your Apple ID, and send any text so this Mac learns its number. This updates on its own."}</span>
            </div>
          </li>
          <li className={step3}><Dot s={step3} n={3} />
            <div><b>Your number</b>
              <span className="why">{step3 === "wait" ? "Which number you will text from." : "Which number will you text from?"}</span>
              {step3 !== "wait" ? (
                <div className="chips">
                  {own.map((h) => (
                    <button key={h} type="button" className={`chip${handle.trim() === h ? " on" : ""}`} onClick={() => { setHandle(h); setOther(false); }}>{h} · my own number</button>
                  ))}
                  <button type="button" className={`chip${other || (handle.trim() && !own.includes(handle.trim())) ? " on" : ""}`} onClick={() => setOther(true)}>Another…</button>
                </div>
              ) : null}
              {step3 !== "wait" && (other || (handle.trim() && !own.includes(handle.trim()))) ? (
                <input id="bmimsg" placeholder="+1 555 123 4567 or an Apple ID" autoComplete="off" value={handle} onChange={(e) => setHandle(e.target.value)} />
              ) : null}
              {step3 === "ok" ? <span className="why">{own.includes(handle.trim())
                ? "Texting yourself: Super Bot's replies start with “@Super Bot” so you can tell them from your own notes."
                : "A separate Apple ID signed into Messages here: replies come as plain texts."}</span> : null}
            </div>
          </li>
          <li className={step4}><Dot s={step4} n={4} />
            <div><b>Send a test text</b>
              <span className="why">{tested ? tested.text : "Super Bot texts you “Connected”. macOS asks once to let this app control Messages."}</span>
            </div>
            {step4 === "need" ? <button type="button" className="primary" disabled={testing} onClick={() => { void sendTest(); }}>{testing ? "Sending…" : "Send"}</button> : null}
          </li>
        </ol>
      )}
    </div>
  );
}
