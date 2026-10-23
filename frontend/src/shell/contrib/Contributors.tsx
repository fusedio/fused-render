// "Contributors: alice, bob" — one quiet line on the Overview (handoff A4:
// "Bob's name appears as a contributor on the app's page"). Read from the
// app's own history by the server (§10), most commits first. Shown only when
// there is more than one person: an app with one author has no one else to
// credit, and the line would just repeat the author's name.
import type { AppContrib } from "@platform/lib/contrib";

export function Contributors({ contrib }: { contrib: AppContrib | null }) {
  const people = contrib?.flags.loop ? contrib.contributors : null;
  if (!people || people.length < 2) return null;
  const names = people.map((p) => p.login || p.name);
  return (
    <p className="contrib-contributors">
      Contributors:{" "}
      {names.map((n, i) => (
        <span key={n + i}>
          {i > 0 && ", "}
          {people[i].login ? (
            <a href={`https://github.com/${people[i].login}`} target="_blank" rel="noopener noreferrer">
              {n}
            </a>
          ) : (
            n
          )}
        </span>
      ))}
    </p>
  );
}
