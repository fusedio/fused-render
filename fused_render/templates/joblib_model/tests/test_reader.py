"""Unit tests for joblib_model/reader.py.

Needs joblib/scikit-learn/xgboost/lightgbm at test time (unlike every other
template's tests, since this reader genuinely has to unpickle real estimators
to introspect them — confirmed there's no existing wiring in this repo that
installs a template's own pyproject.toml deps into the pytest env, so this is
self-documented rather than added to CI):

    uv run --with joblib --with scikit-learn --with xgboost --with lightgbm \
        pytest fused_render/templates/joblib_model/tests/test_reader.py -v
"""
import io
import json
import os
import pickle
import random
import sys

import joblib
import joblib.numpy_pickle
import numpy as np
import pytest
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # import the sibling reader.py

import reader  # noqa: E402


def _dump(obj, tmp_path, name="model.joblib"):
    path = os.path.join(str(tmp_path), name)
    joblib.dump(obj, path)
    return path


def _make_bundle():
    rng = np.random.RandomState(0)
    x = rng.rand(200, 6)
    y = rng.randint(0, 3, size=200)
    scaler = StandardScaler().fit(x)
    clf = XGBClassifier(n_estimators=5, max_depth=2, eval_metric="mlogloss")
    clf.fit(scaler.transform(x), y)
    return {
        "scaler": scaler,
        "classifier": clf,
        "dataset": "synthetic_test_v1",
        "dropped_polygons": ["abc-123"],
    }


# --------------------------------------------------------------- safety scan


def test_dangerous_reduce_is_blocked_and_never_executed(tmp_path, monkeypatch):
    class _Reducer:
        def __reduce__(self):
            return (os.system, ("echo pwned",))

    path = os.path.join(str(tmp_path), "evil.pkl")
    with open(path, "wb") as handle:
        pickle.dump(_Reducer(), handle)  # dumped BEFORE patching os.system below

    calls = []
    monkeypatch.setattr(os, "system", lambda cmd: calls.append(cmd))

    out = reader.main(file=path)
    assert out["scan"]["verdict"] == "blocked"
    offending = [r for r in out["scan"]["refs"] if not r["allowed"]]
    assert any(r["name"] == "system" for r in offending)
    assert calls == []  # the real os.system was never reached
    assert "structure" not in out


def test_object_dtype_array_bypass_is_blocked_and_never_executed(tmp_path, monkeypatch):
    # joblib's own NumpyArrayWrapper.read_array calls a bare pickle.load()
    # directly on the file handle for object-dtype arrays -- entirely
    # outside find_class's allowlist gate -- so np.array([...], dtype=object)
    # can smuggle in a dangerous __reduce__ payload even though every class
    # the scanner sees looks safe. Same technique as
    # test_dangerous_reduce_is_blocked_and_never_executed, but patched on
    # os.system's actual defining module: os.system is a re-export (e.g. of
    # nt.system on Windows) and pickle serialises a global by
    # __module__/__qualname__, not by the name it happens to be imported
    # under, so patching os.system itself doesn't intercept the real call.
    class _Reducer:
        def __reduce__(self):
            return (os.system, ("echo pwned",))

    path = os.path.join(str(tmp_path), "evil_array.joblib")
    joblib.dump({"arr": np.array([_Reducer()], dtype=object)}, path)

    calls = []
    monkeypatch.setattr(sys.modules[os.system.__module__], "system", lambda cmd: calls.append(cmd))

    out = reader.main(file=path)
    assert out["scan"]["verdict"] != "safe"
    assert calls == []  # the real os.system (nt.system/posix.system) was never reached
    assert "structure" not in out


