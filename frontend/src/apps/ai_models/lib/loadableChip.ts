import type { HubModel } from "@platform/lib/api";

/** Item 2 (scope-corrected): the chip text for a row the runner ACTIVE for
 *  its capability cannot open — "Won't run here · <reason>", where `reason`
 *  is the server's own short clause (`HubModel.loadableReason`, e.g. "mflux
 *  only loads FLUX.2 Klein"). Split into its own testable function rather
 *  than inlined JSX so the exact wording has one place to check, the same
 *  reason `formatToken`/`downloadedVariantLabel` live in this `lib/`
 *  directory rather than the screen component.
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
