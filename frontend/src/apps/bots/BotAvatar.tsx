// The bot's round avatar: its emoji on its own colour. The colour arrives as
// the inline `--bot-accent` var (bot.json's value), which is the one colour on
// this page that is not a palette token — styles/bots.css paints through it.
import type { CSSProperties } from "react";

export function botAccent(color: string | undefined): CSSProperties {
  return { "--bot-accent": color || "var(--accent)" } as CSSProperties;
}

export function BotAvatar({
  emoji,
  color,
  size = "md",
}: {
  emoji: string;
  color: string;
  size?: "sm" | "md" | "lg" | "xl";
}) {
  return (
    <span className={`bots-avatar bots-avatar--${size}`} style={botAccent(color)} aria-hidden="true">
      {emoji || "🤖"}
    </span>
  );
}
