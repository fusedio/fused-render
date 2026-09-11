// A system chip: the harness woke the run because a background shell it had
// started finished or was stopped (agent.py's `notice` segment, D415, T:15622).
//
// It is the ONE line that accounts for a reply nobody asked for — without it the
// turn underneath reads as the agent answering a message that is not there — and
// it is deliberately not a bubble: the user did not say it.
//
// Plain text, never renderMd: the summary is a sentence the CLI wrote ABOUT a
// command the user ran, and markdown would let a stray backtick or underscore in
// a command line restyle the chip.
export function NoticeView({ text }: { text: string }) {
  return (
    <div className="seg-notice">
      <span className="seg-notice-glyph" aria-hidden="true">
        ⏵
      </span>
      <span>{text}</span>
    </div>
  );
}

export default NoticeView;
