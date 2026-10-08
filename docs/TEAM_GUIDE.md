# LapClusters team guide

What exists, how it works, how to test it, and what each person builds next.

## 1. Technologies

| Technology | Used for | Status |
|------------|----------|--------|
| Python 3.11+ | All project code | In use |
| Gemma 4 (`gemma4:e4b`) | The AI model on every laptop | In use |
| Ollama | Runs Gemma 4 locally and exposes it over HTTP on port 11434 | In use |
| Redis 7 | Shared task queue and task status store | In use |
| Redis Streams + consumer groups | Delivers each task to exactly one worker | In use |
| Docker Desktop | Runs Redis on the host laptop | In use |
| `redis` (redis-py) | Python client for Redis | In use |
| `httpx` | HTTP calls from the worker to Ollama | In use |
| `python-dotenv` | Loads settings from a `.env` file | In use |
| `pytest` | Automated tests | In use |
| Git + GitHub | Version control, pull requests, five merges | In use |
| FastAPI + `uvicorn` | Dashboard web server | Checkpoint 4, not built |
| HTML + JavaScript | Dashboard page | Checkpoint 4, not built |
| Tailscale | Fallback if venue Wi-Fi blocks laptop-to-laptop traffic | Only if needed |

## 2. What each file does

### `lapclusters/config.py`
Reads four settings from environment variables (or a `.env` file), with defaults:
`REDIS_URL`, `OLLAMA_URL`, `MODEL`, `WORKER_NAME` (defaults to the computer name).
Every other file imports settings from here.

### `lapclusters/taskqueue.py`
The only file that talks to Redis. It defines:

- `connect(url)` — opens the Redis connection. Always use this, never
  `redis.Redis(...)` directly: it sets a 60-second read timeout, without which an
  idle worker crashes after 5 seconds.
- Status constants `PENDING`, `RUNNING`, `DONE`, `FAILED`, and `FINISHED`.
- `Task` — one unit of work: `entry_id`, `task_id`, `job_id`, `payload`.
- `TaskQueue.ensure_group()` — creates the stream `tasks` and the consumer group
  `workers` if they do not exist. Safe to call repeatedly.
- `TaskQueue.add_task(job_id, payload)` — in one transaction: creates the hash
  `task:{task_id}` with status `pending`, adds the task id to the set
  `job:{job_id}`, and appends the task to the stream. Returns the `task_id`.
- `TaskQueue.job_tasks(job_id)` — returns the ids of every task in a job.
- `TaskQueue.claim(consumer, block_ms)` — waits up to `block_ms` for a task, marks
  it `running` with the worker's name, and returns it. Returns `None` if nothing arrived.
- `TaskQueue.complete(task, result)` — stores the result, sets status `done`,
  acknowledges the stream entry.
- `TaskQueue.fail(task, error)` — stores the error, sets status `failed`, acknowledges.
- `TaskQueue.get(task_id)` — returns the task's hash as a dictionary.

### `lapclusters/llm.py`
One function, `generate(prompt)`. It sends the prompt to the local Ollama server
and returns Gemma 4's reply. It raises the context size to 8192 tokens because
Ollama's default of 4096 is too small for source files.

### `lapclusters/worker.py`
- `process_one(queue, consumer, generate)` — claims one task, runs the model on
  its `prompt`, and marks it done or failed. A model error or a task with no
  prompt is recorded as `failed`; the worker never crashes on a bad task.
- `run_forever(queue, consumer, generate)` — calls `process_one` in a loop. If
  the Redis connection drops it waits 3 seconds and tries again.
- `main()` — connects to Redis and starts the loop. Stop it with Ctrl+C.
  Run with `python -m lapclusters.worker`.

### `lapclusters/repo.py`, `review.py`, `orchestrator.py`
Repository review. See section 6.

### `lapclusters/cli.py`
- `submit_and_wait(queue, prompt, timeout_s)` — adds one task and polls its
  status until it is `done` or `failed`. Raises `TimeoutError` if no worker
  finishes it in time.
- `main()` — the command-line wrapper. Run with `python -m lapclusters.cli "your prompt"`.

### `tests/`
`conftest.py` provides a `queue` fixture connected to Redis database 15, which it
empties before and after each test. Real work uses database 0, so tests never
touch real tasks. There are 65 tests.

## 3. How to test that it works

Start Redis once (host laptop):

```bash
docker run -d --name redis -p 6379:6379 redis:7
```

If the container already exists but is stopped:

```bash
docker start redis
```

Run the automated tests:

```bash
python -m pytest -v
```

Expected: `65 passed` in about 15 seconds. If every test fails with
`ConnectionError`, Redis is not running.

Run it for real, with two terminals. Terminal 1:

```bash
python -m lapclusters.worker
```

Terminal 2:

```bash
python -m lapclusters.orchestrator tests/fixtures/sample_repo
```

Expected: progress lines up to `3/3 files reviewed`, then a `review-report.md`
listing the bugs planted in the sample files. The first
request takes longer because Ollama has to load the model.

## 4. How to look inside Redis

Open the Redis command line inside the container:

```bash
docker exec -it redis redis-cli
```

Then type these one at a time:

