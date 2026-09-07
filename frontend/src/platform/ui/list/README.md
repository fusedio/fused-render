# List kit

Leaf controls for list-shaped pages (Tasks today). Rows, lanes, cards, scrollers
and drag targets are NOT here on purpose: they keep their DOM and their CSS
(already on the design scale) — hover, `:has()`, scroll and drag behaviour is
functionality, and a kit that re-implements it in React is how the first Tasks
attempt broke. Only elements that render one control with the same handlers
and attributes get a component.

| Old | Component | Notes |
|---|---|---|
| `.schedule-form-seg` (view switch only) | `SegmentedControl` | the calendar's and the New-task form's segs stay on CSS |
| `.btn.btn-secondary.schedule-view-btn` | `SegmentButton` | `schedule-view-btn` + `is-active` stay as the glyph-colour hook |
| `.schedule-tv-filter-count` | `FilterCount` | shadcn Badge |
| `.btn.btn-primary.schedule-new` | shadcn `Button` | in Scheduled.tsx |
| `.tasks-outcome-pill` | shadcn `Badge variant="outline"` | class kept; the only pill on the page |

Deliberately left on CSS: `.schedule-tv-filter-btn` / `.is-split` / `.schedule-tv-filter-x`
(a coupled split control whose group-hover paints both halves), folder chip / id
tag (row hit-zone math), everything inside rows, lanes and cards.
