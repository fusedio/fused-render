"""`ai/toolcalls.py` (docs/LOCAL_MODEL_GATEWAY.md §2 "Tools"): the
family-keyed, streaming-safe tool-call parser. Every family is fed both as
one chunk and with its markers split across chunks; the events must be the
same either way, and text outside a marker must come out on the chunk that
delivered it."""
import pytest

from fused_render.ai.toolcalls import FAMILIES, StreamParser, parse_family


def run(family, chunks):
    parser = StreamParser(family)
    events = []
    for chunk in chunks:
        events.extend(parser.feed(chunk))
    events.extend(parser.finish())
    return events


def split_every(text, n):
    return [text[i:i + n] for i in range(0, len(text), n)]


def calls(events):
    return [v for k, v in events if k == "call"]


def text(events):
    return "".join(v for k, v in events if k == "text")


# ------------------------------------------------------------ parse_family

@pytest.mark.parametrize("name, family", [
    ("mlx-community/Qwen3.5-4B-OptiQ-4bit", "qwen"),
    ("Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf", "qwen"),
    ("NousResearch/Hermes-3-Llama-3.1-8B", "hermes"),
    ("prism-ml/Ternary-Bonsai-2-27B-mlx-2bit", "hermes"),
    ("meta-llama/Llama-3.2-3B-Instruct", "llama3"),
    ("gemma-4-E4B-it-Q4_K_M.gguf", "gemma"),
    ("mlx-community/gemma-4-26B-A4B-it-4bit", "gemma"),
    ("LFM2.5-1.2B-Instruct-Q4_K_M.gguf", "lfm"),
    ("LiquidAI/LFM2.5-24B-A2B-MLX-4bit", "lfm"),
    ("someone/mystery-model", "hermes"),
    ("", "hermes"),
])
def test_parse_family_by_basename_prefix(name, family):
    assert parse_family(name) == family


def test_every_family_has_a_parser():
    for family in FAMILIES:
        assert StreamParser(family).family == family
    assert StreamParser("nope").family == "hermes"


# ------------------------------------------------------------ qwen / hermes

QWEN = ('Let me check. <tool_call>{"name": "get_weather", "arguments": '
        '{"city": "Paris"}}</tool_call> Done.')


@pytest.mark.parametrize("family", ["qwen", "hermes"])
def test_qwen_single_call_with_text_around_it(family):
    events = run(family, [QWEN])
    assert events == [
        ("text", "Let me check. "),
        ("call", {"name": "get_weather", "arguments": {"city": "Paris"}}),
        ("text", " Done."),
    ]


@pytest.mark.parametrize("n", [1, 3, 7, 16])
def test_qwen_markers_split_across_chunks(n):
    events = run("qwen", split_every(QWEN, n))
    assert calls(events) == [{"name": "get_weather", "arguments": {"city": "Paris"}}]
    assert text(events) == "Let me check.  Done."


def test_qwen_text_outside_markers_is_emitted_immediately():
    parser = StreamParser("qwen")
    assert parser.feed("Hello ") == [("text", "Hello ")]
    assert parser.feed("world") == [("text", "world")]
    # A chunk ending in a prefix of `<tool_call>` is held, the rest is not.
    assert parser.feed(" ok <tool") == [("text", " ok ")]
    assert parser.feed("_call>") == []
    assert parser.feed('{"name": "f", "arguments": {}}</tool_call>') == [
        ("call", {"name": "f", "arguments": {}})]
    assert parser.finish() == []


def test_qwen_angle_bracket_that_is_not_a_marker_is_released():
    events = run("qwen", ["a < b and <too", "k> c"])
    # Only the tail that could still become `<tool_call>` is held back.
    assert events == [("text", "a < b and "), ("text", "<took> c")]


def test_qwen_multiple_calls_and_parameters_alias():
    body = ('<tool_call>{"name": "a", "arguments": {"x": 1}}</tool_call>\n'
            '<tool_call>{"name": "b", "parameters": {"y": 2}}</tool_call>')
    events = run("qwen", split_every(body, 5))
    assert calls(events) == [{"name": "a", "arguments": {"x": 1}},
                             {"name": "b", "arguments": {"y": 2}}]
    assert text(events) == "\n"


def test_qwen_list_inside_one_block_is_several_calls():
    body = '<tool_call>[{"name": "a", "arguments": {}}, {"name": "b", "arguments": {}}]</tool_call>'
    assert [c["name"] for c in calls(run("qwen", [body]))] == ["a", "b"]


def test_qwen_unparseable_block_is_released_as_text_verbatim():
    body = "<tool_call>this is not json</tool_call>"
    assert run("qwen", split_every(body, 4)) == [("text", body)] or \
        text(run("qwen", split_every(body, 4))) == body


def test_qwen_unterminated_block_at_finish_is_decoded_if_it_can_be():
    events = run("qwen", ['<tool_call>{"name": "a", "arguments": {"k": "v"}}'])
    assert events == [("call", {"name": "a", "arguments": {"k": "v"}})]
    events = run("qwen", ["<tool_call>{broken"])
    assert text(events) == "<tool_call>{broken"


