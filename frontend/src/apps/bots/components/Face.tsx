// The avatar face (OpenBot core.js face(b, id)) as SVG. Keyed by fkey(b): a change of shape, colour or mood remounts
// it, exactly as OpenBot replaced the markup, so the animator (faceAnim.ts) restarts on a fresh node. Within one key
// React never rewrites the nodes, so anime.js transforms survive re-renders.
// svgId: "hface" (chat header), "lface" (the selected bot's list row), "pface" (the face-picker preview).
// A brand face (preset bots, Super Bot) is a disc with the mark and no eyes: the body still bounces, snores and spins
// with the mood, and the mark (`.mark`, inside the positioning group so anime's CSS transform never overrides the
// SVG one) breathes, glances and leans toward the pointer in place of the eye routines (faceAnim.ts).
import { BRANDS, FACE_SHAPES, MOODS, RIBBONS, SPARKS, faceOf, fkey, moodOf, type FaceSubject } from "../lib/face";

export function Face({ b, svgId }: { b: FaceSubject; svgId?: string }) {
  const { shape, color, icon } = faceOf(b), m = MOODS[moodOf(b)], [ty, rot, sx, sy] = m.eye, k = fkey(b);
  return (
    <svg key={k} viewBox="-1.25 -1.25 102.5 102.5" data-k={k} id={svgId || undefined}>
      <g className="face" style={{ transform: `translateY(${m.face[0]}px) rotate(${m.face[1]}deg)` }}>
        {icon ? (
          <>
            <circle className="body" cx="50" cy="50" r="38" fill={color} />
            <g transform="translate(26 26) scale(2)" fill="#fff"><g className="mark" dangerouslySetInnerHTML={{ __html: BRANDS[icon].glyph(color) }} /></g>
          </>
        ) : (
          <>
            <path className="body" d={FACE_SHAPES[shape]} fill={color} />
            <ellipse cx="37" cy="31" rx="9" ry="4.5" fill="#fff" opacity=".28" transform="rotate(-28 37 31)" />
            {[41, 54].map((x, i) => (
              <g key={x} className="eyeg" style={{ transform: `translateY(${ty}px) rotate(${rot * (i ? 1 : -1)}deg) scaleX(${sx}) scaleY(${sy})` }}>
                <rect className="eye" x={x} y="43" width="5" height="12" rx="2.5" />
              </g>
            ))}
          </>
        )}
        <ellipse className="tear" cx="42" cy="57" rx="1.8" ry="2.6" fill="#7fd0ff" opacity="0" />
      </g>
      <g className="ribbons" fill="none" strokeWidth="5" strokeLinecap="round" opacity="0">
        {RIBBONS.map((r) => <path key={r.stroke} stroke={r.stroke} d={r.d} />)}
      </g>
      <g className="sparks" fill="#fff">
        {Array.from({ length: SPARKS.count }, (_, i) => <g key={i}><path d={SPARKS.d} opacity="0" /></g>)}
      </g>
    </svg>
  );
}
