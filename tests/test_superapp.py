"""Handing work back, lanes for pictures, long files, PDFs, pictures with
their text, and the jobs that answer a question across many files."""

import base64
import io
import json

import pytest
from PIL import Image

from lapclusters import ask, config, llm, orchestrator
from lapclusters.app import views
from lapclusters.llm import LaptopProblem
from lapclusters.orchestrator import (
    advance, build_answer_report, build_report, fill_job, job_answer, prepare_job, retry_job,
    start_job,
)
from lapclusters.repo import ASK, SourceFile, collect_files, encode_image
from lapclusters.taskqueue import MAX_ATTEMPTS
from lapclusters.worker import Runtime, process_one, run_forever


def picture_bytes(colour="red", size=(40, 30)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, format="PNG")
    return out.getvalue()


def pdf_bytes(text: str) -> bytes:
    """A one-page PDF containing `text`, written by hand so no PDF library is
    needed to make test files."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return out


def write(root, relative, content):
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")


# --- handing tasks back ------------------------------------------------------


def test_released_task_goes_to_another_laptop(queue):
    task_id = queue.add_task("job1", {"prompt": "hi"}, name="a.py")
    task = queue.claim("broken-pc", block_ms=100)
    assert queue.release(task, "broken-pc could not run it: Ollama is not running") is True
    info = queue.get(task_id)
    assert (info["status"], info["attempts"]) == ("pending", "1")
    assert "Ollama is not running" in info["last_error"]
    assert "started_at" not in info
    again = queue.claim("good-pc", block_ms=100)
    assert again.task_id == task_id
    assert queue.client.xpending(queue.stream, queue.group)["pending"] == 1


def test_task_nobody_can_run_is_failed_after_a_few_attempts(queue):
    task_id = queue.add_task("job1", {"prompt": "hi"})
    for attempt in range(1, MAX_ATTEMPTS + 1):
        task = queue.claim("pc", block_ms=100)
        still_queued = queue.release(task, "pc could not run it: model missing")
        assert still_queued is (attempt < MAX_ATTEMPTS)
    info = queue.get(task_id)
    assert info["status"] == "failed"
    assert info["error"] == f"pc could not run it: model missing. Gave up after {MAX_ATTEMPTS} attempts."
    assert queue.claim("pc", block_ms=100) is None


def test_payload_is_kept_apart_from_the_queue_entry(queue):
    task_id = queue.add_task("job1", {"prompt": "x" * 5000})
    entries = queue.client.xrange(queue.stream)
    assert entries[-1][1] == {"task_id": task_id, "job_id": "job1"}
    assert queue.claim("w1", block_ms=100).payload == {"prompt": "x" * 5000}


def test_entry_written_by_an_older_version_still_runs(queue):
    queue.client.hset("task:old1", mapping={"status": "pending", "job_id": "j", "name": "a"})
    queue.client.xadd(queue.stream, {"task_id": "old1", "job_id": "j", "payload": '{"prompt": "hi"}'})
    task = queue.claim("w1", block_ms=100)
    assert (task.task_id, task.payload) == ("old1", {"prompt": "hi"})


def test_task_whose_data_was_deleted_is_failed_not_lost(queue):
    task_id = queue.add_task("job1", {"prompt": "hi"})
    queue.client.delete(f"payload:{task_id}")
    assert queue.claim("w1", block_ms=100) is None
    assert queue.get(task_id)["error"] == "the task's data is missing"
    assert queue.client.xpending(queue.stream, queue.group)["pending"] == 0


def test_retry_puts_failed_tasks_back_and_leaves_the_rest(queue):
    good = queue.add_task("job1", {"prompt": "a"})
    queue.complete(queue.claim("w1", block_ms=100), "answer")
    bad = queue.add_task("job1", {"prompt": "b"}, lane="vision")
    queue.fail(queue.claim("w1", block_ms=100, lanes=("", "vision")), "boom", raw="junk")
    assert queue.retry_failed("job1") == 1
    info = queue.get(bad)
    assert (info["status"], info["attempts"]) == ("pending", "0")
    assert "error" not in info and "raw" not in info and "finished_at" not in info
    assert queue.get(good)["status"] == "done"
    assert queue.claim("w1", block_ms=100) is None  # it went back to the lane it came from
    assert queue.claim("w1", block_ms=100, lanes=("", "vision")).task_id == bad


def test_delete_job_removes_every_trace(queue):
    queue.create_job("job1", source="x", status="running")
    task_id = queue.add_task("job1", {"prompt": "a"})
    queue.append_output(task_id, "text")
    queue.delete_job("job1")
    assert queue.recent_jobs() == []
    assert queue.job_meta("job1") == {} and queue.job_tasks("job1") == []
    assert queue.get(task_id) == {} and queue.read_output(task_id) == ("", 0)
    assert queue.claim("w1", block_ms=100) is None


# --- lanes -------------------------------------------------------------------


def test_picture_work_only_reaches_laptops_that_can_see(queue):
    seeing = queue.add_task("job1", {"prompt": "look"}, lane="vision")
    plain = queue.add_task("job1", {"prompt": "read"})
    assert queue.claim("blind-pc", block_ms=100).task_id == plain
    assert queue.claim("blind-pc", block_ms=100) is None
    task = queue.claim("seeing-pc", block_ms=100, lanes=("", "vision"))
    assert (task.task_id, task.lane) == (seeing, "vision")
    queue.complete(task, "a cat")
    assert queue.get(seeing)["status"] == "done"
    assert queue.client.xpending("work:vision", queue.group)["pending"] == 0


def test_reading_two_lanes_never_strands_a_task(queue):
    first = queue.add_task("job1", {"prompt": "a"})
    second = queue.add_task("job1", {"prompt": "b"}, lane="vision")
    taken = queue.claim("pc-1", block_ms=100, lanes=("", "vision"))
    other = queue.claim("pc-2", block_ms=100, lanes=("", "vision"))
    assert {taken.task_id, other.task_id} == {first, second}


def test_dead_laptops_picture_task_is_only_taken_over_by_one_that_can_see(queue):
    queue.reclaim_idle_ms = 0
    task_id = queue.add_task("job1", {"prompt": "look"}, lane="vision")
    queue.claim("dead-pc", block_ms=100, lanes=("", "vision"))
    assert queue.claim("blind-pc", block_ms=100) is None
    assert queue.claim("seeing-pc", block_ms=100, lanes=("", "vision")).task_id == task_id


# --- a laptop that cannot run its model ---------------------------------------


def broken_model(prompt, schema=None):
    raise LaptopProblem("the model gemma4:e4b is not installed in Ollama")


def test_a_laptop_problem_hands_the_file_back_instead_of_failing_it(queue):
    task_id = queue.add_task("job1", {"prompt": "hi"}, name="a.py")
    with pytest.raises(LaptopProblem):
        process_one(queue, "broken-pc", broken_model, block_ms=100)
    info = queue.get(task_id)
    assert info["status"] == "pending"
    assert info["last_error"] == (
        "broken-pc could not run it: the model gemma4:e4b is not installed in Ollama"
    )
    assert "handed a.py back to the queue" in queue.events()[0]["text"]
    process_one(queue, "good-pc", lambda prompt: "answer", block_ms=100)
    assert queue.get(task_id)["status"] == "done"


class _StopAfter:
    def __init__(self, loops):
        self.left = loops

    def __call__(self):
        self.left -= 1
        return self.left < 0


@pytest.fixture
def ollama(monkeypatch):
    """A pretend Ollama whose installed models a test can change."""
    state = {"models": ["model-a"], "sees": {"model-a": False, "model-b": True}}
    monkeypatch.setattr(llm, "list_models", lambda timeout=5.0: list(state["models"]))
    monkeypatch.setattr(llm, "can_see", lambda model, timeout=5.0: state["sees"].get(model, False))
    monkeypatch.setattr(config, "MODEL", "model-a")
    return state


def test_a_laptop_without_its_model_takes_no_work_and_says_why(queue, ollama):
    ollama["models"] = ["model-b"]
    task_id = queue.add_task("job1", {"prompt": "hi"})
    runtime = Runtime("pc")
    runtime.refresh_models()
    run_forever(
        queue, "pc", lambda prompt: "answer", should_stop=_StopAfter(2), block_ms=100, runtime=runtime
    )
    assert queue.get(task_id)["status"] == "pending"  # untouched, free for another laptop
    assert "model-a is not installed on this laptop" in runtime.problem()
    assert "model-b" in runtime.problem()
    assert json.loads(runtime.info())["ready"] is False


def test_work_resumes_by_itself_once_the_model_is_there(queue, ollama):
    ollama["models"] = []
    task_id = queue.add_task("job1", {"prompt": "hi"})
    runtime = Runtime("pc")
    runtime.refresh_models()
    ollama["models"] = ["model-a"]  # the owner pulls the model
    run_forever(
        queue, "pc", lambda prompt: "answer", should_stop=_StopAfter(1), block_ms=100, runtime=runtime
    )
    assert queue.get(task_id)["status"] == "done"
    assert runtime.problem() == ""


def test_after_a_failed_run_the_laptop_rests_then_tries_again(queue, ollama):
    calls = []

    def flaky(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            raise LaptopProblem("Ollama could not run model-a (out of memory)")
        return "answer"

    task_id = queue.add_task("job1", {"prompt": "hi"})
    runtime = Runtime("pc")
    runtime.refresh_models()
    run_forever(
        queue, "pc", flaky, block_ms=100, runtime=runtime, cooldown_s=0,
        should_stop=lambda: queue.get(task_id).get("status") == "done" or len(calls) > 5,
    )
    info = queue.get(task_id)
    assert (info["status"], info["attempts"], info["result"]) == ("done", "1", "answer")
    assert runtime.trouble == ""


def test_only_a_model_that_can_see_reads_the_picture_lane(ollama):
    runtime = Runtime("pc")
    runtime.refresh_models()
    assert runtime.lanes() == ("",) and runtime.vision is False
    ollama["models"] = ["model-a", "model-b"]
    assert runtime.set_model("model-b") == ""
    assert runtime.lanes() == ("", "vision")
    assert json.loads(runtime.info())["vision"] is True


def test_the_hosts_context_size_travels_with_the_task(queue):
    seen = {}

    def model(prompt, schema=None, context=None, images=None):
        seen.update(context=context, images=images)
        return "answer"

    queue.add_task("job1", {"prompt": "hi", "ctx": "16384"})
    process_one(queue, "w1", model, block_ms=100)
    assert seen == {"context": 16384, "images": None}


# --- documents, PDFs and pictures --------------------------------------------


def test_review_mode_takes_source_code_only(tmp_path):
    write(tmp_path, "main.py", "x = 1\n")
    write(tmp_path, "notes.md", "# Notes\n")
    write(tmp_path, "shot.png", picture_bytes())
    write(tmp_path, "paper.pdf", pdf_bytes("Hello"))
    assert [f.path for f in collect_files(tmp_path).files] == ["main.py"]


def test_ask_mode_reads_documents_pdfs_and_pictures(tmp_path):
    write(tmp_path, "main.py", "x = 1\n")
    write(tmp_path, "notes.md", "# Notes\n")
    write(tmp_path, "shot.png", picture_bytes())
    write(tmp_path, "paper.pdf", pdf_bytes("Quarterly revenue rose 12 percent"))
    collected = collect_files(tmp_path, mode=ASK)
    by_path = {f.path: f for f in collected.files}
    assert sorted(by_path) == ["main.py", "notes.md", "paper.pdf", "shot.png"]
    assert collected.skipped == []
    assert "[page 1]" in by_path["paper.pdf"].content
    assert "Quarterly revenue rose 12 percent" in by_path["paper.pdf"].content
    picture = by_path["shot.png"]
    assert picture.kind == "image" and picture.content == "" and len(picture.images) == 1
    assert Image.open(io.BytesIO(base64.b64decode(picture.images[0]))).format == "JPEG"


def test_a_document_carries_the_pictures_it_refers_to(tmp_path):
    write(tmp_path, "report/figures/sales.png", picture_bytes("blue"))
    write(tmp_path, "report/readme.md", "# Sales\n\n![Sales by month](figures/sales.png)\n\nSales peaked in March.\n")
    by_path = {f.path: f for f in collect_files(tmp_path, mode=ASK).files}
    document = by_path["report/readme.md"]
    assert len(document.images) == 1
    assert document.kind == "text" and "Sales peaked in March" in document.content


def test_a_picture_is_told_what_the_text_beside_it_says(tmp_path):
    write(tmp_path, "sales.png", picture_bytes("blue"))
    write(tmp_path, "notes.md", "The chart sales.png shows revenue by month; March was the peak.\n")
    by_path = {f.path: f for f in collect_files(tmp_path, mode=ASK).files}
    context = by_path["sales.png"].context
    assert "notes.md refers to this picture" in context
    assert "March was the peak" in context
    assert "Other files in the same folder: notes.md" in context


def test_large_pictures_are_shrunk_before_travelling(tmp_path):
    big = encode_image(picture_bytes(size=(4000, 3000)))
    assert max(Image.open(io.BytesIO(base64.b64decode(big))).size) == 1280
    assert encode_image(b"this is not a picture") is None


def test_unreadable_documents_are_listed_not_fatal(tmp_path):
    write(tmp_path, "broken.png", b"not a picture")
    write(tmp_path, "broken.pdf", b"not a pdf")
    write(tmp_path, "empty.pdf", pdf_bytes(""))
    write(tmp_path, "good.md", "fine\n")
    collected = collect_files(tmp_path, mode=ASK)
    assert [f.path for f in collected.files] == ["good.md"]
    assert dict(collected.skipped) == {
        "broken.png": "could not be read as a picture",
        "broken.pdf": "could not be read as a PDF",
        "empty.pdf": "no text could be extracted (a scanned PDF?)",
    }


def test_without_a_laptop_that_can_see_pictures_are_left_out_and_said_so(tmp_path):
    write(tmp_path, "fig.png", picture_bytes())
    write(tmp_path, "doc.md", "![x](fig.png)\n")
    collected = collect_files(tmp_path, mode=ASK, pictures=False)
    assert [(f.path, f.images) for f in collected.files] == [("doc.md", [])]
    assert collected.skipped == [("fig.png", "no laptop in the cluster has a model that can see")]


def test_a_document_cannot_pull_in_a_picture_from_outside_the_collection(tmp_path):
    write(tmp_path, "secret.png", picture_bytes())
    write(tmp_path, "docs/readme.md", "![x](../../secret.png) ![y](../secret.png) ![z](https://x.test/a.png)\n")
    by_path = {f.path: f for f in collect_files(tmp_path / "docs", mode=ASK).files}
    assert by_path["docs/readme.md".removeprefix("docs/")].images == []


# --- asking a question ---------------------------------------------------------


def test_ask_sends_a_documents_pictures_with_its_text():
    seen = {}

    def model(prompt, images=None):
        seen.update(prompt=prompt, images=images)
        return "  Sales peaked in March.  "

    payload = {"type": "ask", "kind": "text", "path": "readme.md", "content": "See the chart.",
               "images": ["QUJD"], "question": "When did sales peak?", "context": ""}
    answer, details = ask.run(payload, model)
    assert answer == "Sales peaked in March."
    assert seen["images"] == ["QUJD"]
    assert "When did sales peak?" in seen["prompt"] and "See the chart." in seen["prompt"]
    assert "pictures attached to this request belong to this file" in seen["prompt"]
    assert details["raw"] == "Sales peaked in March."


def test_ask_about_a_picture_includes_its_surroundings():
    payload = {"type": "ask", "kind": "image", "path": "sales.png", "images": ["QUJD"],
               "question": "What does it show?", "context": "notes.md refers to this picture."}
    prompt = ask.build_prompt(payload)
    assert "Picture: sales.png" in prompt
    assert "notes.md refers to this picture." in prompt


def test_ask_rejects_broken_tasks_and_empty_answers():
    from lapclusters.taskqueue import TaskError

    with pytest.raises(TaskError, match="needs a question and a file"):
        ask.run({"type": "ask", "path": "a"}, lambda prompt: "x")
    with pytest.raises(TaskError, match="has no picture"):
        ask.run({"type": "ask", "kind": "image", "path": "a.png", "question": "q"}, lambda prompt: "x")
    with pytest.raises(TaskError, match="empty answer"):
        ask.run({"type": "ask", "path": "a", "question": "q", "content": "c"}, lambda prompt: "  ")


def test_notes_are_grouped_to_fit_and_long_ones_trimmed():
    notes = [(f"file{n}.md", "x" * 5000) for n in range(5)]
    batches = ask.plan_batches(notes, budget_chars=6000)
    assert [len(batch) for batch in batches] == [2, 2, 1]
    assert all(len(text) == ask.NOTE_CHARS for batch in batches for _, text in batch)
    assert ask.plan_batches(notes[:1], budget_chars=10) == [[("file0.md", "x" * ask.NOTE_CHARS)]]


# --- jobs that answer across files ---------------------------------------------


def cluster_model(prompt, schema=None, images=None, context=None):
    """Stands in for every laptop's model across a whole job."""
    if schema is not None:
        return '{"findings": [{"line": 5, "severity": "high", "message": "Stub."}]}'
    if "Several files were each examined" in prompt:
        labels = [line[4:] for line in prompt.splitlines() if line.startswith("### ")]
        return "COMBINED " + " + ".join(labels)
    if "Picture:" in prompt:
        return f"picture answer ({len(images or [])} picture)"
    if "careful programmer" in prompt:
        return "```python\nprint('hi')\n```"
    return "text answer"


