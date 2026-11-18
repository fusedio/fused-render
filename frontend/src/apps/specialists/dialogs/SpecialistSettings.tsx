// SETTINGS: one dialog with tabs (PRODUCT §8; mock-ups Learned.dc.html and
// Memory.dc.html) — Learned, Memory, Skills, People, Connections, General.
import { useRef, type KeyboardEvent } from "react";
import { Modal } from "@platform/ui/modal/Modal";
import type { SpecialistDetail } from "@platform/lib/specialists-api";
import { Face } from "../components/Face";
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

export function SpecialistSettings({
  specialist,
  tab,
  onTab,
  onClose,
  sendToChat,
  onSaved,
  onDeleted,
}: {
  specialist: SpecialistDetail;
  tab: SettingsTab;
  onTab: (t: SettingsTab) => void;
  onClose: () => void;
  /** Send into the main chat (and close this dialog so the reader sees it). */
  sendToChat: (text: string) => boolean;
  onSaved: (s: SpecialistDetail) => void;
  onDeleted: () => void;
}) {
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const i = TABS.findIndex((t) => t.id === tab);
    const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    e.preventDefault();
    const next = TABS[(i + step + TABS.length) % TABS.length];
    onTab(next.id);
    tabRefs.current[TABS.indexOf(next)]?.focus();
  };
  const send = (text: string) => {
    const ok = sendToChat(text);
    if (ok) onClose();
    return ok;
  };
  const panelId = `spx-settings-${specialist.id}-panel`;

  return (
    <Modal
      title={`${specialist.name} specialist`}
      titleLead={<Face id={specialist.id} name={specialist.name} face={specialist.face} size={28} />}
      onClose={onClose}
      width={900}
      dialogClassName="spx-settings"
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
          </button>
        ))}
      </div>
      <div role="tabpanel" id={panelId} aria-labelledby={`${panelId}-${tab}`} className="spx-tabpanel">
        {tab === "learned" ? <LearnedTab specialistId={specialist.id} sendToChat={send} /> : null}
        {tab === "memory" ? <MemoryTab specialistId={specialist.id} sendToChat={send} /> : null}
        {tab === "skills" ? <SkillsTab specialistId={specialist.id} /> : null}
        {tab === "people" ? <PeopleTab specialistId={specialist.id} /> : null}
        {tab === "connections" ? <ConnectionsTab specialistId={specialist.id} /> : null}
        {tab === "general" ? <GeneralTab specialist={specialist} onSaved={onSaved} onDeleted={onDeleted} /> : null}
      </div>
    </Modal>
  );
}
