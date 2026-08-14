"""Reader backing joblib_model/template.html: restricted, allowlist-based
loading of untrusted `.joblib`/`.pkl`/`.pickle` files, then introspection
(hyperparameters, feature importances, tree structure) once every referenced
class/callable is confirmed trusted. Self-contained — templates are scripts
run by the engine, not part of the package (SPEC PY-15/D166).
"""
import io
import json
import math
import os
import pickle
import zlib

import joblib.numpy_pickle

_RAW_SIZE_CAP = 2 * 1024 * 1024 * 1024
_DECOMPRESSED_CAP = 512 * 1024 * 1024
_MAX_ARRAY_BYTES = _DECOMPRESSED_CAP
_MAX_TREE_DEPTH = 200  # real trees rarely exceed depth 30-40

# Exact-symbol allow: never widened to a prefix ("builtins" would allow eval/exec/open).
_EXACT_ALLOW = {
    "builtins": {"dict", "list", "tuple", "set", "frozenset", "complex",
                 "bytes", "bytearray", "str", "int", "float", "bool", "slice"},
    "collections": {"OrderedDict", "defaultdict"},
    "copyreg": {"_reconstructor", "__newobj__", "__newobj_ex__"},
    "_codecs": {"encode"},
}

# Prefix allow: trust the package, not its internal module layout.
_PREFIX_ALLOW = (
    "numpy.", "numpy._core.", "numpy.core.",
    "scipy.", "sklearn.", "xgboost.", "lightgbm.", "catboost.",
    "pandas.", "joblib.",
)

# A prefix-trusted FUNCTION (unlike a class) is called directly with
# attacker-chosen arguments the moment REDUCE runs (e.g. joblib.load itself
# would re-enter an unrestricted load) — so only these known reconstruction
# helpers may resolve as functions; see find_class.
_PREFIX_FUNCTION_ALLOW = {
    ("numpy.core.multiarray", "_reconstruct"),
    ("numpy.core.multiarray", "scalar"),
    ("numpy._core.multiarray", "_reconstruct"),
    ("numpy._core.multiarray", "scalar"),
    ("numpy.core.numeric", "_frombuffer"),
    ("numpy._core.numeric", "_frombuffer"),
    ("numpy.random._pickle", "__randomstate_ctor"),
    ("numpy.random._pickle", "__bit_generator_ctor"),
    ("numpy.random._pickle", "__generator_ctor"),
}


def _is_pyx_unpickle_helper(name: str) -> bool:
    """Cython's auto-generated `__pyx_unpickle_<Class>` reconstruction helpers."""
    return name.startswith("__pyx_unpickle_")


class _Stub:
    """Inert stand-in for every disallowed class; swallows any call as a no-op."""

    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return _Stub()

    def __setstate__(self, state):
        pass

    def __reduce__(self):
        return (_Stub, ())

    def __setitem__(self, key, value):
        pass

    def __getitem__(self, key):
        return _Stub()

    def append(self, value):
        pass

    def extend(self, values):
        pass

    def update(self, *args, **kwargs):
        pass

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0


def _classify(module: str, name: str) -> bool:
    """Fail closed: anything not explicitly recognised is unsafe by default."""
    allowed_names = _EXACT_ALLOW.get(module)
    if allowed_names is not None and name in allowed_names:
        return True
    return any(module == prefix.rstrip(".") or module.startswith(prefix) for prefix in _PREFIX_ALLOW)


class _DelegatingUnpickler(pickle.Unpickler):
    """Plain Unpickler with externally supplied find_class/persistent_load
    (the C-accelerated Unpickler won't allow find_class as an instance attribute)."""

    def __init__(self, file_handle, find_class, persistent_load):
        super().__init__(file_handle)
        self._find_class = find_class
        self._persistent_load = persistent_load

    def find_class(self, module, name):
        return self._find_class(module, name)

    def persistent_load(self, pid):
        return self._persistent_load(pid)