def work(queue, lanes=("", "vision"), model=cluster_model):
    while process_one(queue, "w1", model, block_ms=100, lanes=lanes):
        pass


@pytest.fixture
def collection(tmp_path):
    write(tmp_path, "notes.md", "March was the best month.\n")
    write(tmp_path, "sales.png", picture_bytes())
    write(tmp_path, "paper.pdf", pdf_bytes("Revenue rose 12 percent"))
    return tmp_path


def test_a_question_is_answered_per_file_then_combined(queue, collection):
    queue.heartbeat("w1", json.dumps({"vision": True}))
    job_id = prepare_job(queue, str(collection), "host", kind="ask", question="What happened?")
    fill_job(queue, job_id, str(collection))
    assert queue.job_meta(job_id)["total"] == "3"

    advance(queue, job_id)  # nothing is finished yet, so nothing is combined
    assert "answer_task" not in queue.job_meta(job_id)
    work(queue)
    view = views.job_view(queue, job_id, close=True)
    assert view["status"] == "running" and view["combining"] is True  # answered, not yet combined

    advance(queue, job_id)
    advance(queue, job_id)  # a second call must not queue a second combining task
    assert len(queue.job_tasks(job_id)) == 4
    work(queue)
    view = views.job_view(queue, job_id, close=True)
    assert view["status"] == "done"
    assert view["answer"] == "COMBINED notes.md + paper.pdf + sales.png"
    assert job_answer(queue, job_id) == view["answer"]
    assert [t["name"] for t in view["tasks"]][-1] == "Combined answer"

    picture = next(t for t in view["tasks"] if t["name"] == "sales.png")
    assert picture["lane"] == "vision"
    assert queue.get(picture["id"])["result"] == "picture answer (1 picture)"

    found = views.findings_view(queue, job_id)
    assert [n["file"] for n in found["notes"]] == ["notes.md", "paper.pdf", "sales.png"]
    assert found["findings"] == []


