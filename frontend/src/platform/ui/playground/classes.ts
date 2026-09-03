// Class strings the composites share, and one reason they exist.
//
// Tailwind's preflight is OFF in this app (styles/tailwind.css says why), so a
// bare <button>, <input> or <textarea> still wears the UA's own
// `font: 400 13.333px Arial` — which is why nearly every hand-written control
// in ai-playground.css opens with `font: inherit`. The composites need the same
// reset, and they cannot spell it as the `font` SHORTHAND: a shorthand and a
// `text-[13px]` beside it are two declarations in one Tailwind layer, and which
// of them wins is a question about utility sort order rather than about the
// design. Longhands only, split so a control that sets its OWN size or leading
// takes just the part it needs and `text-*` / `leading-*` stays the one thing
// setting it.

// One ordering fact these constants are built around: Tailwind v4 emits an
// arbitrary PROPERTY (`[font-weight:inherit]`) immediately after the named
// utilities that set the same property, so the arbitrary one always wins
// whatever order the class list is written in — and tailwind-merge does not
// deduplicate the pair. So a control that wants its own weight (`font-semibold`
// on the filled button) must take a const that does NOT name weight.

/** Family and style — the parts nothing ever overrides. Deliberately NOT
 *  `font-variant`: it is a SHORTHAND, so `[font-variant:inherit]` resets
 *  `font-variant-numeric` to normal and (being an arbitrary property) wins over
 *  the `tabular-nums` a chip or a byte counter sets right beside it. The
 *  original `font: inherit` had the same reset, but it was a DECLARATION the
 *  `font-variant-numeric` after it overrode; in Tailwind the order is the
 *  compiler's, so the reset has to go. Nothing inherits a non-normal
 *  `font-variant` here, so it buys nothing anyway. */
export const INHERIT_FONT_FAMILY = "[font-family:inherit] [font-style:inherit]";

/** …plus the weight. Everything `font: inherit` does except the two metrics. */
export const INHERIT_FONT_FACE = `${INHERIT_FONT_FAMILY} [font-weight:inherit]`;

/** …plus the leading, for a control that sets its own `font-size` only. */
export const INHERIT_FONT = `${INHERIT_FONT_FACE} [line-height:inherit]`;

/** The whole of `font: inherit`, for a control that names no size of its own. */
export const INHERIT_FONT_ALL = `${INHERIT_FONT} [font-size:inherit]`;

/** A borderless, chrome-free button: the UA defaults every ghost control in
 *  this family has to undo before it can be styled. */
export const BARE_BUTTON = `cursor-pointer border-none bg-transparent p-0 ${INHERIT_FONT}`;
