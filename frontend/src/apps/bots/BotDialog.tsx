// New / edit bot. One form for both; delete (edit only) sits behind an
// AlertDialog confirm because it rmtree's the bot folder (memory, task log).
import { useEffect, useState } from "react";
import type { DefaultModel, SessionEffort } from "@platform/lib/api";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@platform/shadcn/ui/alert-dialog";
import { Button } from "@platform/shadcn/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@platform/shadcn/ui/dialog";
import { Input } from "@platform/shadcn/ui/input";
import { Textarea } from "@platform/shadcn/ui/textarea";
import type { Bot, BotInput } from "./api";
import { BotAvatar, botAccent } from "./BotAvatar";
import { BLANK_BOT, COLORS, EFFORTS, EMOJIS, MODELS } from "./lib";

export function BotDialog({
  open,
  bot,
  seed,
  onClose,
  onSave,
  onDelete,
}: {
  open: boolean;
  /** The bot being edited; null = create. */
  bot: Bot | null;
  /** Create-mode prefill (a sample persona chip). */
  seed?: BotInput | null;
  onClose: () => void;
  onSave: (input: BotInput) => Promise<void>;
  onDelete?: () => Promise<void>;
}) {
  const [form, setForm] = useState<BotInput>(BLANK_BOT);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);

  // Re-seed on every open: the dialog stays mounted between uses.
  useEffect(() => {
    if (!open) return;
    setError(null);
    setBusy(false);
    setForm(
      bot
        ? {
            name: bot.name,
            persona: bot.persona,
            emoji: bot.emoji,
            color: bot.color,
            model: bot.model,
            effort: bot.effort,
          }
        : { ...BLANK_BOT, ...(seed ?? {}) },
    );
  }, [open, bot, seed]);

  const set = (patch: Partial<BotInput>) => setForm((f) => ({ ...f, ...patch }));
  const customColor = !COLORS.includes(form.color);

  const submit = async () => {
    if (!form.name.trim()) {
      setError("Give the bot a name.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await onSave({ ...form, name: form.name.trim(), persona: form.persona.trim() });
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!onDelete) return;
    setBusy(true);
    try {
      await onDelete();
      setConfirmDelete(false);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
      setConfirmDelete(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(o) => !o && !busy && onClose()}>
      <DialogContent className="bots-dialog sm:max-w-[560px]">
        <DialogHeader>
          <DialogTitle className="bots-dialog-title">
            <BotAvatar emoji={form.emoji} color={form.color} />
            {bot ? `Edit ${bot.name}` : "New bot"}
          </DialogTitle>
        </DialogHeader>
        <form
          className="bots-form"
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <label className="bots-field">
            <span className="bots-field-label">Name</span>
            <Input
              value={form.name}
              onChange={(e) => set({ name: e.target.value })}
              placeholder="Ada"
              autoFocus
              maxLength={80}
            />
          </label>
          <div className="bots-field">
            <span className="bots-field-label">Avatar</span>
            <div className="bots-emoji-grid" role="radiogroup" aria-label="Emoji">
              {EMOJIS.map((em) => (
                <button
                  key={em}
                  type="button"
                  role="radio"
                  aria-checked={form.emoji === em}
                  className={`bots-emoji${form.emoji === em ? " is-on" : ""}`}
                  onClick={() => set({ emoji: em })}
                >
                  {em}
                </button>
              ))}
              <Input
                className="bots-emoji-input"
                value={EMOJIS.includes(form.emoji) ? "" : form.emoji}
                onChange={(e) => set({ emoji: e.target.value.trim().slice(0, 8) || EMOJIS[0] })}
                placeholder="Other"
                aria-label="Custom emoji"
              />
            </div>
          </div>
          <div className="bots-field">
            <span className="bots-field-label">Colour</span>
            <div className="bots-swatches" role="radiogroup" aria-label="Colour">
              {COLORS.map((c) => (
                <button
                  key={c}
                  type="button"
                  role="radio"
                  aria-checked={form.color === c}
                  aria-label={c}
                  className={`bots-swatch${form.color === c ? " is-on" : ""}`}
                  style={botAccent(c)}
                  onClick={() => set({ color: c })}
                />
              ))}
              <label
                className={`bots-swatch bots-swatch--custom${customColor ? " is-on" : ""}`}
                style={botAccent(form.color)}
                title="Custom colour"
              >
                <input
                  type="color"
                  value={/^#[0-9a-fA-F]{6}$/.test(form.color) ? form.color : COLORS[0]}
                  onChange={(e) => set({ color: e.target.value })}
                  aria-label="Custom colour"
                />
              </label>
            </div>
          </div>
          <label className="bots-field">
            <span className="bots-field-label">Persona</span>
            <Textarea
              className="bots-persona-input"
              value={form.persona}
              onChange={(e) => set({ persona: e.target.value })}
              placeholder="You are Ada, a warm and precise research partner who…"
              rows={7}
            />
          </label>
          <div className="bots-field-row">
            <label className="bots-field">
              <span className="bots-field-label">Model</span>
              <select
                className="bots-select"
                value={form.model}
                onChange={(e) => set({ model: e.target.value as DefaultModel })}
              >
                {MODELS.map((m) => (
                  <option key={m.value} value={m.value}>
                    {m.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="bots-field">
              <span className="bots-field-label">Effort</span>
              <select
                className="bots-select"
                value={form.effort}
                onChange={(e) => set({ effort: e.target.value as SessionEffort })}
              >
                {EFFORTS.map((m) => (
                  <option key={m.value} value={m.value}>
                    {m.label}
                  </option>
                ))}
              </select>
            </label>
          </div>
          {error && <p className="bots-error">{error}</p>}
          <DialogFooter className="bots-dialog-footer">
            {bot && onDelete && (
              <Button
                type="button"
                variant="destructive"
                className="bots-delete"
                disabled={busy}
                onClick={() => setConfirmDelete(true)}
              >
                Delete bot
              </Button>
            )}
            <Button type="button" variant="outline" disabled={busy} onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={busy}>
              {bot ? "Save" : "Create bot"}
            </Button>
          </DialogFooter>
        </form>
        <AlertDialog open={confirmDelete} onOpenChange={(o) => !busy && setConfirmDelete(o)}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Delete {bot?.name}?</AlertDialogTitle>
              <AlertDialogDescription>
                This removes the bot, its memory and its task log. Apps it built stay where
                they are.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
              <AlertDialogAction variant="destructive" disabled={busy} onClick={() => void remove()}>
                Delete
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </DialogContent>
    </Dialog>
  );
}