def test_array_with_huge_declared_shape_is_blocked_before_allocating(tmp_path):
    # NumpyArrayWrapper.read_array computes `count` from the wrapper's own
    # (attacker-controlled) shape and calls np.empty(count, dtype) before
    # reading a single array byte -- unbounded by _RAW_SIZE_CAP or
    # _DECOMPRESSED_CAP, which only bound the pickle byte *stream*, not
    # values encoded inside it. A few bytes of shape can declare a
    # petabyte-sized array.
    wrapper = joblib.numpy_pickle.NumpyArrayWrapper(
        subclass=np.ndarray, shape=(10**15,), order="C",
        dtype=np.dtype("float64"), allow_mmap=False,
    )
    buf = io.BytesIO()
    joblib.numpy_pickle.NumpyPickler(buf).dump(wrapper)
    # No array bytes follow -- a real allocation attempt would blow up long
    # before it ever got to reading (or failing to read) them.
    path = os.path.join(str(tmp_path), "huge_shape.joblib")
    with open(path, "wb") as handle:
        handle.write(buf.getvalue())

    out = reader.main(file=path)
    assert out["scan"]["verdict"] != "safe"


def test_unrecognised_but_allowlist_prefixed_library_is_unavailable(tmp_path):
    # catboost is deliberately not a declared dependency of this template, so
    # a real pickle referencing it (hand-built: GLOBAL + EMPTY_TUPLE + REDUCE
    # is enough for our scanner, which never actually imports the module) must
    # report "unavailable", not "blocked" — the class is trusted, just absent.
    payload = (
        pickle.PROTO + bytes([2])
        + pickle.GLOBAL + b"catboost.core\nCatBoostClassifier\n"
        + pickle.EMPTY_TUPLE
        + pickle.REDUCE
        + pickle.STOP
    )
    path = os.path.join(str(tmp_path), "catboost_ref.pkl")
    with open(path, "wb") as handle:
        handle.write(payload)

    out = reader.main(file=path)
    assert out["scan"]["verdict"] == "unavailable"
    assert "catboost" in out["scan"]["message"]


