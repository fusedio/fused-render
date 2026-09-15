// Writing an app's `icon.svg` from an icon-picker pick — the one place the
// pick → disk rule lives, because two surfaces now offer it: the sidebar's
// Projects row (its glyph is the picker's toggle) and the app page's header
// mark. The READ side needs no sharing; every surface already draws the file
// through `appIconUrl`.
import type { IconPick } from "@platform/ui/IconPicker";
import { removeAppIcon, setAppIcon } from "@platform/lib/api";
import { iconPlateRect, iconStyleBlock } from "@platform/lib/icon-color";
import { announceCurrentAppsChanged } from "@platform/lib/tasksChanged";

/** The picked emoji as a standalone icon.svg document — square viewBox, no
 *  fixed size, on the same rounded plate a lucide pick gets (white on light,
 *  black on dark; icon-color.ts). `data-fused-color="default"` is there so
 *  the shell's theme swap (themeIconSvg) resolves the plate; the emoji has no
 *  currentColor strokes, so the glyph itself is untouched. */
export function emojiIconSvg(emoji: string): string {
  const safe = emoji
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  return (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" data-fused-color="default">' +
    iconStyleBlock("default") +
    iconPlateRect(64) +
    '<text x="32" y="32" text-anchor="middle" dominant-baseline="central" ' +
    `font-size="44">${safe}</text></svg>`
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