| Command | Shows |
|---------|-------|
| `XLEN tasks` | How many tasks were ever added to the stream |
| `XRANGE tasks - +` | Every task in the stream with its payload |
| `XINFO GROUPS tasks` | The consumer group and how many tasks are unacknowledged |
| `XINFO CONSUMERS tasks workers` | Each worker that has claimed tasks |
| `XPENDING tasks workers` | Tasks claimed but not finished |
| `KEYS task:*` | Every task's status record |
| `HGETALL task:<task_id>` | One task's status, worker, result or error |
| `MONITOR` | Every command hitting Redis, live. Press Ctrl+C to stop |

`MONITOR` is the best way to watch it work: leave it running, then send a prompt
from another terminal and you will see the task added, claimed and completed.

For a graphical view, install Redis Insight and connect it to `localhost:6379`.

## 5. How multiple agents work

An "agent" here is a worker process on a laptop, with its own copy of Gemma 4.

- **Sending work.** One big request is not sent to every agent. The orchestrator
  splits it into many small tasks and adds each to the one shared stream.
- **Reading work.** Every worker reads from the same stream through the same
  consumer group, `workers`. Redis hands each task to exactly one worker, so no
  task is done twice.
- **Pull, not push.** A worker asks for the next task only when it is free. A
  fast laptop therefore takes more tasks than a slow one, with no scheduling code.
- **Writing results.** The worker writes its answer into the hash
  `task:{task_id}` and acknowledges the stream entry.
- **Collecting.** The orchestrator watches the task hashes and merges the
  results when all are `done` or `failed`.

Running more workers is a matter of starting the same command on more laptops
with `REDIS_URL` pointing at the host. See section 7.

## 6. How a repository gets reviewed

Run on the host laptop:

```bash
python -m lapclusters.orchestrator <folder-or-git-url>
```

**Where the repository lives.** Only the host laptop needs the repository and
internet access. A local folder is used as it is. A git URL is cloned with
`git clone --depth 1` into a temporary folder (under the system temp directory,
named `lapclusters-...`), which is deleted as soon as the files have been read.

**How it reaches other laptops.** `orchestrator.py` puts each file's path and
content inside its task. The content travels through Redis. Other laptops need
only a connection to Redis: no clone, no internet.

**How the work is split** (`repo.py`). One task per source file. This is a fixed
rule, not something the model decides.

- Skipped folders include `.git`, `node_modules`, `.venv`, `__pycache__`, `dist`, `build`.
- Only source files are kept (`.py`, `.js`, `.ts`, `.java`, `.go`, `.c`, `.cpp`,
  `.rs` and similar). Empty files are ignored.
- Files over 20 KB, files that are not UTF-8 text, and files that cannot be
  opened are listed in the report under "Not reviewed".

**What each worker is asked** (`review.py`). The file with numbered lines and an
instruction to report only real defects as JSON: `line`, `severity` (`high`,
`medium`, `low`) and `message`. Ollama is given a JSON schema so Gemma 4's reply
is constrained to that shape. If the reply is still unusable the worker asks
once more, then marks the task `failed`.

**How results are merged** (`orchestrator.py`). All findings go into one
Markdown report sorted by severity, then file, then line, with a count of files
reviewed by each worker.

## 7. Workers joining and leaving

- Every worker refreshes a heartbeat key `worker:{name}` every 5 seconds; the key
  expires after 15 seconds. `TaskQueue.workers()` lists the live ones.
- When a worker asks for work, it first checks for tasks held by a worker whose
  heartbeat has expired, and takes one over if it has been untouched for 10
  seconds. A slow worker that is still alive is never interrupted.
- A worker that restarts picks up the task it was holding when it stopped.
- A task abandoned by three workers in a row is marked `failed`, so a job always ends.

Checked so far with two workers on one laptop, killing one mid-job. Not yet run
across separate laptops.

### Connecting another laptop

1. Host: start Redis with a password (see `docs/SETUP.md`) and open port 6379 in
   the firewall.
2. Other laptop: clone the repository, install the requirements, pull the model.
3. Other laptop: create `.env` containing
   `REDIS_URL=redis://:PASSWORD@HOST_IP:6379/0`.
4. Other laptop: `python -m lapclusters.worker`.
5. Host: run the orchestrator. It prints the number of live workers, and the
   report's "Reviewed by" section shows how many files each laptop handled.

## What is left to build

Commit straight to `main`. Run `git pull --rebase origin main` before every push.

### Checkpoint 4: live dashboard

- Add `fastapi` and `uvicorn` to `requirements.txt`.
- `lapclusters/dashboard/app.py` with endpoints:
  - `/api/workers` from `TaskQueue.workers()`
  - `/api/jobs/{job_id}` from `TaskQueue.job_tasks()` and `TaskQueue.get_many()`
- One HTML page that polls those endpoints every second and shows live laptops,
  each file's status and which laptop has it, and files finished per minute.
- The orchestrator should print the job id so the dashboard can be pointed at it.

### Checkpoint 5: benchmark and polish

- `scripts/benchmark.py`: run the same review with one worker, then with all
  workers, and print both times. The orchestrator already prints elapsed seconds.
- Run it for real across the team's laptops and record the numbers.
- Finish every README section, add the `LICENSE` file, record the demo video.
- Fill in the Devpost link and tick the submission checklist.

### Ideas if there is time

- Split files over 20 KB into overlapping blocks of lines instead of skipping them.
- A second pass that asks the model to double-check each `high` finding, to cut
  false positives.

## Rules to remember

- Never commit `.env` or a Redis password.
- Do not put results, timings or features in the README until they exist.
- Run `python -m pytest` before every commit.
