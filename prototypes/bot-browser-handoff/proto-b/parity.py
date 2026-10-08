import sys, os, json, subprocess, itertools; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from keys import keyparams, T, NAMED
D = os.path.dirname(os.path.abspath(__file__))
vec = []
keys = [(v[1], c) for c, v in T.items()] + [(v[2], c) for c, v in T.items()] + [(k, k) for k in NAMED] + [("é", "KeyE"), ("日", ""), ("😀", "")]
for (k, c), sh, ct, al, me, mac in itertools.product(keys, [0, 1], [0, 1], [0, 1], [0, 1], [True, False]):
    vec.append([k, c, dict(shift=bool(sh), ctrl=bool(ct), alt=bool(al), meta=bool(me), mac=mac)])
py = [keyparams(k, c, **o) for k, c, o in vec]
js = json.loads(subprocess.run(["node", "-e", f"const {{keyparams}}=require('{D}/keys.js');const v=JSON.parse(require('fs').readFileSync(0));console.log(JSON.stringify(v.map(([k,c,o])=>keyparams(k,c,o))))"],
                               input=json.dumps(vec), capture_output=True, text=True).stdout)
bad = [(vec[i], py[i], js[i]) for i in range(len(vec)) if py[i] != js[i]]
print("vectors", len(vec), "mismatch", len(bad)); print(bad[:2])
