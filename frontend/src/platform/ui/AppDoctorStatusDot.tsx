// The small status dot beside the "App Doctor" button on both surfaces
// (shell/AppPage.tsx header, apps/explorer/Preview.tsx topbar): coloured by
// the worst FAILING severity in the report (appdoctor-lib.ts's
// `worstSeverity` — the one reduction, not reimplemented here), muted/neutral
// while unknown or clean. Colour is never the only carrier: `title` and
// `aria-label` name the state in words (`severityDotLabel`), so a
// colour-blind reader and a screen reader both get the same answer a sighted
// reader gets from the colour alone.
import { severityDotLabel, worstSeverity } from "./appdoctor-lib";
import type { AppCheck } from "@platform/lib/api";

export function AppDoctorStatusDot({ checks }: { checks: AppCheck[] | null }) {
  const worst = checks ? worstSeverity(checks) : null;
  const label = severityDotLabel(checks);
  return (
    <span
      className={"appdoc-dot " + (worst ? "appdoc-dot-" + worst : "appdoc-dot-neutral")}
      role="img"
      aria-label={label}
      title={label}
    />
  );
}

export default AppDoctorStatusDot;