class _SafeNumpyArrayWrapper(joblib.numpy_pickle.NumpyArrayWrapper):
    """NumpyArrayWrapper.read_array bypasses find_class entirely for
    object-dtype arrays (a bare pickle.load()) and sizes its allocation from
    its own declared shape before reading a single byte. This closes both:
    object-dtype content is re-read through the same restricted find_class,
    and an oversized declared shape is refused before allocating."""

    def read_array(self, unpickler, ensure_native_byte_order):
        if self.dtype.hasobject:
            embedded = _DelegatingUnpickler(unpickler.file_handle, unpickler.find_class, unpickler.persistent_load)
            return embedded.load()
        count = 1
        for dim in self.shape:
            count *= int(dim)
        declared_bytes = count * self.dtype.itemsize
        if declared_bytes > _MAX_ARRAY_BYTES:
            raise pickle.UnpicklingError(
                f"refusing to allocate a {declared_bytes:,}-byte array "
                f"(declared shape {tuple(self.shape)!r}), above the "
                f"{_MAX_ARRAY_BYTES:,}-byte safety cap"
            )
        return super().read_array(unpickler, ensure_native_byte_order)


class _SafeNDArrayWrapper(joblib.numpy_pickle.NDArrayWrapper):
    """Legacy joblib<=0.9 wrapper: loads an attacker-named sibling file via
    np.load(allow_pickle=True). Nothing recent uses this format — refuse outright."""

    def read(self, unpickler):
        raise pickle.UnpicklingError(
            "refusing to load a legacy (joblib<=0.9) NDArrayWrapper array"
        )


class _RestrictedUnpickler(joblib.numpy_pickle.NumpyUnpickler):
    """find_class is the sole gate: allowed references resolve for real,
    everything else becomes an inert _Stub and is recorded as blocked."""

    def __init__(self, filename, file_obj):
        super().__init__(filename, file_obj, ensure_native_byte_order=True)
        self.refs = []  # [{"module", "name", "allowed"}], first-seen order, deduped
        self._seen = set()
        self.blocked = []
        self.missing = set()

    def find_class(self, module, name):
        key = (module, name)
        first_seen = key not in self._seen
        self._seen.add(key)

        def record(final_allowed):
            if first_seen:
                self.refs.append({"module": module, "name": name, "allowed": final_allowed})

        if not _classify(module, name):
            record(False)
            self.blocked.append({"module": module, "name": name})
            return _Stub
        try:
            resolved = super().find_class(module, name)
        except (ImportError, AttributeError):
            record(True)
            self.missing.add(module.split(".", 1)[0])
            return _Stub
        if resolved is joblib.numpy_pickle.NumpyArrayWrapper:
            record(True)
            return _SafeNumpyArrayWrapper
        if resolved is joblib.numpy_pickle.NDArrayWrapper:
            record(True)
            return _SafeNDArrayWrapper
        exact = _EXACT_ALLOW.get(module)
        prefix_trusted = exact is None or name not in exact
        is_safe_function = key in _PREFIX_FUNCTION_ALLOW or _is_pyx_unpickle_helper(name)
        if prefix_trusted and not isinstance(resolved, type) and not is_safe_function:
            record(False)
            self.blocked.append({"module": module, "name": name})
            return _Stub
        record(True)
        return resolved

    def persistent_load(self, pid):
        raise pickle.UnpicklingError(f"unsupported persistent reference: {pid!r}")


def _decompress_capped(raw: bytes, cap: int):
    """None on cap exceeded. Recognises joblib's own zlib/gzip wrapping;
    anything else passes through unchanged (fails to parse later, already treated as blocked)."""
    if raw[:2] == b"\x1f\x8b":
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif raw[:1] == b"\x78":
        decompressor = zlib.decompressobj()
    else:
        return raw

    out = bytearray()
    pending = raw
    while pending or decompressor.unconsumed_tail:
        chunk = decompressor.unconsumed_tail or pending
        pending = b""
        piece = decompressor.decompress(chunk, max(1, cap - len(out)))
        out.extend(piece)
        if len(out) > cap:
            return None
        if not piece and not decompressor.unconsumed_tail:
            break
    out.extend(decompressor.flush())
    return bytes(out) if len(out) <= cap else None


def _bounded_bytes(path: str):
    """(bytes, error); never raises."""
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return None, f"could not stat file: {exc}"
    if size > _RAW_SIZE_CAP:
        return None, f"file is {size:,} bytes, above the {_RAW_SIZE_CAP:,}-byte safety cap"
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        return None, f"could not read file: {exc}"
    try:
        data = _decompress_capped(raw, _DECOMPRESSED_CAP)
    except zlib.error as exc:
        return None, f"could not decompress: {exc}"
    if data is None:
        return None, f"decompressed content exceeds the {_DECOMPRESSED_CAP:,}-byte safety cap"
    return data, None


