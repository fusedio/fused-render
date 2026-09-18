// ONE SPELLING OF FABLE, for every picker on this side of the wire.
//
// The menus used to offer two entries for one model: a pinned full id
// ("claude-fable-5-1") above the floating alias ("fable"). They are the same
// model, so the pair read as a choice that wasn't one (Akshil, 2026-09-18:
// "fable and fable 5-1 are the same. so let's only label it as fable"). The
// pinned entry is gone from every list; the alias is the whole of it.
//
// What the id OUTLIVES those lists in, though, is data: scheduled entries,
// `session_settings.json` records, `?model=` deep links and transcripts written
// before today all still say "claude-fable-5-1". Validated raw against a list
// that no longer holds it, every one of those reads as an unknown model — a
// blank pill in the composer (no `selectedOptions[0]`, so fitSelect returns
// early) or a raw id printed at the user in the New task card. So the id is
// UNDERSTOOD rather than enumerated: normalise first, validate after.
//
// Kept here, in platform, because both pickers need the same answer and they
// live in different halves of the app — the chat composer
// (apps/claude/ui/composer-defaults) and the New task card (shell/schedule-lib).
// The chat TEMPLATE (templates/claude/template.html) carries its own copy for
// the reason it carries its own MODELS: it is vanilla JS served off disk and
// cannot import this bundle.
// The id, then optionally the CLI's context qualifier (`[1m]`), which is a
// modifier on a model rather than a different one — so it is kept, and only
// the id in front of it is folded.
const FABLE_ID = /^claude-fable(?:[-.][^[\]]*)?(\[[^\]]*\])?$/i;

/** Any Fable spelling — the retired pinned id, a dated full id, either with a
 *  `[1m]` qualifier — said the one way the pickers offer it now, qualifier
 *  kept. Anything else is returned untouched, including "" (which every caller
 *  reads as "nothing chosen"). */
export function normalizeModel(value: string | null | undefined): string {
  const v = (value || "").trim();
  const m = FABLE_ID.exec(v);
  return m ? `fable${m[1] ?? ""}` : v;
}

// A trailing `[…]` qualifier, so a value can be tried against a list without it.
const QUALIFIER = /\[[^\]]*\]$/;

/** The one entry of `list` that `value` names, or "". Tried as written (after
 *  the Fable fold), then with its `[1m]` qualifier taken off — a picker that
 *  offers only `fable` still means a stored `claude-fable-5-1[1m]` by that row,
 *  which is where a reader expects to find their own setting. "" is "not on
 *  this list", and every caller has its own answer for that. */
export function listedModelIn(
  value: string | null | undefined,
  list: readonly string[],
): string {
  const v = normalizeModel(value);
  if (list.includes(v)) return v;
  const base = v.replace(QUALIFIER, "");
  return base !== v && list.includes(base) ? base : "";
}
