"""Which UDFs a node's page asks about before running (SPEC §53.5 NU-6):
`canvas_node_exec.execution_type`, the hosted workbench's classifier rule for
rule. Each case names the workbench rule it pins."""
import pytest

from fused_render import canvas_node_exec as ex

PLAIN = '@fused.udf\ndef udf():\n    return 1\n'


@pytest.mark.parametrize("code", [
    PLAIN,
    '@fused.udf()\ndef udf():\n    return 1\n',
    '@fused.udf(cache_max_age="0s")\ndef udf(name: str = "x"):\n    return name\n',
    # `.map` on something that is not a UDF.
    '@fused.udf\ndef udf():\n    return list(map(str, [1]))\n',
    '@fused.udf\ndef udf():\n    import pandas as pd\n    return pd.Series([1]).map(str)\n',
    # A realtime engine named INSIDE the code is no batch work.
    '@fused.udf\ndef udf():\n    return fused.run("x", engine="realtime")\n',
    '@fused.udf\ndef udf():\n    return fused.run("x", engine="local")\n',
    '@fused.udf\ndef udf():\n    return fused.run("x", instance_type="realtime")\n',
    # An `engine=` on a call that is neither a UDF nor fused.run/submit.
    '@fused.udf\ndef udf():\n    import sqlalchemy\n    return sqlalchemy.create_engine(engine="small")\n',
    "",
])
def test_runs_freely(code):
    assert ex.execution_type(code) == ex.NONE


@pytest.mark.parametrize("code", [
    '@fused.udf(engine="small")\ndef udf():\n    return 1\n',
    '@fused.udf(instance_type="m5.large")\ndef udf():\n    return 1\n',
    '@fused.udf(cache_max_age="0s", engine = "large", disk_size_gb=100)\ndef udf():\n    return 1\n',
    # The text rule does not read the value: the workbench asks for these too.
    '@fused.udf(engine="realtime")\ndef udf():\n    return 1\n',
    '@fused.udf(engine="local")\ndef udf():\n    return 1\n',
    '@fused.udf(instance_type="realtime")\ndef udf():\n    return 1\n',
    # …and it reads text, so it decides even where the code does not parse.
    '@fused.udf(engine="small")\ndef udf(:\n',
])
def test_a_decorator_that_names_an_engine_is_a_batch_job(code):
    assert ex.execution_type(code) == ex.DECORATOR_BATCH


def test_the_tree_finds_a_batch_decorator_the_text_rule_cannot_see():
    # `[^)]*` stops at the first `)`: a nested call hides the kwarg from the
    # text rule, and the tree still reads it — by its value this time.
    hidden = '@fused.udf(cache_max_age=str(1), engine="small")\ndef udf():\n    return 1\n'
    assert ex._decorator_names_an_engine(hidden) is False
    assert ex.execution_type(hidden) == ex.DECORATOR_BATCH
    realtime = '@fused.udf(cache_max_age=str(1), engine="realtime")\ndef udf():\n    return 1\n'
    assert ex.execution_type(realtime) == ex.NONE


@pytest.mark.parametrize("code", [
    # A decorated function, with and without parentheses.
    '@fused.udf\ndef inner(x):\n    return x\n\n@fused.udf\ndef udf():\n    return inner.map([1, 2])\n',
    '@fused.udf()\ndef inner(x):\n    return x\n\n@fused.udf\ndef udf():\n    return inner.map([1, 2]).df()\n',
    # A loaded UDF.
    '@fused.udf\ndef udf():\n    other = fused.load("other")\n    return other.map([1], engine="realtime")\n',
])
def test_map_on_a_udf(code):
    assert ex.execution_type(code) == ex.MAP


@pytest.mark.parametrize("code", [
    '@fused.udf\ndef udf():\n    return fused.run("x", engine="small")\n',
    '@fused.udf\ndef udf():\n    return fused.submit("x", [1], instance_type="m5.large")\n',
    '@fused.udf\ndef udf():\n    other = fused.load("other")\n    return other(engine="large")\n',
    '@fused.udf\ndef inner():\n    return 1\n\n@fused.udf\ndef udf():\n    return inner(instance_type="small")\n',
])
def test_batch_work_started_inside(code):
    assert ex.execution_type(code) == ex.INNER_BATCH


def test_search_order_is_decorator_then_map_then_method():
    both = ('@fused.udf\ndef inner(x):\n    return x\n\n@fused.udf\ndef udf():\n'
            '    fused.run("x", engine="small")\n    return inner.map([1])\n')
    assert ex.execution_type(both) == ex.MAP


def test_code_that_does_not_parse_runs_freely_and_nothing_raises():
    assert ex.execution_type("def udf(:\n") == ex.NONE
    assert ex.execution_type("\x00") == ex.NONE
    assert ex.execution_type(None) == ex.NONE  # type: ignore[arg-type]


def test_the_prompts_wording_is_the_workbenchs():
    assert ex.confirm_title(ex.DECORATOR_BATCH) == "Confirm batch job run"
    assert ex.confirm_title(ex.INNER_BATCH) == "Confirm batch operation"
    assert ex.confirm_title(ex.MAP) == "Confirm map operation"
    assert ex.confirm_title("unknown") == "Confirm execution"
    assert ex.paused_reason(ex.DECORATOR_BATCH) == "Auto-execution is disabled for batch job UDFs"
    assert ex.paused_reason(ex.INNER_BATCH) == "Auto-execution is disabled for UDFs with inner batch jobs"
    assert ex.paused_reason(ex.MAP) == "Auto-execution is disabled for map operations"
    assert ex.paused_reason("unknown") == "Auto-execution is disabled"
