import { clsx, type ClassValue } from "clsx"
import { extendTailwindMerge } from "tailwind-merge"

// The design scale (styles/scale.css) adds named text-size and radius
// utilities on top of Tailwind's own (`text-dense`, `rounded-panel`, …).
// Vanilla tailwind-merge has never heard of them, so when a composite
// overrides a shadcn primitive's own Tailwind-native class in the SAME
// conflict group (Button's `text-sm`/`rounded-lg`, Empty's `rounded-xl`,
// Skeleton's `rounded-md`, Badge's `text-xs`, …) merge left both classes in
// the string and the winner came down to generated-CSS order, not to which
// one a caller wrote last — silently reverting the override. Teaching merge
// these class names restores "last one wins" for them, the same guarantee
// every other Tailwind utility already gets.
const twMerge = extendTailwindMerge({
  extend: {
    classGroups: {
      "font-size": [
        "text-micro",
        "text-caption",
        "text-meta",
        "text-dense",
        "text-body",
        "text-control",
        "text-title",
        "text-heading",
        "text-display",
      ],
      rounded: ["rounded-hairline", "rounded-control", "rounded-card", "rounded-panel", "rounded-pill"],
    },
  },
})

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}