def _restricted_load(path: str, data: bytes):
    """(verdict, refs, message, obj); obj is real only when verdict is "safe"."""
    unpickler = _RestrictedUnpickler(path, io.BytesIO(data))
    try:
        obj = unpickler.load()
    except Exception as exc:  # noqa: BLE001 - any failure here means "not proven safe"
        return "blocked", [], f"could not parse as a pickle stream: {exc}", None

    if unpickler.blocked:
        return "blocked", unpickler.refs, None, None
    if unpickler.missing:
        return "unavailable", unpickler.refs, ", ".join(sorted(unpickler.missing)), None
    return "safe", unpickler.refs, None, obj


def _sibling_chunks(path: str):
    """joblib's memmap sibling files (`name.pkl_01.npy`, `_02.npy`, ...)."""
    base = os.path.basename(path)
    directory = os.path.dirname(path) or "."
    try:
        entries = os.listdir(directory)
    except OSError:
        return []
    prefix = base + "_"
    return sorted(
        name for name in entries
        if name.startswith(prefix) and name.endswith(".npy")
    )


def _short_repr(value, limit=120) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _json_float(v):
    """float, or a JSON-safe string sentinel for nan/inf (XGBClassifier's
    default `missing` param is nan, so this is common, not exotic)."""
    f = float(v)
    if math.isnan(f):
        return "NaN"
    if math.isinf(f):
        return "Infinity" if f > 0 else "-Infinity"
    return f


def _escape_path_key(key) -> str:
    """Escapes a dict key before splicing into a synthesized dotted path, so a
    literal "." in a key can't collide with an actually-nested path."""
    return str(key).replace("\\", "\\\\").replace(".", "\\.").replace("[", "\\[").replace("]", "\\]")


def _describe(obj, path="", depth=0, max_depth=4):
    if depth >= max_depth:
        return {"path": path, "kind": "truncated"}
    if isinstance(obj, dict):
        return {
            "path": path, "kind": "dict",
            "length": len(obj),
            "items": [
                {"key": str(k), **_describe(v, f"{path}.{_escape_path_key(k)}" if path else _escape_path_key(k), depth + 1, max_depth)}
                for k, v in list(obj.items())[:50]
            ],
        }
    if isinstance(obj, (list, tuple)):
        return {
            "path": path, "kind": "list",
            "length": len(obj),
            "items": [_describe(v, f"{path}[{i}]", depth + 1, max_depth) for i, v in enumerate(obj[:50])],
        }
    if _is_ndarray(obj):
        return {"path": path, "kind": "ndarray", "shape": list(obj.shape), "dtype": str(obj.dtype)}
    if hasattr(obj, "get_params") and callable(obj.get_params):
        return {"path": path, "kind": "estimator", "type": type(obj).__name__, "module": type(obj).__module__}
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        value = _json_float(obj) if isinstance(obj, float) else obj
        return {"path": path, "kind": "scalar", "value": value if not isinstance(value, str) or len(value) <= 200 else value[:200] + "…"}
    return {"path": path, "kind": "object", "type": type(obj).__name__, "repr": _short_repr(obj)}


def _is_ndarray(obj) -> bool:
    return type(obj).__module__.startswith("numpy") and type(obj).__name__ == "ndarray"


def _find_estimators(obj, path="", depth=0, max_depth=4, seen=None):
    """All objects with a scikit-learn-style get_params(), tagged with their path."""
    if seen is None:
        seen = set()
    if depth >= max_depth:
        return []
    obj_id = id(obj)
    if obj_id in seen:
        return []
    found = []
    is_estimator = hasattr(obj, "get_params") and callable(obj.get_params)
    if is_estimator:
        seen.add(obj_id)
        found.append((path, obj))
    if isinstance(obj, dict):
        for key, value in list(obj.items())[:50]:
            child_path = f"{path}.{_escape_path_key(key)}" if path else _escape_path_key(key)
            found.extend(_find_estimators(value, child_path, depth + 1, max_depth, seen))
    elif isinstance(obj, (list, tuple)):
        for index, value in enumerate(obj[:50]):
            found.extend(_find_estimators(value, f"{path}[{index}]", depth + 1, max_depth, seen))
    elif is_estimator and hasattr(obj, "__dict__"):
        # Composite estimators (Pipeline, VotingClassifier, ...) hold sub-estimators
        # as plain attributes, not get_params() values.
        for key, value in list(vars(obj).items())[:50]:
            child_path = f"{path}.{_escape_path_key(key)}" if path else _escape_path_key(key)
            found.extend(_find_estimators(value, child_path, depth + 1, max_depth, seen))
    return found


