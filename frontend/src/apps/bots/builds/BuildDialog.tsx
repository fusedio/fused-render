// #bdmodal (OpenBot index.html + builds.js buildDialog): the New build dialog, and with an app the "New task · <app>"
// variant that asks for a change to that existing app (name and folder fixed). buildDialog(app?) in builds.ts opens it
// and resolves with the fields (or null). Esc / backdrop / Cancel close; ⌘↩ or Ctrl+Enter starts when allowed.
import { useEffect, useRef, useState } from "react";
import { slugOf, useBuildDialog, useBuildsRoot, type BuildDialogReq, type BuildDialogResult } from "./builds";

function Box({ req }: { req: BuildDialogReq }) {
  const app = req.app, root = useBuildsRoot();
  const [name, setName] = useState(app ? app.name || app.folder || "" : "");
  const [prompt, setPrompt] = useState("");
  const [model, setModel] = useState("opus");
  const [effort, setEffort] = useState("high");
  const [mode, setMode] = useState("default");
  const nameRef = useRef<HTMLInputElement>(null), promptRef = useRef<HTMLTextAreaElement>(null);
  const disabled = (!app && !name.trim()) || !prompt.trim();
  const read = (): BuildDialogResult => ({ name: name.trim(), prompt, model, effort, permissionMode: mode });
  const where = app ? app.dir : `${root}/${name.trim() ? slugOf(name.trim()) : "…"}`;

  // Latest values for the capture-phase key handler.
  const live = useRef({ disabled, read });
  live.current = { disabled, read };
  useEffect(() => {
    (app ? promptRef.current : nameRef.current)?.focus();
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); req.resolve(null); }
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !live.current.disabled) { e.stopPropagation(); req.resolve(live.current.read()); }
    };
    document.addEventListener("keydown", key, true);
    return () => document.removeEventListener("keydown", key, true);
  }, [req, app]);

  return (
    <div className="box">
      <h3 id="bdtitle">{app ? `New task · ${app.name || app.folder || "App"}` : "New build"}</h3>
      <div className="body">
        <label className="field" id="bdnamefield" style={app ? { display: "none" } : undefined}>App name<input id="bdname" ref={nameRef} placeholder="e.g. Invoice tracker" value={name} onChange={(e) => setName(e.target.value)} /></label>
        <p className="muted where">Folder: <code id="bddir">{where}</code></p>
        <label className="field"><span id="bdask">{app ? "What should change?" : "What should the app do?"}</span>
          <textarea id="bdprompt" ref={promptRef} rows={6} value={prompt} onChange={(e) => setPrompt(e.target.value)}
            placeholder={app ? "Describe the change: what to add, fix or remove. Claude runs inside the app's folder with the fused-render app contract." : "Describe the app: what it shows, what data it reads, what the user can do. Claude gets the fused-render app contract on top of this."} />
        </label>
        <div className="pair">
          <label className="field">Model
            <select id="bdmodel" value={model} onChange={(e) => setModel(e.target.value)}>
              <option value="sonnet">Sonnet · balanced</option>
              <option value="opus">Opus · strongest</option>
              <option value="fable">Fable · most capable</option>
            </select>
          </label>
          <label className="field">Effort
            <select id="bdeffort" value={effort} onChange={(e) => setEffort(e.target.value)}>
              <option value="medium">Medium</option>
              <option value="high">High · careful</option>
              <option value="xhigh">Extra high · slowest</option>
            </select>
          </label>
        </div>
        <label className="field">Approvals
          <select id="bdmode" value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="default">Ask before risky tools (answer in the Builds panel)</option>
            <option value="auto">Never ask · unattended</option>
            <option value="plan">Plan only · no edits</option>
          </select>
        </label>
      </div>
      <div className="row">
        <button id="bdcancel" onClick={() => req.resolve(null)}>Cancel</button>
        <button id="bdok" className="primary" title="⌘↩ / Ctrl+Enter" disabled={disabled} onClick={() => { if (!disabled) req.resolve(read()); }}>{app ? "Start task" : "Start build"}</button>
      </div>
    </div>
  );
}

export function BuildDialog() {
  const req = useBuildDialog();
  return (
    <div id="bdmodal" className={`modal${req ? " show" : ""}`} onClick={(e) => { if (req && e.target === e.currentTarget) req.resolve(null); }}>
      {/* Keyed by request: every open starts from OpenBot's defaults (opus · high · default). */}
      {req ? <Box key={req.seq} req={req} /> : null}
    </div>
  );
}
