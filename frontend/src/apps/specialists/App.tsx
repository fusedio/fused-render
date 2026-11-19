// THE SPECIALISTS PAGE (PRODUCT §8; mock-up Main.dc.html), mounted by the
// shell for every /specialists* path while the flag is on (shell/App.tsx).
// Laid out like the Bots page: the list on the left, ONE chat in the middle,
// the side panel on the right. There is no list of conversations.
import { useCallback, useEffect, useRef, useState } from "react";
import { useUrlVersion } from "@platform/lib/hooks";
import { navigateUrl } from "@platform/lib/router";
import {
  errorText,
  getGeneralInbox,
  getSpecialist,
  listSpecialists,
  type Note,
  type SpecialistDetail,
  type SpecialistSummary,
} from "@platform/lib/specialists-api";
import { GeneralInbox } from "./dialogs/GeneralInbox";
import { parseSpecialistsPath, specialistHref, specialistsHref } from "./routes";
import { MainChat, type ChatHandle } from "./components/MainChat";
import { SidePanel } from "./components/SidePanel";
import { SpecialistList } from "./components/SpecialistList";
import { CreateSpecialist } from "./dialogs/CreateSpecialist";
import { SpecialistSettings, type SettingsTab } from "./dialogs/SpecialistSettings";
import "./styles/specialists.css";

export const LIST_POLL_MS = 20_000;

export function Specialists() {
  useUrlVersion();
  const route = parseSpecialistsPath(location.pathname);
  const [rows, setRows] = useState<SpecialistSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState("");
  const [detail, setDetail] = useState<SpecialistDetail | null>(null);
  const [detailError, setDetailError] = useState("");
  const [creating, setCreating] = useState(false);
  const [settings, setSettings] = useState<SettingsTab | null>(null);
  const [panelKey, setPanelKey] = useState(0);
  const [general, setGeneral] = useState<Note[]>([]);
  const [generalOpen, setGeneralOpen] = useState(false);
  /** The selected chat's own run state (the list's "Working…" before its next poll). */
  const [runningHere, setRunningHere] = useState(false);
  /** Narrow layout: the side panel shows over the chat while this is on. */
  const [sideOpen, setSideOpen] = useState(false);
  const chat = useRef<ChatHandle | null>(null);
  const selected = route.kind === "specialist" ? route.id : null;

  const loadList = useCallback(() => {
    listSpecialists().then(
      (r) => {
        setRows(Array.isArray(r) ? r : []);
        setListError("");
        setLoading(false);
      },
      (err) => {
        setListError(errorText(err));
        setLoading(false);
      },
    );
    getGeneralInbox().then(
      (r) => setGeneral(Array.isArray(r?.notes) ? r.notes : []),
      () => {},
    );
  }, []);
  useEffect(() => {
    loadList();
    const t = setInterval(loadList, LIST_POLL_MS);
    return () => clearInterval(t);
  }, [loadList]);

  // The bare door shows the first specialist once the list knows one.
  useEffect(() => {
    if (route.kind === "list" && rows.length) void navigateUrl(specialistHref(rows[0].id), { replace: true });
  }, [route.kind, rows]);

  useEffect(() => {
    setSideOpen(false);
    setRunningHere(false);
    if (!selected) {
      setDetail(null);
      return;
    }
    let live = true;
    setDetail((d) => (d && d.id === selected ? d : null));
    setDetailError("");
    getSpecialist(selected).then(
      (d) => live && setDetail(d),
      (err) => live && setDetailError(errorText(err)),
    );
    return () => {
      live = false;
    };
  }, [selected]);

  const sendToChat = useCallback((text: string) => {
    const ok = chat.current?.send(text) ?? false;
    if (ok) setPanelKey((n) => n + 1);
    return ok;
  }, []);

  // Escape closes the narrow layout's panel (not while a dialog is up: its own
  // Escape is the one that counts).
  useEffect(() => {
    if (!sideOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented || settings || creating || generalOpen) return;
      setSideOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [sideOpen, settings, creating, generalOpen]);

  return (
    <div className="spx-page">
      <div className={"spx-cols" + (sideOpen ? " is-side-open" : "")}>
        <SpecialistList
          rows={rows}
          selected={selected}
          loading={loading}
          error={listError}
          onSelect={(id) => void navigateUrl(specialistHref(id))}
          onCreate={() => setCreating(true)}
          generalCount={general.length}
          onOpenGeneral={() => setGeneralOpen(true)}
          runningHere={runningHere}
        />
        {detail ? (
          <>
            <MainChat
              specialist={detail}
              handle={chat}
              onOpenSettings={() => setSettings("learned")}
              onRunning={setRunningHere}
              headExtra={
                <button
                  type="button"
                  className="spx-pill spx-side-toggle"
                  aria-expanded={sideOpen}
                  aria-controls={`spx-side-${detail.id}`}
                  onClick={() => setSideOpen((v) => !v)}
                >
                  Updates
                </button>
              }
            />
            <SidePanel
              specialistId={detail.id}
              sendToChat={sendToChat}
              onOpenSettings={(tab) => {
                setSideOpen(false);
                setSettings(tab);
              }}
              refreshKey={panelKey}
              onClose={() => setSideOpen(false)}
            />
          </>
        ) : (
          <main className="spx-main spx-empty">
            {detailError ? (
              <div className="spx-error" role="alert">
                {detailError}
              </div>
            ) : selected ? (
              <div className="spx-muted">Loading…</div>
            ) : !loading && !rows.length ? (
              <div className="spx-empty-copy">
                <h1>Specialists</h1>
                <p className="spx-muted">
                  A specialist knows one subject in depth and has its own memory and skills. Chat with it here; your tasks can
                  borrow its guidance or hand it a problem.
                </p>
                <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>
                  New specialist
                </button>
              </div>
            ) : null}
          </main>
        )}
      </div>
      {generalOpen ? (
        <GeneralInbox
          notes={general}
          specialists={rows}
          onClose={() => setGeneralOpen(false)}
          onChanged={loadList}
        />
      ) : null}
      {creating ? (
        <CreateSpecialist
          onClose={() => setCreating(false)}
          onCreated={(s) => {
            setCreating(false);
            loadList();
            void navigateUrl(specialistHref(s.id));
          }}
        />
      ) : null}
      {settings && detail ? (
        <SpecialistSettings
          specialist={detail}
          tab={settings}
          onTab={setSettings}
          onClose={() => {
            setSettings(null);
            // Run now, Revert, a draft sent or discarded, a memory edit: the panel shows the same
            // facts, so read it again now rather than on its next 30 s poll.
            setPanelKey((n) => n + 1);
          }}
          sendToChat={sendToChat}
          onSaved={(s) => {
            setDetail(s);
            loadList();
          }}
          onDeleted={() => {
            const gone = detail.id;
            setRows((rs) => rs.filter((r) => r.id !== gone));
            setSettings(null);
            setDetail(null);
            loadList();
            void navigateUrl(specialistsHref(), { replace: true });
          }}
        />
      ) : null}
    </div>
  );
}
