"""Reviewing one source file: the prompt, and reading the model's reply."""

from __future__ import annotations

import json
import re
from typing import Callable

from lapclusters.taskqueue import TaskError

SEVERITIES = ("high", "medium", "low")

# Passed to Ollama so the model is constrained to this shape.
FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line": {"type": "integer"},
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "message": {"type": "string"},
                },
                "required": ["line", "severity", "message"],
            },
        }
    },
    "required": ["findings"],
}

PROMPT = """You are reviewing one source file for real defects.

Report only problems that would cause wrong results, crashes, security holes, \
data loss or resource leaks. Do not report style, naming, formatting or missing comments.

Reply with JSON only, in this shape:
{{"findings": [{{"line": <line number>, "severity": "high" | "medium" | "low", \
"message": "<one sentence saying what is wrong and why>"}}]}}

Use the line numbers shown at the start of each line. If the file has no real \
defects, reply {{"findings": []}}.

File: {path}

{numbered}
"""

_CODE_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def build_prompt(path: str, content: str, first_line: int = 1, part: str = "") -> str:
    """`first_line` is the file's line number of the first line of `content`,
    so that a part of a long file is numbered as it is in the whole file."""
    numbered = "\n".join(
        f"{number} | {line}"
        for number, line in enumerate(content.splitlines(), start=first_line)
    )
    label = f"{path} ({part} of a longer file; judge only what is shown)" if part else path
    return PROMPT.format(path=label, numbered=numbered)


def _line_number(value) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def parse_findings(text: str) -> list[dict]:
    """Turn the model's reply into a clean list of findings, or raise ValueError."""
    cleaned = text.strip()
    fenced = _CODE_FENCE.match(cleaned)
    if fenced:
        cleaned = fenced.group(1)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"reply is not JSON ({exc.msg})") from exc
    if isinstance(data, dict):
        data = data.get("findings")
    if not isinstance(data, list):
        raise ValueError("reply has no list of findings")

    findings = []
    for item in data:
        if not isinstance(item, dict):
            continue
        message = str(item.get("message") or "").strip()
        if not message:
            continue
        severity = str(item.get("severity") or "").lower()
        findings.append(
            {
                "line": _line_number(item.get("line")),
                "severity": severity if severity in SEVERITIES else "medium",
                "message": message,
            }
        )
    return findings


def run(payload: dict[str, str], generate: Callable[..., str]) -> str:
    """Review the file in a task payload and return its findings as a JSON list."""
    return run_detailed(payload, generate)[0]


def run_detailed(
    payload: dict[str, str], generate: Callable[..., str]
) -> tuple[str, dict[str, str]]:
    """Like run(), and also return what to keep for inspection: the exact
    prompt, the model's raw reply, and a count of findings per severity."""
    if "path" not in payload or "content" not in payload:
        raise TaskError("review task needs a path and content")
    try:
        first_line = int(payload.get("first_line") or 1)
    except (TypeError, ValueError):
        first_line = 1
    prompt = build_prompt(payload["path"], payload["content"], first_line, payload.get("part", ""))
    details = {"prompt": prompt, "raw": ""}
    problem = ""
    for _attempt in range(2):
        details["raw"] = generate(prompt, FINDINGS_SCHEMA)
        try:
            findings = parse_findings(details["raw"])
        except ValueError as exc:
            problem = str(exc)
            continue
        for level in SEVERITIES:
            details[f"count_{level}"] = str(sum(1 for f in findings if f["severity"] == level))
        return json.dumps(findings), details
    raise TaskError(f"model returned invalid JSON twice: {problem}", details)
