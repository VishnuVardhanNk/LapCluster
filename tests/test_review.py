import json

import pytest

from lapclusters.review import build_prompt, parse_findings, run
from lapclusters.taskqueue import TaskError


def test_prompt_names_the_file_and_numbers_its_lines():
    prompt = build_prompt("src/app.py", "a = 1\nb = 2\n")
    assert "src/app.py" in prompt
    assert "1 | a = 1" in prompt
    assert "2 | b = 2" in prompt


def test_parses_findings_object():
    text = json.dumps({"findings": [{"line": 3, "severity": "high", "message": "Divides by zero."}]})
    assert parse_findings(text) == [
        {"line": 3, "severity": "high", "message": "Divides by zero."}
    ]


def test_parses_bare_list_and_code_fence():
    text = '```json\n[{"line": 1, "severity": "low", "message": "Unused variable."}]\n```'
    assert parse_findings(text) == [
        {"line": 1, "severity": "low", "message": "Unused variable."}
    ]


def test_no_findings_is_an_empty_list():
    assert parse_findings('{"findings": []}') == []


def test_tidies_loose_values():
    text = json.dumps(
        [
            {"line": "12", "severity": "HIGH", "message": "  Leaks a file handle.  "},
            {"line": None, "severity": "urgent", "message": "Odd severity."},
            {"line": 4, "severity": "low", "message": ""},
            "not an object",
        ]
    )
    assert parse_findings(text) == [
        {"line": 12, "severity": "high", "message": "Leaks a file handle."},
        {"line": 0, "severity": "medium", "message": "Odd severity."},
    ]


@pytest.mark.parametrize("text", ["I found two bugs.", "", '{"findings": "none"}', "42"])
def test_rejects_replies_that_are_not_a_findings_list(text):
    with pytest.raises(ValueError):
        parse_findings(text)


def test_run_returns_findings_as_json():
    reply = '{"findings": [{"line": 2, "severity": "medium", "message": "Off by one."}]}'
    result = run({"path": "a.py", "content": "x\ny\n"}, lambda prompt, schema=None: reply)
    assert json.loads(result) == [{"line": 2, "severity": "medium", "message": "Off by one."}]


def test_run_asks_the_model_for_structured_output():
    seen = {}

    def generate(prompt, schema=None):
        seen["schema"] = schema
        return '{"findings": []}'

    run({"path": "a.py", "content": "x\n"}, generate)
    assert seen["schema"]["properties"]["findings"]["type"] == "array"


def test_run_retries_once_after_invalid_json():
    replies = iter(["sorry, here you go", '{"findings": []}'])
    result = run({"path": "a.py", "content": "x\n"}, lambda prompt, schema=None: next(replies))
    assert json.loads(result) == []


def test_run_fails_after_two_invalid_replies():
    with pytest.raises(TaskError, match="invalid JSON twice"):
        run({"path": "a.py", "content": "x\n"}, lambda prompt, schema=None: "nope")


def test_run_rejects_a_task_without_path_or_content():
    with pytest.raises(TaskError, match="needs a path and content"):
        run({"path": "a.py"}, lambda prompt, schema=None: "[]")