def _jsonable_params(params: dict) -> dict:
    out = {}
    for key, value in params.items():
        if isinstance(value, float):
            out[key] = _json_float(value)
        elif isinstance(value, (str, int, bool)) or value is None:
            out[key] = value
        else:
            out[key] = _short_repr(value, 80)
    return out


def _estimator_summary(path: str, est) -> dict:
    summary = {
        "path": path,
        "type": type(est).__name__,
        "module": type(est).__module__,
        "params": _jsonable_params(est.get_params(deep=False)),
    }
    if hasattr(est, "n_features_in_"):
        summary["n_features_in"] = int(est.n_features_in_)
    if hasattr(est, "classes_"):
        try:
            summary["classes"] = [c.item() if hasattr(c, "item") else c for c in list(est.classes_)]
        except Exception:  # noqa: BLE001 - best-effort, never fatal to the view
            pass
    return summary


def _feature_importance(path: str, est):
    values = None
    if hasattr(est, "feature_importances_"):
        values = list(est.feature_importances_)
    elif hasattr(est, "coef_"):
        coef = est.coef_
        if getattr(coef, "ndim", 1) > 1:
            values = list(abs(coef).mean(axis=0))  # multi-class: mean |coef| across classes
        else:
            values = [abs(float(v)) for v in coef]
    if values is None:
        return None
    ranked = sorted(
        ({"feature": i, "importance": float(v)} for i, v in enumerate(values)),
        key=lambda row: -row["importance"],
    )
    for row in ranked:
        row["importance"] = _json_float(row["importance"])
    return {"path": path, "features": ranked}


def _tree_kind(est):
    if hasattr(est, "get_booster"):
        return "xgboost"
    if hasattr(est, "booster_") and hasattr(est.booster_, "dump_model"):
        return "lightgbm"
    if hasattr(est, "estimators_"):
        if getattr(est.estimators_, "ndim", 1) > 1:
            return "sklearn_gradient_boosting"  # 2-D: one row of trees per round
        return "sklearn_forest"
    if hasattr(est, "tree_") and hasattr(est.tree_, "children_left"):
        return "sklearn_tree"
    return None


def _sklearn_tree_node(tree, node_id, depth=0):
    if depth >= _MAX_TREE_DEPTH:
        return {"leaf": [], "truncated": True}
    if tree.children_left[node_id] == -1:
        value = tree.value[node_id]
        return {"leaf": [_json_float(v) for v in value.reshape(-1)]}
    return {
        "feature": int(tree.feature[node_id]),
        "threshold": _json_float(tree.threshold[node_id]),
        "left": _sklearn_tree_node(tree, tree.children_left[node_id], depth + 1),
        "right": _sklearn_tree_node(tree, tree.children_right[node_id], depth + 1),
    }


def _xgboost_feature_index(split: str):
    """xgboost names an unnamed feature "f<index>" (a string); normalized to
    an int only when it round-trips exactly, so a real feature literally
    named "f007" isn't corrupted."""
    if split.startswith("f"):
        try:
            index = int(split[1:])
        except ValueError:
            return split
        if f"f{index}" == split:
            return index
    return split


def _xgboost_tree_node(node, depth=0):
    if depth >= _MAX_TREE_DEPTH:
        return {"leaf": [], "truncated": True}
    if "leaf" in node:
        return {"leaf": [_json_float(node["leaf"])]}
    children = {child["nodeid"]: child for child in node.get("children", [])}
    return {
        "feature": _xgboost_feature_index(node["split"]),
        "threshold": _json_float(node["split_condition"]),
        "left": _xgboost_tree_node(children[node["yes"]], depth + 1),
        "right": _xgboost_tree_node(children[node["no"]], depth + 1),
    }


