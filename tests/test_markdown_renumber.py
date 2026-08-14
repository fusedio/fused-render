"""Ordered-list renumbering in the markdown editor (SPEC §32, MD-25).

`markdownKeymap` renumbers on Enter and nowhere else, so before MD-25 every
other way of changing a list left the numbers stale: Backspace, selecting a row
and deleting it, cut, paste. The fix is a transactionFilter, which means the
thing to test is not a function but a behaviour — "make this edit, and the
numbers come out right".

So these dispatch real transactions into a real EditorState carrying the real
extensions, through scripts/vendor-codemirror/renumber-probe.mjs. A source
assertion could say the filter is registered; only this can say a cut renumbers
and an undo does not.

Like tests/test_markdown_live_preview.py, the probe needs node and
scripts/vendor-codemirror/node_modules, which is gitignored — these skip rather
than fail where the vendor deps were never installed.
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR = os.path.join(ROOT, "scripts", "vendor-codemirror")
PROBE = os.path.join(VENDOR, "renumber-probe.mjs")
TEMPLATE = os.path.join(
    ROOT, "fused_render", "templates", "markdown", "template.html")


def _require_node():
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    if not os.path.isdir(os.path.join(VENDOR, "node_modules")):
        pytest.skip("scripts/vendor-codemirror/node_modules is absent "
                    "(gitignored; run the vendor install to enable this test)")


def edit(tmp_path, doc, **spec):
    """Apply one edit to `doc` in a real editor and return the resulting text.

    `spec` is the probe's edit descriptor: `changes` + `userEvent` for a direct
    dispatch, `key` + `at` to press a key through the real keymap, `undo` to
    take it back again.
    """
    _require_node()
    path = tmp_path / "note.md"
    path.write_text(doc, encoding="utf-8")
    proc = subprocess.run(
        ["node", PROBE, TEMPLATE, str(path), json.dumps(spec)],
        cwd=VENDOR, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def delete_line(doc, index):
    """A `changes` spec that removes line `index` (0-based) entirely."""
    lines = doc.split("\n")
    start = sum(len(line) + 1 for line in lines[:index])
    return {"from": start, "to": start + len(lines[index]) + 1}


LIST = "1. one\n2. two\n3. three\n4. four\n"


def test_deleting_a_row_renumbers_the_rows_below_it(tmp_path):
    result = edit(tmp_path, LIST,
                  changes=[dict(delete_line(LIST, 1), insert="")],
                  userEvent="delete.selection")
    assert result["doc"] == "1. one\n2. three\n3. four\n"


def test_cutting_a_row_renumbers_it_too(tmp_path):
    # A cut is a delete with a different user event, and the filter keys off the
    # event — so the event a cut actually carries has to be one it accepts.
    result = edit(tmp_path, LIST,
                  changes=[dict(delete_line(LIST, 2), insert="")],
                  userEvent="delete.cut")
    assert result["doc"] == "1. one\n2. two\n3. four\n"


def test_cutting_the_first_row_leaves_the_new_first_number_alone(tmp_path):
    # Deliberate, and the one case where "auto-updates" is arguably not what
    # happens. The first item's number anchors the sequence, so removing the
    # head of `1. 2. 3. 4.` leaves `2. 3. 4.` rather than resetting to 1.
    #
    # The alternative — always normalise to 1 — cannot be had at the same time as
    # keeping a list that legitimately starts at 3 (a list resumed after a
    # paragraph), because the two are identical text. Anchoring is what
    # `markdownKeymap`'s own Enter renumbering does, so this is also the only
    # rule under which Enter and Backspace agree with each other.
    result = edit(tmp_path, LIST,
                  changes=[dict(delete_line(LIST, 0), insert="")],
                  userEvent="delete.cut")
    assert result["doc"] == "2. two\n3. three\n4. four\n"


def test_pasting_a_row_into_the_middle_renumbers_below_it(tmp_path):
    result = edit(tmp_path, LIST,
                  changes=[{"from": 7, "to": 7, "insert": "9. inserted\n"}],
                  userEvent="input.paste")
    assert result["doc"] == "1. one\n2. inserted\n3. two\n4. three\n5. four\n"


def test_a_list_that_does_not_start_at_one_keeps_its_own_start(tmp_path):
    # The first item's number is the sequence's start, not a mistake to correct:
    # `3.` `4.` `5.` is a legitimate continuation of a list split by a paragraph.
    doc = "3. three\n4. four\n9. five\n"
    result = edit(tmp_path, doc,
                  changes=[{"from": 8, "to": 8, "insert": "!"}],
                  userEvent="input.type")
    assert result["doc"] == "3. three!\n4. four\n5. five\n"


def test_a_nested_list_is_numbered_independently(tmp_path):
    doc = "1. one\n   1. a\n   5. b\n2. two\n   1. c\n   1. d\n"
    result = edit(tmp_path, doc,
                  changes=[{"from": 6, "to": 6, "insert": "!"}],
                  userEvent="input.type")
    assert result["doc"] == "1. one!\n   1. a\n   2. b\n2. two\n   1. c\n   2. d\n"


def test_a_bullet_list_between_two_ordered_ones_separates_them(tmp_path):
    # Different marker, different list — the ordered run does not carry its
    # counter across the bullets.
    doc = "1. one\n2. two\n- a\n- b\n1. back\n5. again\n"
    result = edit(tmp_path, doc,
                  changes=[{"from": 6, "to": 6, "insert": "!"}],
                  userEvent="input.type")
    assert result["doc"] == "1. one!\n2. two\n- a\n- b\n1. back\n2. again\n"


def test_the_paren_delimiter_starts_a_new_list(tmp_path):
    # CommonMark: changing `.` to `)` starts a new list, so the counter restarts
    # rather than continuing through.
    doc = "1. one\n2. two\n1) a\n7) b\n"
    result = edit(tmp_path, doc,
                  changes=[{"from": 6, "to": 6, "insert": "!"}],
                  userEvent="input.type")
    assert result["doc"] == "1. one!\n2. two\n1) a\n2) b\n"


def test_digits_inside_a_fenced_block_are_left_alone(tmp_path):
    # The one place `1.` at the start of a line is not a list item. This is why
    # the pass tracks fences rather than matching the regex line by line.
    doc = "1. one\n3. two\n\n```\n1. not\n1. a list\n```\n"
    result = edit(tmp_path, doc,
                  changes=[{"from": 6, "to": 6, "insert": "!"}],
                  userEvent="input.type")
    assert result["doc"] == "1. one!\n2. two\n\n```\n1. not\n1. a list\n```\n"


def test_a_tilde_fence_is_not_closed_by_a_backtick_one(tmp_path):
    doc = "~~~\n1. a\n```\n1. b\n~~~\n\n1. one\n5. two\n"
    at = doc.index("1. one") + len("1. one")
    result = edit(tmp_path, doc,
                  changes=[{"from": at, "to": at, "insert": "!"}],
                  userEvent="input.type")
    assert result["doc"] == "~~~\n1. a\n```\n1. b\n~~~\n\n1. one!\n2. two\n"


def test_a_list_elsewhere_in_the_document_is_untouched(tmp_path):
    # Only the list the edit landed in is renumbered. A deliberately odd list two
    # paragraphs away is not this edit's business, and rewriting it would put
    # changes in the undo step that the user never made.
    doc = "1. one\n3. two\n\nprose\n\n1. far\n9. away\n"
    result = edit(tmp_path, doc,
                  changes=[{"from": 6, "to": 6, "insert": "!"}],
                  userEvent="input.type")
    assert result["doc"] == "1. one!\n2. two\n\nprose\n\n1. far\n9. away\n"


def test_an_indented_continuation_paragraph_does_not_break_the_list(tmp_path):
    doc = "1. one\n\n   more about one\n\n5. two\n"
    result = edit(tmp_path, doc,
                  changes=[{"from": 6, "to": 6, "insert": "!"}],
                  userEvent="input.type")
    assert result["doc"] == "1. one!\n\n   more about one\n\n2. two\n"


def test_enter_still_renumbers_through_the_markdown_keymap(tmp_path):
    # markdownKeymap already renumbered on Enter. The filter runs on top of that
    # transaction, so the check here is that the two do not fight: the result is
    # the same sequence, not a doubly-incremented one.
    result = edit(tmp_path, LIST, key="Enter", at=6)
    assert result["doc"] == "1. one\n2. \n3. two\n4. three\n5. four\n"


def test_undo_restores_the_numbers_the_edit_changed(tmp_path):
    # Two properties at once. The renumbering rides in the same transaction, so
    # ONE undo takes back both the deletion and the renumbering; and undo is
    # excluded from the filter, so it does not immediately re-correct what it
    # just restored.
    result = edit(tmp_path, LIST,
                  changes=[dict(delete_line(LIST, 1), insert="")],
                  userEvent="delete.selection", undo=True)
    assert result["doc"] == LIST


def test_an_already_correct_list_produces_no_changes(tmp_path):
    # The cheap path, and the reason this can run on every keystroke: a
    # well-numbered list yields an empty change set, so the transaction is
    # returned untouched rather than extended.
    result = edit(tmp_path, LIST,
                  changes=[{"from": 6, "to": 6, "insert": "!"}],
                  userEvent="input.type")
    assert result["doc"] == "1. one!\n2. two\n3. three\n4. four\n"
    assert result["pureChanges"] == []


# ---- review findings --------------------------------------------------------


def test_editing_prose_next_to_a_list_does_not_renumber_it(tmp_path):
    # Found in review. `listRegion` used to walk to the neighbouring line when
    # the edit was not on a list line, so typing a character in the paragraph
    # directly above a list rewrote that list's numbers — and put those changes
    # in the same undo step as the keystroke. MD-25 says only the list the edit
    # landed in.
    doc = "prose\n1. one\n5. two\n"
    result = edit(tmp_path, doc,
                  changes=[{"from": 5, "to": 5, "insert": "!"}],
                  userEvent="input.type")
    assert result["doc"] == "prose!\n1. one\n5. two\n"


def test_editing_prose_after_a_list_does_not_renumber_it_either(tmp_path):
    doc = "1. one\n5. two\nprose\n"
    at = doc.index("prose") + len("prose")
    result = edit(tmp_path, doc,
                  changes=[{"from": at, "to": at, "insert": "!"}],
                  userEvent="input.type")
    assert result["doc"] == "1. one\n5. two\nprose!\n"


def test_editing_the_blank_line_between_prose_and_a_list_leaves_it_alone(tmp_path):
    # The narrower half of the same fix: a blank line may EXTEND a region (it
    # sits between two items of a loose list) but may not ANCHOR one, because a
    # blank line is also exactly what separates a list from the prose beside it.
    doc = "prose\n\n1. one\n5. two\n"
    result = edit(tmp_path, doc,
                  changes=[{"from": 6, "to": 6, "insert": " "}],
                  userEvent="input.type")
    assert result["doc"] == "prose\n \n1. one\n5. two\n"


def test_clearing_an_items_text_still_renumbers_the_items_below(tmp_path):
    # The other half of the blank-line rule, found in review. Rejecting every
    # blank as an anchor fixed the prose-adjacent case and broke this one:
    # selecting an item's text and deleting it leaves a blank line INSIDE a
    # loose list, and the items below still have to follow.
    #
    # The two are told apart by what surrounds the blank — an interior blank has
    # list items on both sides, the prose gap does not.
    doc = "1. one\n2. two\n3. three\n"
    result = edit(tmp_path, doc,
                  changes=[{"from": 7, "to": 13, "insert": ""}],
                  userEvent="delete.selection")
    assert result["doc"] == "1. one\n\n2. three\n"
