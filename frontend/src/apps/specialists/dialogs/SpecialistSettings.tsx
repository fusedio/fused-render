// SETTINGS: one dialog with tabs (PRODUCT §8; mock-ups Learned.dc.html and
// Memory.dc.html) — Learned, Memory, Skills, People, Connections, General.
// Underlined tabs ("Learned · N new"), Done in the head, one fixed height so
// switching tabs never makes the dialog jump, and no wider than the window.
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { Modal } from "@platform/ui/modal/Modal";
import { getPanel, type SpecialistDetail } from "@platform/lib/specialists-api";
import { Face, initialsFor } from "../components/Face";
import { ConnectionsTab } from "./ConnectionsTab";
import { GeneralTab } from "./GeneralTab";
import { LearnedTab } from "./LearnedTab";
import { MemoryTab } from "./MemoryTab";
import { PeopleTab } from "./PeopleTab";
import { SkillsTab } from "./SkillsTab";

export const TABS = [
  { id: "learned", label: "Learned" },
  { id: "memory", label: "Memory" },
  { id: "skills", label: "Skills" },
  { id: "people", label: "People" },
  { id: "connections", label: "Connections" },
  { id: "general", label: "General" },
] as const;
export type SettingsTab = (typeof TABS)[number]["id"];

/** The dialog's width: the mock's 900, never wider than the window less 16px a side. */
export const SETTINGS_WIDTH = "min(900px, calc(100vw - 32px))";

export function SpecialistSettings({
  specialist,
  tab,
  onTab,
  onClose,
  sendToChat,
  onSaved,
  onDeleted,
  learnedCount,
}: {
  specialist: SpecialistDetail;
  tab: SettingsTab;
  onTab: (t: SettingsTab) => void;
  onClose: () => void;
  /** Send into the main chat. */
  sendToChat: (text: string) => boolean;
  onSaved: (s: SpecialistDetail) => void;
  onDeleted: () => void;
  /** "Learned · N new": the latest update's change count (default: read from the panel). */
  learnedCount?: number;
}) {
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const [fetched, setFetched] = useState<number | null>(null);
  useEffect(() => {
    if (learnedCount !== undefined) return;
    let live = true;
    getPanel(specialist.id).then(
      (p) => live && setFetched(p.learned?.total ?? 0),
      () => {},
    );
    return () => {
      live = false;
    };
  }, [specialist.id, learnedCount]);
  const newCount = learnedCount ?? fetched ?? 0;

  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const i = TABS.findIndex((t) => t.id === tab);
    const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    const to = e.key === "Home" ? 0 : e.key === "End" ? TABS.length - 1 : step ? (i + step + TABS.length) % TABS.length : -1;
    if (to < 0) return;
    e.preventDefault();
    onTab(TABS[to].id);
    tabRefs.current[to]?.focus();
  };
  // Review and send / Ask to change: the answer is in the chat, so the dialog
  // gets out of the way. Revert stays here and says "Revert asked in the chat".
  const sendAndClose = (text: string) => {
    const ok = sendToChat(text);
    if (ok) onClose();
    return ok;
  };
  const panelId = `spx-settings-${specialist.id}-panel`;

  return (
    <Modal
      title={`${specialist.name} specialist`}
      titleLead={
        <span className="spx-settings-face">
          <Face id={specialist.id} name={specialist.name} face={specialist.face} size={28} letters={initialsFor(specialist.name)} />
        </span>
      }
      headActions={
        <button type="button" className="btn btn-secondary modal-head-act spx-done" onClick={onClose}>
          Done
        </button>
      }
      hideClose
      onClose={onClose}
      width={SETTINGS_WIDTH}
      dialogClassName="spx-dialog spx-settings"
    >
      <div className="spx-tabs" role="tablist" aria-label="Specialist settings" onKeyDown={onKey}>
        {TABS.map((t, i) => (
          <button
            key={t.id}
            ref={(el) => {
              tabRefs.current[i] = el;
            }}
            type="button"
            role="tab"
            id={`${panelId}-${t.id}`}
            aria-selected={t.id === tab}
            aria-controls={panelId}
            tabIndex={t.id === tab ? 0 : -1}
            className={"spx-tabbtn" + (t.id === tab ? " is-active" : "")}
            onClick={() => onTab(t.id)}
          >
            {t.label}
            {t.id === "learned" && newCount > 0 ? <span className="spx-tab-count"> · {newCount} new</span> : null}
          </button>
        ))}
      </div>
      <div role="tabpanel" id={panelId} aria-labelledby={`${panelId}-${tab}`} className="spx-tabpanel" tabIndex={-1}>
        {tab === "learned" ? (
          <LearnedTab specialistId={specialist.id} sendToChat={sendToChat} sendAndClose={sendAndClose} />
        ) : null}
        {tab === "memory" ? <MemoryTab specialistId={specialist.id} sendToChat={sendAndClose} /> : null}
        {tab === "skills" ? <SkillsTab specialistId={specialist.id} /> : null}
        {tab === "people" ? <PeopleTab specialistId={specialist.id} /> : null}
        {tab === "connections" ? <ConnectionsTab specialistId={specialist.id} /> : null}
        {tab === "general" ? <GeneralTab specialist={specialist} onSaved={onSaved} onDeleted={onDeleted} /> : null}
      </div>
    </Modal>
  );
}
