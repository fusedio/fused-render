// New skill — placeholder until the Draft with Claude flow lands.
export function NewSkill({ initialDesc, onClose }: { initialDesc: string; onClose: () => void; onSaved?: (key: string) => void }) {
  return (
    <div className="cc-new">
      <p>{initialDesc}</p>
      <button type="button" onClick={onClose}>
        Cancel
      </button>
    </div>
  );
}