def test_many_answers_are_condensed_in_rounds(queue, tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "_combine_budget", lambda: 3000)
    for n in range(6):
        write(tmp_path, f"doc{n}.md", f"document {n}\n")
    job_id = prepare_job(queue, str(tmp_path), kind="ask", question="Summarise.")
    fill_job(queue, job_id, str(tmp_path))
    work(queue, model=lambda prompt, schema=None: "x" * 1300 if "Several files" not in prompt else "condensed")

    advance(queue, job_id)
    meta = queue.job_meta(job_id)
    first_round = json.loads(meta["combine_ids"])
    assert len(first_round) == 3 and "answer_task" not in meta
    assert queue.get(first_round[0])["name"] == "Summary of group 1 (round 1)"

    work(queue, model=lambda prompt, schema=None: "x" * 1300 if "Several files" not in prompt else "condensed")
    advance(queue, job_id)
    final = queue.job_meta(job_id)["answer_task"]
    assert queue.get(final)["name"] == "Combined answer"
    work(queue, model=lambda prompt, schema=None: "FINAL")
    assert views.job_view(queue, job_id)["answer"] == "FINAL"
    assert views.job_view(queue, job_id)["status"] == "done"


def test_a_question_nobody_could_answer_ends_without_an_answer(queue, collection):
    job_id = prepare_job(queue, str(collection), kind="ask", question="What happened?")
    fill_job(queue, job_id, str(collection))
    queue.cancel_job(job_id)
    advance(queue, job_id)
    view = views.job_view(queue, job_id)
    assert (view["status"], view["answer"], view["answer_task"]) == ("done", "", "")


