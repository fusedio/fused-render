// "Install Chrome" for WSL (bots/chrome_install.py): a fresh WSL distro has no browser for the bots to launch, and
// the one on Windows cannot be driven. One click downloads Google's package and installs it with apt as root in the
// distro. Draws nothing unless the server says the button can work here (GET /api/bots/setup: chrome.install.available)
// or an install is already under way. Shown under the empty hero's setup lines and under a "no browser found" error.
import { useEffect, useRef, useState } from "react";
import { api, type ChromeInstallPlan, type ChromeInstallStatus, type SetupReply } from "../lib/api";
import { chromeInstallPlan } from "../lib/setup";

const POLL_MS = 1000;

/** `setup`: the reply the caller already has (the hero); absent = fetch it here (the thread). `onDone`: Chrome is in. */
export function ChromeInstall({ setup, onDone, after }: { setup?: SetupReply | null; onDone?: () => void; after?: string }) {
  const [own, setOwn] = useState<SetupReply | null>(null);
  const [st, setSt] = useState<ChromeInstallStatus | null>(null);
  const [err, setErr] = useState("");
  const done = useRef(onDone);
  done.current = onDone;
  const fetches = setup === undefined;

  useEffect(() => {
    let live = true;
    if (fetches) api.setup().then((r) => { if (live) setOwn(r); }, () => { /* no check, no button */ });
    // An install started from another window (or before a reload) shows here as running, not as a fresh button.
    api.chromeInstallStatus().then((r) => { if (live && r.state === "running") setSt(r); }, () => { /* older server */ });
    return () => { live = false; };
  }, [fetches]);

  const running = st?.state === "running";
  useEffect(() => {
    if (!running) return;
    let live = true;
    const t = window.setInterval(() => {
      api.chromeInstallStatus().then((r) => {
        if (!live) return;
        setSt(r);
        if (r.state === "done") done.current?.();
      }, () => { /* a missed poll: the next one answers */ });
    }, POLL_MS);
    return () => { live = false; window.clearInterval(t); };
  }, [running]);

  const plan: ChromeInstallPlan | null = chromeInstallPlan(fetches ? own : setup ?? null);
  if (!plan && !st) return null;

  const start = () => {
    setErr("");
    api.chromeInstall().then(setSt, (e: unknown) => setErr(e instanceof Error ? e.message : String(e)));
  };
  if (st?.state === "done") {
    return <div className="chromeinstall"><span className="ok">Google Chrome is installed.{after ? ` ${after}` : ""}</span></div>;
  }
  const failed = st?.state === "error" ? st.error || "The install failed." : err;
  return (
    <div className="chromeinstall">
      <button type="button" className="primary" onClick={start} disabled={running}
        title="Downloads Google Chrome and installs it with apt, as root in your WSL distro">
        {running ? "Installing Chrome…" : failed ? "Try again" : "Install Chrome in WSL"}
      </button>
      {running
        ? <span className="muted" role="status">{st?.detail || "Starting"}</span>
        : failed
          ? <span className="warn" role="alert">{failed}</span>
          : <span className="muted">Downloads Google Chrome and installs it with apt in your WSL distro. No password needed.</span>}
      {failed && st?.output ? <pre className="out">{st.output}</pre> : null}
      {failed && plan?.command ? <span className="muted">Or paste this into a WSL terminal: <code>{plan.command}</code></span> : null}
    </div>
  );
}