def _lightgbm_tree_node(node, depth=0):
    if depth >= _MAX_TREE_DEPTH:
        return {"leaf": [], "truncated": True}
    if "leaf_value" in node:
        return {"leaf": [_json_float(node["leaf_value"])]}
    return {
        "feature": int(node["split_feature"]),
        "threshold": _json_float(node["threshold"]),
        "left": _lightgbm_tree_node(node["left_child"], depth + 1),
        "right": _lightgbm_tree_node(node["right_child"], depth + 1),
    }


def _tree_ensemble(path: str, est, tree_index):
    kind = _tree_kind(est)
    if kind is None:
        return None

    if kind == "sklearn_tree":
        count = 1
        meta = [{"index": 0}]
        tree = est.tree_
        node_builder = lambda: _sklearn_tree_node(tree, 0)  # noqa: E731
    elif kind == "sklearn_forest":
        count = len(est.estimators_)
        meta = [{"index": i} for i in range(count)]
        idx = tree_index if tree_index is not None else 0
        node_builder = lambda: _sklearn_tree_node(est.estimators_[idx].tree_, 0)  # noqa: E731
    elif kind == "sklearn_gradient_boosting":
        n_rounds, n_classes = est.estimators_.shape
        count = n_rounds * n_classes
        meta = [{"index": i, "round": i // n_classes, "class": i % n_classes} for i in range(count)]
        idx = tree_index if tree_index is not None else 0

        def node_builder():
            round_idx, class_idx = divmod(idx, n_classes)
            return _sklearn_tree_node(est.estimators_[round_idx, class_idx].tree_, 0)
    elif kind == "xgboost":
        # num_boosted_rounds() is cheap; get_dump() (needed only for the one
        # requested tree) costs seconds on a large model.
        booster = est.get_booster()
        n_classes = len(est.classes_) if hasattr(est, "classes_") and len(est.classes_) > 2 else 1
        count = booster.num_boosted_rounds() * n_classes
        meta = [{"index": i, "round": i // n_classes, "class": i % n_classes} for i in range(count)]
        idx = tree_index if tree_index is not None else 0

        def node_builder():
            return _xgboost_tree_node(json.loads(booster.get_dump(dump_format="json")[idx]))
    elif kind == "lightgbm":
        count = est.booster_.num_trees()
        meta = [{"index": i} for i in range(count)]
        idx = tree_index if tree_index is not None else 0

        def node_builder():
            return _lightgbm_tree_node(est.booster_.dump_model()["tree_info"][idx]["tree_structure"])
    else:
        return None

    result = {"path": path, "library": kind, "count": count, "trees": meta}
    if tree_index is not None or count == 1:
        try:
            result["tree"] = node_builder()
        except Exception as exc:  # noqa: BLE001 - a bad index still returns a usable summary
            result["tree_error"] = str(exc)
    return result


def _safe(fn, *args):
    """None on any failure, isolating one bad estimator from the rest of the bundle."""
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001 - best-effort, isolated per estimator
        return None


def main(file: str, tree_index=None) -> dict:
    try:
        idx = int(tree_index) if tree_index not in (None, "") else None
    except (TypeError, ValueError):
        idx = None

    try:
        size = os.path.getsize(file)
        mtime = os.path.getmtime(file)
    except OSError as exc:
        return {"file": {"error": str(exc)}, "scan": {"verdict": "blocked", "refs": [], "message": str(exc)}}

    data, error = _bounded_bytes(file)
    file_info = {"size": size, "mtime": mtime, "sibling_files": _sibling_chunks(file)}
    if error is not None:
        return {"file": file_info, "scan": {"verdict": "blocked", "refs": [], "message": error}}

    verdict, refs, message, obj = _restricted_load(file, data)
    scan_info = {"verdict": verdict, "refs": refs, "message": message}
    if verdict != "safe":
        return {"file": file_info, "scan": scan_info}

    found = _find_estimators(obj)

    return {
        "file": file_info,
        "scan": scan_info,
        "structure": _describe(obj),
        "estimators": [s for s in (_safe(_estimator_summary, p, e) for p, e in found) if s],
        "feature_importance": [fi for fi in (_safe(_feature_importance, p, e) for p, e in found) if fi],
        "trees": [t for t in (_safe(_tree_ensemble, p, e, idx) for p, e in found) if t],
    }
