"""Gate for the model_tokenizer template (SPEC §38).

One `isfile`, no reads: `tokenizer.json` is the modern fast-tokenizer format,
it is self-describing, and the `tokenizers` library loads it standalone — which
is exactly the set of folders this playground can serve. A model whose
tokenizer is the older `vocab.txt` + `merges.txt` pair is deliberately NOT
offered: loading those needs the model class that owns them, and offering a
playground that then cannot tokenise is worse than not offering it.

Bound to the universal `/` registry key, so this runs on every directory the
user opens — hence one probe and nothing else.
"""
import os


def main(path):
    return os.path.isdir(path) and os.path.isfile(os.path.join(path, "tokenizer.json"))