def test_string_arguments_are_decoded_when_they_hold_json():
    body = '<tool_call>{"name": "a", "arguments": "{\\"k\\": 1}"}</tool_call>'
    assert calls(run("qwen", [body])) == [{"name": "a", "arguments": {"k": 1}}]


# ------------------------------------------------------------------ llama3

def test_llama3_python_tag_with_terminator():
    body = '<|python_tag|>{"name": "get_weather", "parameters": {"city": "Paris"}}<|eom_id|>'
    for n in (1, 4, 9, len(body)):
        events = run("llama3", split_every(body, n))
        assert calls(events) == [{"name": "get_weather", "arguments": {"city": "Paris"}}], n
        assert text(events) == "", n


def test_llama3_python_tag_without_terminator_closes_on_complete_json():
    parser = StreamParser("llama3")
    assert parser.feed('<|python_tag|>{"name": "a", "parameters": {"x": ') == []
    events = parser.feed('1}}')
    assert events == [("call", {"name": "a", "arguments": {"x": 1}})]
    assert parser.finish() == []


def test_llama3_json_only_reply_is_a_call():
    body = '{"name": "get_weather", "parameters": {"city": "Paris"}}'
    for n in (1, 5, len(body)):
        events = run("llama3", split_every(body, n))
        assert calls(events) == [{"name": "get_weather", "arguments": {"city": "Paris"}}]
        assert text(events) == ""


def test_llama3_json_only_shape_only_applies_to_a_reply_that_opens_with_a_brace():
    # Prose first: a later `{` is prose too, emitted as it arrives.
    events = run("llama3", ["The answer is ", '{"name": "x"}'])
    assert calls(events) == []
    assert text(events) == 'The answer is {"name": "x"}'
    # Opens with `{` but never becomes a call: released at finish.
    events = run("llama3", ["{not json at all"])
    assert events == [("text", "{not json at all")]


def test_llama3_terminators_are_never_text():
    assert run("llama3", ["Sure.<|eot", "_id|>"]) == [("text", "Sure.")]


def test_llama3_several_python_tag_calls_separated_by_semicolons():
    body = '<|python_tag|>{"name": "a", "parameters": {}}; {"name": "b", "parameters": {}}<|eom_id|>'
    assert [c["name"] for c in calls(run("llama3", [body]))] == ["a", "b"]


# ------------------------------------------------------------------- gemma

def test_gemma_tool_code_fence_with_python_call():
    body = "Let me look.\n```tool_code\nprint(get_weather(city='Paris', days=3))\n```\n"
    for n in (1, 6, len(body)):
        events = run("gemma", split_every(body, n))
        assert calls(events) == [{"name": "get_weather",
                                  "arguments": {"city": "Paris", "days": 3}}], n
        assert text(events) == "Let me look.\n\n", n


def test_gemma_function_call_block_shapes():
    for body in (
        '<start_function_call>call:get_weather{"city": "Paris"}<end_function_call>',
        '<start_function_call>{"name": "get_weather", "arguments": {"city": "Paris"}}<end_function_call>',
        "<start_function_call>get_weather(city='Paris')<end_function_call>",
    ):
        for n in (1, 7, len(body)):
            events = run("gemma", split_every(body, n))
            assert calls(events) == [{"name": "get_weather", "arguments": {"city": "Paris"}}], (body, n)
            assert text(events) == ""


def test_gemma_ordinary_code_fence_is_text():
    body = "```python\nprint(1)\n```"
    assert text(run("gemma", split_every(body, 3))) == body
    assert calls(run("gemma", [body])) == []


# --------------------------------------------------------------------- lfm

def test_lfm_json_list_of_calls():
    body = ('<|tool_call_start|>[{"name": "a", "arguments": {"x": 1}}, '
            '{"name": "b", "arguments": {}}]<|tool_call_end|>')
    for n in (1, 5, 13, len(body)):
        events = run("lfm", split_every(body, n))
        assert calls(events) == [{"name": "a", "arguments": {"x": 1}},
                                 {"name": "b", "arguments": {}}], n
        assert text(events) == ""


def test_lfm_python_style_call_list():
    body = '<|tool_call_start|>[get_weather(city="Paris")]<|tool_call_end|>tail'
    events = run("lfm", split_every(body, 4))
    assert calls(events) == [{"name": "get_weather", "arguments": {"city": "Paris"}}]
    assert text(events) == "tail"


def test_lfm_positional_arguments_ride_as_a_string():
    body = '<|tool_call_start|>[run("ls -la")]<|tool_call_end|>'
    assert calls(run("lfm", [body])) == [{"name": "run", "arguments": "'ls -la'"}]


# --------------------------------------------------------------- bookkeeping

def test_call_counter_tracks_emitted_calls():
    parser = StreamParser("qwen")
    parser.feed('<tool_call>{"name": "a", "arguments": {}}</tool_call>')
    parser.feed('<tool_call>{"name": "b", "arguments": {}}</tool_call>')
    parser.finish()
    assert parser.calls == 2


def test_empty_feed_is_a_no_op():
    parser = StreamParser("qwen")
    assert parser.feed("") == []
    assert parser.finish() == []