def test_retrying_a_question_redoes_the_combining(queue, tmp_path):
    write(tmp_path, "a.md", "alpha\n")
    write(tmp_path, "b.md", "beta\n")
    job_id = prepare_job(queue, str(tmp_path), kind="ask", question="Q?")
    fill_job(queue, job_id, str(tmp_path))

    def half(prompt, schema=None):
        if "b.md" in prompt and "Several files" not in prompt:
            raise RuntimeError("boom")
        return cluster_model(prompt)

    work(queue, model=half)
    advance(queue, job_id)
    work(queue, model=half)
    assert views.job_view(queue, job_id, close=True)["answer"] == "COMBINED a.md"

    assert retry_job(queue, job_id) == 1
    view = views.job_view(queue, job_id)
    assert (view["status"], view["answer"], view["total"]) == ("running", "", 2)
    work(queue)
    advance(queue, job_id)
    work(queue)
    assert views.job_view(queue, job_id)["answer"] == "COMBINED a.md + b.md"


def test_prompt_and_code_jobs_are_a_single_task(queue):
    prompt_job = prepare_job(queue, "", kind="prompt", question="What is a queue?")
    fill_job(queue, prompt_job, "")
    code_job = prepare_job(queue, "", kind="code", question="Reverse a string in Python")
    fill_job(queue, code_job, "")
    prompts = {}

    def model(prompt, schema=None):
        prompts[len(prompts)] = prompt
        return cluster_model(prompt)

    work(queue, model=model)
    assert views.job_view(queue, prompt_job)["answer"] == "text answer"
    assert views.job_view(queue, prompt_job)["status"] == "done"
    assert views.job_view(queue, code_job)["answer"].startswith("```python")
    assert prompts[0] == "What is a queue?"
    assert "Request: Reverse a string in Python" in prompts[1] and "careful programmer" in prompts[1]


