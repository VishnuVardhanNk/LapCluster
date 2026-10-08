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

### `lapclusters/cli.py`
- `submit_and_wait(queue, prompt, timeout_s)` — adds one task and polls its
  status until it is `done` or `failed`. Raises `TimeoutError` if no worker
  finishes it in time.
- `main()` — the command-line wrapper. Run with `python -m lapclusters.cli "your prompt"`.

### `tests/`
`conftest.py` provides a `queue` fixture connected to Redis database 15, which it
empties before and after each test. Real work uses database 0, so tests never
touch real tasks. There are 21 tests across the three test files.

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

Expected: `21 passed` in about 11 seconds. If every test fails with
`ConnectionError`, Redis is not running.

Run it for real, with two terminals. Terminal 1:

```bash
python -m lapclusters.worker
```

Terminal 2:

```bash
python -m lapclusters.cli "Explain what a task queue is in one sentence."
```

Expected: an answer in terminal 2 and `Handled one task` in terminal 1. The first
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

Today only one worker has been run. Running more is a matter of starting the
same command on more laptops with `REDIS_URL` pointing at the host.

## 6. How a repository gets reviewed (checkpoint 2 design)

**Where the repository lives.** Only the host laptop needs the repository and
internet access. The orchestrator accepts either a local folder or a GitHub URL;
for a URL it runs `git clone --depth 1` into a temporary folder.

**How it reaches other laptops.** The orchestrator reads each file and puts the
file's path and content inside the task payload. The content travels through
Redis. Other laptops need only a connection to Redis: no clone, no internet.

**How the work is split.** One task per source file. This is a fixed rule, not
something the model decides, because small models split work unreliably.

- Skip folders: `.git`, `node_modules`, `.venv`, `venv`, `__pycache__`, `dist`, `build`.
- Keep only text source files (for example `.py`, `.js`, `.ts`, `.java`, `.go`,
  `.c`, `.cpp`, `.rs`, `.md`).
- Skip files larger than 20 KB so the file plus the prompt fits in the model's
  8192-token context. Skipped files are listed in the report as "not reviewed".
- Possible later improvement: split large files into overlapping blocks of lines
  instead of skipping them.

**What each worker is asked.** A review prompt containing the file path and
content, asking for JSON: a list of findings, each with `line`, `severity`
(`high`, `medium` or `low`) and `message`.

**How results are merged.** The orchestrator gathers all findings and writes one
Markdown report sorted by severity, then by file.

## 7. What to build next

Every checkpoint is one branch and one pull request into `main`. Each person
commits to the checkpoint's branch at least once an hour, under their own name.

### Checkpoint 2 — split and merge (branch `checkpoint-2`)

| Person | File | Builds |
|--------|------|--------|
| A | `lapclusters/repo.py` | `collect_files(root) -> list[tuple[str, str]]` returning (relative path, content) using the skip rules above; `fetch_repo(source) -> str` that returns a local path, cloning when given a URL |
| B | `lapclusters/review.py` | `build_prompt(path, content) -> str`; `parse_findings(text) -> list[dict]` that extracts the JSON and raises `ValueError` when it is invalid |
| C | `lapclusters/orchestrator.py` | `start_job(queue, files) -> str` adding one task per file; `wait_for_job(queue, job_id)`; `build_report(results) -> str`; command `python -m lapclusters.orchestrator <path-or-url>` |
| D | `tests/fixtures/sample_repo/`, tests, README | A tiny repository with known bugs to review; tests for A, B and C; README "How It Works" |

The queue already remembers which tasks belong to a job: use
`TaskQueue.job_tasks(job_id)` to list them and `TaskQueue.get(task_id)` to read
each one's status and result.

The worker must retry once when `parse_findings` raises, then mark the task `failed`.

### Checkpoint 3 — multi-laptop cluster (branch `checkpoint-3`)

- Worker heartbeat: key `worker:{name}` with a 15-second expiry, refreshed every 5 seconds.
- Reclaim stalled tasks: any worker takes over tasks idle for more than 120
  seconds using Redis `XAUTOCLAIM`.
- A task that fails three times is marked `failed` for good.
- Start Redis with a password, open port 6379 on the host's firewall, and put
  the host's address in every other laptop's `.env` as `REDIS_URL`.
- Demo: kill a worker mid-job and the review still completes.

### Checkpoint 4 — live dashboard (branch `checkpoint-4`)

- Add `fastapi` and `uvicorn` to `requirements.txt`.
- `lapclusters/dashboard/app.py`: endpoints `/api/workers`, `/api/jobs/{job_id}`.
- One HTML page that polls those endpoints every second and shows online
  laptops, each task's status, and tasks finished per minute.

### Checkpoint 5 — benchmark and polish (branch `checkpoint-5`)

- `scripts/benchmark.py`: run the same review with one worker, then with all
  workers, and print both times.
- Finish every README section, add the architecture diagram, record the demo video.
- Fill in the Devpost link and tick the submission checklist.

## 8. Rules to remember

- Never commit `.env` or a Redis password.
- Do not put results, timings or features in the README until they exist.
- Run `python -m pytest` before every commit.
