import { useEffect, useState } from "react";

// `fused` is the runtime fused-render injects into every page; its types come
// from fused_render/static/fused.d.ts (served at /static/fused.d.ts), which
// tsconfig.json includes.

type Primes = { limit: number; primes: number[] };

/** The URL is the state: `?limit=50` reproduces this view on refresh. */
function useParam(key: string, fallback: string): [string, (v: string) => void] {
  const read = () => fused.params.get(key) ?? fallback;
  const [value, setValue] = useState(read);
  useEffect(() => fused.params.onChange(() => setValue(read())), [key]);
  return [value, (v) => fused.params.set(key, v)];
}

export function App() {
  const [limit, setLimit] = useParam("limit", "30");
  const [data, setData] = useState<Primes | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // Params travel as strings; main.py's annotations coerce them. A new call
    // for the same .py supersedes the one in flight, so a dragged slider
    // never renders a stale answer.
    fused
      .runPython<Primes>("./main.py", { limit })
      .then((r) => { setData(r); setError(null); })
      .catch((e: Error) => { if (e.name !== "AbortError") setError(e.message); });
  }, [limit]);

  return (
    <main className="app">
      <h1>Primes up to {limit}</h1>
      <p className="muted">
        React renders, Python computes, the URL remembers. Running {fused.env}.
      </p>
      <label>
        Limit
        <input
          type="range"
          min="2"
          max="500"
          value={limit}
          onChange={(e) => setLimit(e.target.value)}
        />
        <output>{limit}</output>
      </label>
      {error && <pre className="error">{error}</pre>}
      <ul className="primes">
        {data?.primes.map((p) => <li key={p}>{p}</li>)}
      </ul>
    </main>
  );
}