def test_no_readable_files_fails_the_job_with_the_reason(queue, tmp_path):
    from lapclusters.repo import RepoError

    write(tmp_path, "data.bin", b"\x00\x01")
    job_id = prepare_job(queue, str(tmp_path), kind="ask", question="Q?")
    with pytest.raises(RepoError, match="No files this kind of job can read"):
        fill_job(queue, job_id, str(tmp_path))
    assert queue.job_meta(job_id)["status"] == "failed"


def test_review_of_a_split_file_reports_each_line_once(queue, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PART_CHARS", 600)
    write(tmp_path, "big.py", "\n".join(f"value_{n} = {n}" for n in range(1, 121)) + "\n")
    job_id = prepare_job(queue, str(tmp_path))
    collected = fill_job(queue, job_id, str(tmp_path))
    assert len(collected.files) > 1
    prompts = []

    def model(prompt, schema=None):
        prompts.append(prompt)
        return cluster_model(prompt, schema)

    work(queue, model=model)
    # Each part is numbered as in the whole file, and says it is a part.
    assert any("\n40 | value_40 = 40" in p and "of a longer file" in p for p in prompts)
    rows = views.report_rows(queue, job_id)
    assert len(rows) == len(collected.files)
    report = build_report("src", rows, [])
    assert report.count("| high | `big.py` | 5 |") == 1  # reported by every part, listed once
    assert len(views.findings_view(queue, job_id)["findings"]) == 1
    assert views.job_view(queue, job_id)["tasks"][0]["file"] == "big.py"


def test_answer_report_shows_the_answer_and_where_it_came_from():
    infos = {
        "t1": {"status": "done", "type": "ask", "name": "a.md", "result": "Alpha.", "worker": "pc-1", "model": "m"},
        "t2": {"status": "failed", "type": "ask", "name": "b.md", "error": "boom"},
        "t3": {"status": "done", "type": "combine", "name": "Combined answer", "result": "Final.", "worker": "pc-1", "model": "m"},
    }
    report = build_answer_report("docs", "What?", "Final.", infos, [("c.pdf", "scanned")])
    assert "- Question: What?" in report and "## Answer\n\nFinal." in report
    assert "### a.md\n\nAlpha." in report
    assert "- `b.md`: boom" in report and "- `c.pdf`: scanned" in report
    assert "Combined answer" not in report.split("## What each file contributed")[1].split("## Not included")[0]


def test_start_job_marks_picture_tasks_for_the_seeing_lane(queue):
    files = [SourceFile("a.md", "text"), SourceFile("b.png", kind="image", images=["QUJD"])]
    job_id = start_job(queue, files, kind="ask", question="Q?")
    rows = queue.get_fields(queue.job_tasks(job_id), ["name", "lane", "type"])
    assert sorted((r["name"], r["lane"], r["type"]) for r in rows.values()) == [
        ("a.md", "", "ask"), ("b.png", "vision", "ask"),
    ]
