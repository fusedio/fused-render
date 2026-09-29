// The two empty states: no bots at all (create-your-first hero with sample
// persona chips), and a bot with no conversation open (big avatar, persona
// blurb, suggested openers).
import { Plus } from "lucide-react";
import { Button } from "@platform/shadcn/ui/button";
import type { Bot, BotInput } from "./api";
import { BotAvatar } from "./BotAvatar";
import { OPENERS, SAMPLE_BOTS } from "./lib";

export function FirstBotHero({
  onCreate,
}: {
  onCreate: (seed: BotInput | null) => void;
}) {
  return (
    <div className="bots-first">
      <div className="bots-first-inner">
        <BotAvatar emoji="🤖" color="" size="xl" />
        <h2 className="bots-first-title">Create your first bot</h2>
        <p className="bots-first-sub">
          A bot is a chat companion with its own persona and memory. It can't write code
          itself, but it can plan an app with you and hand a detailed spec to a builder agent.
        </p>
        <div className="bots-chips">
          {SAMPLE_BOTS.map((s) => (
            <button key={s.name} type="button" className="bots-chip" onClick={() => onCreate(s)}>
              <span aria-hidden="true">{s.emoji}</span> {s.name}
            </button>
          ))}
        </div>
        <Button onClick={() => onCreate(null)}>
          <Plus /> New bot
        </Button>
      </div>
    </div>
  );
}

/** `onOpener` absent = the chat cannot take a prefilled ask (legacy frame):
 *  the openers still show, as static hints. */
export function BotHero({ bot, onOpener }: { bot: Bot; onOpener?: (text: string) => void }) {
  return (
    <div className="bots-hero">
      <BotAvatar emoji={bot.emoji} color={bot.color} size="xl" />
      <h2 className="bots-hero-name">{bot.name}</h2>
      {bot.persona && <p className="bots-hero-persona">{bot.persona}</p>}
      <div className="bots-chips">
        {OPENERS.map((o) =>
          onOpener ? (
            <button key={o} type="button" className="bots-chip" onClick={() => onOpener(o)}>
              {o}
            </button>
          ) : (
            <span key={o} className="bots-chip bots-chip--static">
              {o}
            </span>
          ),
        )}
      </div>
    </div>
  );
}
