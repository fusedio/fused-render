// The sidebar's setup meter: a row above Settings that says how far first-run
// setup has got ("Setup · 60%") and takes the reader back into the wizard, and
// the ring the collapsed rail shows in its place. Reads progress.ts; draws.
//
// Shown only while there is something to show (progress.meterVisible): after
// the wizard has written at least one stage, and until the meter reads 100.
// An install upgrading into this build never sees it, and a finished setup
// gives the row back to the Settings block — the wizard stays one click away
// under Help › Setup wizard.
import { navigateUrl } from "@platform/lib/router";
import type { Config } from "@platform/lib/api";

import { ONBOARDING_PATH } from "./state";
import { meterVisible, progressPercent, seedProgress, useOnboardingState } from "./progress";

export interface SetupMeter {
  percent: number;
  /** "3 of 5 done" — the tooltip's sentence. */
  title: string;
}

/** The meter for the sidebar, or null when the row should not exist. */
export function useSetupMeter(config: Config): SetupMeter | null {
  seedProgress(config);
  const state = useOnboardingState();
  if (!meterVisible(state)) return null;
  const p = progressPercent(state?.stages);
  const half = p.partial > 0 ? `, ${p.partial} partly` : "";
  return {
    percent: p.percent,
    title: `Setup ${p.percent}% — ${p.complete} of ${p.counted} steps done${half}. Open the setup wizard.`,
  };
}

/** A ring that fills clockwise with `percent`. 16px, like every rail glyph. */
export function SetupProgressRing({ percent }: { percent: number }) {
  const r = 6.5;
  const c = 2 * Math.PI * r;
  const dash = (Math.max(0, Math.min(100, percent)) / 100) * c;
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true" className="setup-ring">
      <circle cx="8" cy="8" r={r} stroke="currentColor" strokeWidth="2" opacity="0.22" />
      <circle
        cx="8"
        cy="8"
        r={r}
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeDasharray={`${dash} ${c - dash}`}
        transform="rotate(-90 8 8)"
        className="setup-ring-fill"
      />
    </svg>
  );
}

/** The expanded-sidebar row: ring, "Setup", a thin bar and the number. */
export function SetupProgressRow({ meter }: { meter: SetupMeter }) {
  return (
    <a
      href={ONBOARDING_PATH}
      id="setup-progress-link"
      className="sidebar-item setup-progress"
      title={meter.title}
      onClick={(e) => {
        e.preventDefault();
        navigateUrl(ONBOARDING_PATH);
      }}
    >
      <span className="icon">
        <SetupProgressRing percent={meter.percent} />
      </span>{" "}
      Setup
      <span className="sidebar-item-trail">
        <span className="setup-progress-bar" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={meter.percent}>
          <span className="setup-progress-fill" style={{ width: `${meter.percent}%` }} />
        </span>
        <span className="setup-progress-pct">{meter.percent}%</span>
      </span>
    </a>
  );
}