def test_truncated_compressed_file_reports_blocked_without_raising(tmp_path):
    # _decompress_capped isn't wrapped in try/except in _bounded_bytes:
    # zlib.decompressobj(...).decompress(...) raises zlib.error on malformed/
    # truncated compressed data (e.g. a .joblib file that's mid-download,
    # with valid gzip magic bytes but corrupt content) -- this used to
    # propagate uncaught out of main().
    path = os.path.join(str(tmp_path), "truncated.joblib")
    with open(path, "wb") as handle:
        handle.write(b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x00" + random.Random(0).randbytes(64))

    out = reader.main(file=path)  # must not raise
    assert out["scan"]["verdict"] == "blocked"
    assert "decompress" in out["scan"]["message"]


def test_json_serializable_on_every_verdict(tmp_path):
    # allow_nan=False mirrors fused_render/executor.py's dumps_result, whose
    # _JSON_KW rejects non-finite floats outright -- json.dumps' lenient
    # defaults (allow_nan=True) would pass a nan/inf straight through and
    # never catch that class of bug.
    bundle_path = _dump(_make_bundle(), tmp_path)
    json.dumps(reader.main(file=bundle_path), allow_nan=False)

    class _Reducer:
        def __reduce__(self):
            return (eval, ("1+1",))

    evil_path = os.path.join(str(tmp_path), "evil2.pkl")
    with open(evil_path, "wb") as handle:
        pickle.dump(_Reducer(), handle)
    json.dumps(reader.main(file=evil_path), allow_nan=False)


def test_xgboost_default_missing_nan_param_is_json_safe(tmp_path):
    # XGBClassifier's default `missing` param IS float('nan') -- virtually
    # every fitted XGBoost model has one -- and the production response path
    # rejects non-finite floats with allow_nan=False. This used to raise
    # ValueError for almost any real XGBoost file.
    rng = np.random.RandomState(3)
    x = rng.rand(50, 4)
    y = rng.randint(0, 2, size=50)
    clf = XGBClassifier(n_estimators=3, max_depth=2).fit(x, y)
    path = _dump(clf, tmp_path)

    out = reader.main(file=path)
    assert out["scan"]["verdict"] == "safe"
    summary = next(e for e in out["estimators"] if e["path"] == "")
    assert summary["params"]["missing"] == "NaN"
    json.dumps(out, allow_nan=False)


# ------------------------------------------------------------- introspection


def test_multiclass_linear_model_feature_importance_uses_all_classes(tmp_path):
    # coef_ for a multi-class linear model is 2-D (one row per class); the
    # old code took coef[0] and silently discarded every other class's
    # coefficients with no indication in the output. It should reflect all
    # classes -- mean of absolute values, a standard one-ranking summary --
    # not arbitrarily just class 0.
    rng = np.random.RandomState(4)
    x = rng.rand(150, 5)
    y = rng.randint(0, 3, size=150)  # 3 classes
    clf = LogisticRegression(max_iter=500).fit(x, y)
    assert clf.coef_.shape[0] == 3  # sanity: genuinely multi-class

    path = _dump(clf, tmp_path)
    out = reader.main(file=path)
    fi = next(f for f in out["feature_importance"] if f["path"] == "")
    got = {row["feature"]: row["importance"] for row in fi["features"]}

    expected = np.abs(clf.coef_).mean(axis=0)
    class0_only = np.abs(clf.coef_[0])
    assert got != {i: class0_only[i] for i in range(len(class0_only))}
    for i, v in enumerate(expected):
        assert got[i] == pytest.approx(float(v))


def test_one_bad_estimator_does_not_crash_the_others(tmp_path, monkeypatch):
    # _estimator_summary/_feature_importance/_tree_ensemble call attributes
    # and methods on an object that came through __reduce__/__setstate__
    # rather than a normal constructor, with no exception isolation -- one
    # real allowlisted estimator raising from a getter used to take down
    # every other estimator's data in the same bundle, not just its own.
    rng = np.random.RandomState(7)
    x, y = rng.rand(20, 3), rng.randint(0, 2, size=20)
    good = StandardScaler().fit(x)
    bad = LogisticRegression().fit(x, y)
    path = _dump({"good": good, "bad": bad}, tmp_path)

    def rigged_get_params(self, deep=True):
        raise RuntimeError("rigged failure")

    monkeypatch.setattr(LogisticRegression, "get_params", rigged_get_params)

    out = reader.main(file=path)
    assert out["scan"]["verdict"] == "safe"
    paths = {e["path"] for e in out["estimators"]}
    assert "good" in paths
    assert "bad" not in paths


def test_dotted_dict_key_does_not_collide_with_a_nested_path(tmp_path):
    # _describe/_find_estimators build paths via f"{path}.{key}" with no
    # escaping -- a dict key containing a literal dot collides with the path
    # a genuinely nested dict would produce, and
    # template.html's `data.trees.find(x => x.path === t.path)` would then
    # silently pick the wrong one.
    rng = np.random.RandomState(9)
    x, y = rng.rand(20, 2), rng.randint(0, 2, size=20)
    est_a = LogisticRegression().fit(x, y)
    est_b = StandardScaler().fit(x)
    # Both "sensor.temp" (a literal dotted key) and nested sensor -> temp
    # would naively synthesize the same path "sensor.temp".
    bundle = {"sensor.temp": est_a, "sensor": {"temp": est_b}}
    path = _dump(bundle, tmp_path)

    out = reader.main(file=path)
    assert out["scan"]["verdict"] == "safe"
    paths = [e["path"] for e in out["estimators"]]
    assert len(paths) == 2
    assert len(set(paths)) == 2  # distinct paths -- no collision


def test_safe_dict_bundle_like_real_file(tmp_path):
    path = _dump(_make_bundle(), tmp_path)
    out = reader.main(file=path)

    assert out["scan"]["verdict"] == "safe"
    assert out["file"]["size"] == os.path.getsize(path)

    paths = {e["path"] for e in out["estimators"]}
    assert "scaler" in paths and "classifier" in paths

    clf_summary = next(e for e in out["estimators"] if e["path"] == "classifier")
    assert clf_summary["type"] == "XGBClassifier"
    assert clf_summary["params"]["max_depth"] == 2
    assert clf_summary["n_features_in"] == 6

    fi = next(f for f in out["feature_importance"] if f["path"] == "classifier")
    assert len(fi["features"]) == 6
    assert fi["features"] == sorted(fi["features"], key=lambda r: -r["importance"])

    trees = next(t for t in out["trees"] if t["path"] == "classifier")
    assert trees["library"] == "xgboost"
    assert trees["count"] == 15  # 5 rounds x one tree per class (3 classes) for multiclass softmax

    # plain training-provenance metadata surfaces via the structure walk,
    # with no separate sidecar-merge step
    top_keys = {item["key"] for item in out["structure"]["items"]}
    assert "dataset" in top_keys and "dropped_polygons" in top_keys


def test_tree_detail_lazy_fetch_matches_count(tmp_path):
    path = _dump(_make_bundle(), tmp_path)
    overview = reader.main(file=path)
    trees = next(t for t in overview["trees"] if t["path"] == "classifier")

    detail = reader.main(file=path, tree_index=trees["count"] - 1)
    detail_trees = next(t for t in detail["trees"] if t["path"] == "classifier")
    assert "tree" in detail_trees
    node = detail_trees["tree"]
    assert "leaf" in node or ("feature" in node and "left" in node and "right" in node)


def test_bare_random_forest(tmp_path):
    rng = np.random.RandomState(1)
    x = rng.rand(100, 4)
    y = rng.randint(0, 2, size=100)
    clf = RandomForestClassifier(n_estimators=3, max_depth=2, random_state=0).fit(x, y)
    path = _dump(clf, tmp_path)

    out = reader.main(file=path, tree_index=0)
    assert out["scan"]["verdict"] == "safe"
    trees = next(t for t in out["trees"] if t["path"] == "")
    assert trees["library"] == "sklearn_forest"
    assert trees["count"] == 3
    assert "tree" in trees


def test_gradient_boosting_binary(tmp_path):
    # GradientBoostingClassifier's estimators_ is 2-D (n_rounds, n_classes) --
    # a row of trees, not one tree each -- unlike RandomForest's flat
    # estimators_. _tree_kind used to misclassify it as "sklearn_forest",
    # and est.estimators_[idx].tree_ raised AttributeError since
    # est.estimators_[idx] is itself a sub-array, not a single tree.
    rng = np.random.RandomState(5)
    x = rng.rand(100, 4)
    y = rng.randint(0, 2, size=100)
    clf = GradientBoostingClassifier(n_estimators=4, max_depth=2, random_state=0).fit(x, y)
    path = _dump(clf, tmp_path)

    out = reader.main(file=path, tree_index=0)
    assert out["scan"]["verdict"] == "safe"
    trees = next(t for t in out["trees"] if t["path"] == "")
    assert trees["library"] == "sklearn_gradient_boosting"
    assert trees["count"] == 4  # 4 rounds x 1 tree (binary => single column)
    assert "tree" in trees
    assert "feature" in trees["tree"] or "leaf" in trees["tree"]


def test_gradient_boosting_multiclass(tmp_path):
    rng = np.random.RandomState(6)
    x = rng.rand(120, 4)
    y = rng.randint(0, 3, size=120)  # 3 classes
    clf = GradientBoostingClassifier(n_estimators=3, max_depth=2, random_state=0).fit(x, y)
    assert clf.estimators_.shape == (3, 3)
    path = _dump(clf, tmp_path)

    # Fetch the last tree (round 2, class 2) -- genuinely reachable, not just
    # tree_index=0 which the 1-D fallback would already have gotten "right".
    out = reader.main(file=path, tree_index=8)
    trees = next(t for t in out["trees"] if t["path"] == "")
    assert trees["library"] == "sklearn_gradient_boosting"
    assert trees["count"] == 9  # 3 rounds x 3 classes
    assert trees["trees"][8] == {"index": 8, "round": 2, "class": 2}
    assert "tree" in trees
    assert "feature" in trees["tree"] or "leaf" in trees["tree"]


def test_xgboost_unicode_digit_feature_name_does_not_crash():
    # split[1:].isdigit() is True for non-ASCII Unicode "digit" characters
    # int() can't parse (e.g. superscript "²"), which used to crash this
    # tree's render entirely.
    node = {
        "nodeid": 0, "split": "f²", "split_condition": 0.5,
        "yes": 1, "no": 2,
        "children": [{"nodeid": 1, "leaf": 0.1}, {"nodeid": 2, "leaf": -0.1}],
    }
    result = reader._xgboost_tree_node(node)
    assert result["feature"] == "f²"  # preserved as-is, not silently coerced


def test_xgboost_leading_zero_feature_name_is_preserved_literally():
    # A real feature literally named "f007" used to be silently misread as
    # index 7 with no warning -- it doesn't round-trip back to "f007", so it
    # must be kept as the literal name.
    node = {
        "nodeid": 0, "split": "f007", "split_condition": 0.5,
        "yes": 1, "no": 2,
        "children": [{"nodeid": 1, "leaf": 0.1}, {"nodeid": 2, "leaf": -0.1}],
    }
    result = reader._xgboost_tree_node(node)
    assert result["feature"] == "f007"  # not silently turned into 7 or "f7"


def test_tree_node_recursion_has_a_depth_guard():
    # _sklearn_tree_node/_xgboost_tree_node/_lightgbm_tree_node recurse on the
    # tree's own child links with no depth cap, unlike _describe's max_depth=4
    # -- a manufactured (or corrupted) tree with an excessive chain of nodes
    # would hit RecursionError instead of terminating gracefully.
    node = {"leaf_value": 0.0}
    for _ in range(300):
        node = {"split_feature": 0, "threshold": 0.5, "left_child": node, "right_child": {"leaf_value": 0.0}}

    result = reader._lightgbm_tree_node(node)  # must not raise RecursionError

    walked, depth_walked = result, 0
    while "left" in walked and depth_walked < 250:
        walked = walked["left"]
        depth_walked += 1
    assert "leaf" in walked  # terminated with a (possibly truncated) leaf, not a RecursionError
    assert depth_walked <= reader._MAX_TREE_DEPTH + 1  # guard fired well before the full 300-deep chain


def test_lightgbm_model(tmp_path):
    lgb = pytest.importorskip("lightgbm")
    rng = np.random.RandomState(2)
    x = rng.rand(100, 4)
    y = rng.randint(0, 2, size=100)
    clf = lgb.LGBMClassifier(n_estimators=3, max_depth=2, min_child_samples=5).fit(x, y)
    path = _dump(clf, tmp_path)

    out = reader.main(file=path, tree_index=0)
    assert out["scan"]["verdict"] == "safe"
    trees = next(t for t in out["trees"] if t["path"] == "")
    assert trees["library"] == "lightgbm"
    assert trees["count"] == 3
    assert "tree" in trees


def test_file_stats_and_sibling_npy_chunks(tmp_path):
    path = _dump(_make_bundle(), tmp_path, name="chunked.joblib")
    # Simulate joblib's own memmap-split sibling files.
    for suffix in ("_01.npy", "_02.npy"):
        open(path + suffix, "wb").close()

    out = reader.main(file=path)
    assert out["file"]["size"] == os.path.getsize(path)
    assert sorted(out["file"]["sibling_files"]) == ["chunked.joblib_01.npy", "chunked.joblib_02.npy"]


def test_dict_traversal_is_capped_like_list_traversal(tmp_path):
    # _describe/_find_estimators cap list/tuple iteration at obj[:50], but
    # dict iteration was unbounded -- a dict with many keys (attacker- or
    # just carelessly-constructed) could blow up the structure walk's work
    # with no cap, unlike its list/tuple sibling branches.
    big_dict = {f"key_{i}": i for i in range(200)}
    path = _dump(big_dict, tmp_path)

    out = reader.main(file=path)
    assert out["scan"]["verdict"] == "safe"
    structure = out["structure"]
    assert structure["kind"] == "dict"
    assert structure["length"] == 200
    assert len(structure["items"]) == 50


def test_missing_file_reports_blocked_without_raising(tmp_path):
    out = reader.main(file=os.path.join(str(tmp_path), "does_not_exist.joblib"))
    assert out["scan"]["verdict"] == "blocked"
