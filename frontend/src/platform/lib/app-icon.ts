// Writing an app's `icon.svg` from an icon-picker pick — the one place the
// pick → disk rule lives, because two surfaces now offer it: the sidebar's
// Projects row (its glyph is the picker's toggle) and the app page's header
// mark. The READ side needs no sharing; every surface already draws the file
// through `appIconUrl`.
import type { IconPick } from "@platform/ui/IconPicker";
import { removeAppIcon, setAppIcon } from "@platform/lib/api";
import { announceCurrentAppsChanged } from "@platform/lib/tasksChanged";

/** The picked emoji as a standalone icon.svg document — square viewBox, no
 *  fixed size, transparent ground (a colour emoji carries its own colours, so
 *  it reads on both themes; see skills/fused-render-app-icon). The same file
 *  a hand-authored icon.svg would be, just generated. */
export function emojiIconSvg(emoji: string): string {
  const safe = emoji
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  return (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">' +
    '<text x="32" y="32" text-anchor="middle" dominant-baseline="central" ' +
    `font-size="52">${safe}</text></svg>`
  );
}

/** Apply an icon picker's answer to the app folder at `path`: `null` removes
 *  the file (back to the generic mark), an icon pick is stored as the finished
 *  grey-glyph svg it arrives as, and an emoji gets the standalone wrapper above.
 *
 *  Announces the desk change on success, and that is the reason this is shared
 *  rather than two call sites: the sidebar's Projects row for this app is on
 *  screen while the app page's own mark is picked, and it only refetches on
 *  that event — without it the row keeps yesterday's glyph until something
 *  else pokes it. Throws whatever the write threw; the caller reports it (a
 *  failed write leaves the old icon, and a refetch shows the truth). */
export async function applyIconPick(
  path: string,
  pick: IconPick | null,
): Promise<void> {
  if (pick === null) await removeAppIcon(path);
  else if (pick.kind === "icon") await setAppIcon(path, pick.svg);
  else await setAppIcon(path, emojiIconSvg(pick.emoji));
  announceCurrentAppsChanged();
}
