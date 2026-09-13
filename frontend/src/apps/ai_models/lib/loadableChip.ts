import type { HubModel } from "@platform/lib/api";

/** Item 2 (scope-corrected) / item 3 (D1287/D1288): the chip text for a row
 *  that NO runner available for its capability can open — "Won't run here ·
 *  <reason>", where `reason` is the server's own short clause
 *  (`HubModel.loadableReason`). Since D1288 the reason usually NAMES the
 *  architecture that would load it — "needs Diffusers (MiniMaxH3Pipeline)",
 *  "needs Diffusers", "needs Sentence Transformers — not supported yet",
 *  "no engine loads <name> yet" — falling back to "no engine here loads
 *  this" when nothing about the repo was recognised at all, or (unchanged)
 *  to mlx-vlm's own "<model_type> not supported by mlx-vlm" when that is the
 *  sole available runner and the one refusing. Split into its own testable
 *  function rather than inlined JSX so the exact wording has one place to
 *  check, the same reason `formatToken`/`downloadedVariantLabel` live in
 *  this `lib/` directory rather than the screen component.
 *
 *  Null whenever there is no chip to show — `loadable` is anything but
 *  `false` (`true`, or absent on an older server's response, both read as
 *  "nothing to flag"). A `loadable: false` row with no reason (should not
 *  happen — `hub_loadable.admission` always pairs the two — but a response
 *  is still just JSON) falls back to a bare "Won't run here" rather than
 *  silently showing nothing for a row the server did flag. */
export function loadableChipText(
  model: Pick<HubModel, "loadable" | "loadableReason">,
): string | null {
  if (model.loadable !== false) return null;
  return model.loadableReason ? `Won't run here · ${model.loadableReason}` : "Won't run here";
}
