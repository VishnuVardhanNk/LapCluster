"""Asking a question about one file, and piecing many answers into one."""

from __future__ import annotations

import json
from typing import Callable

from lapclusters.taskqueue import TaskError

# Each per-file answer is cut to this length before answers are combined, so
# that every round of combining is certain to shrink the material.
NOTE_CHARS = 2400

TEXT_PROMPT = """You are examining one file from a larger collection in order to answer a question.

Question: {question}

Answer from this file only. Be specific and brief: at most 150 words. Quote names, \
numbers and line or page references where they matter. If this file does not help \
answer the question, reply with the single sentence "Nothing relevant in this file."
{pictures}{context}
File: {label}

{content}
"""

IMAGE_PROMPT = """You are examining one picture from a larger collection in order to answer a question.

Question: {question}

Answer from what this picture shows, in at most 120 words. Read out any text, labels \
or numbers in it that matter. If the picture does not help answer the question, reply \
with the single sentence "Nothing relevant in this picture."
{context}
Picture: {label}
"""

PICTURES_NOTE = """
The pictures attached to this request belong to this file (its figures or page images). \
Read them together with the text: the text may explain a picture, and a picture may \
show what the text only mentions.
"""

COMBINE_PROMPT = """Several files were each examined to answer one question. Their notes are below.

Question: {question}

Write {goal} using only these notes. Name the files a point comes from. Notes from \
files that belong together, such as a document and its figures or files in the same \
folder, should be read together and connected. If notes disagree, say so. Leave out \
files that had nothing relevant. Do not invent anything that is not in the notes.

{notes}
"""

FINAL_GOAL = "one complete answer to the question"
PARTIAL_GOAL = "a condensed summary that keeps every fact needed to answer the question"


def _label(payload: dict) -> str:
    part = payload.get("part")
    return f"{payload.get('path', 'file')} ({part})" if part else payload.get("path", "file")


def _context(payload: dict) -> str:
    context = (payload.get("context") or "").strip()
    return f"\nWhat surrounds it in the collection:\n{context}\n" if context else ""


def build_prompt(payload: dict) -> str:
    if payload.get("kind") == "image":
        return IMAGE_PROMPT.format(
            question=payload["question"], label=_label(payload), context=_context(payload)
        )
    return TEXT_PROMPT.format(
        question=payload["question"],
        label=_label(payload),
        content=payload.get("content", ""),
        pictures=PICTURES_NOTE if payload.get("images") else "",
        context=_context(payload),
    )


def run(payload: dict, generate: Callable[..., str]) -> tuple[str, dict[str, str]]:
    """Answer the question for one file. `generate(prompt, images=...)`."""
    if "question" not in payload or "path" not in payload:
        raise TaskError("this task needs a question and a file")
    images = payload.get("images") or []
    if payload.get("kind") == "image" and not images:
        raise TaskError("this picture task has no picture")
    prompt = build_prompt(payload)
    answer = (generate(prompt, images=images) if images else generate(prompt)).strip()
    details = {"prompt": prompt, "raw": answer}
    if not answer:
        raise TaskError("the model returned an empty answer", details)
    return answer, details


def plan_batches(notes: list[tuple[str, str]], budget_chars: int) -> list[list[tuple[str, str]]]:
    """Group notes so each group fits in one request. One group means the
    next request can be the final answer."""
    batches: list[list[tuple[str, str]]] = [[]]
    used = 0
    for label, text in notes:
        size = len(label) + min(len(text), NOTE_CHARS) + 12
        if batches[-1] and used + size > budget_chars:
            batches.append([])
            used = 0
        batches[-1].append((label, text[:NOTE_CHARS]))
        used += size
    return batches


def build_combine_prompt(question: str, notes: list[tuple[str, str]], final: bool) -> str:
    body = "\n\n".join(f"### {label}\n{text.strip()}" for label, text in notes)
    return COMBINE_PROMPT.format(
        question=question, notes=body, goal=FINAL_GOAL if final else PARTIAL_GOAL
    )


def run_combine(payload: dict, generate: Callable[..., str]) -> tuple[str, dict[str, str]]:
    try:
        notes = [(str(label), str(text)) for label, text in json.loads(payload["notes"])]
    except (KeyError, ValueError, TypeError) as exc:
        raise TaskError("this combining task has no notes to combine") from exc
    if not notes:
        raise TaskError("this combining task has no notes to combine")
    prompt = build_combine_prompt(
        payload.get("question", ""), notes, payload.get("final") == "1"
    )
    answer = generate(prompt).strip()
    details = {"prompt": prompt, "raw": answer}
    if not answer:
        raise TaskError("the model returned an empty answer", details)
    return answer, details
