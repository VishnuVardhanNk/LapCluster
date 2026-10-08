# How LapClusters works

For teammates who want to understand or change the code. For installing, see
[SETUP.md](SETUP.md).

## The idea in one paragraph

One laptop, the host, runs Redis. Every laptop runs a worker with its own copy
of the model. A job is split into tasks, the tasks go into a Redis stream, and
each worker takes a task whenever it is free. Results are written back to Redis.
The dashboard on each laptop only reads and writes Redis; the laptops never talk
to each other directly.

## The modules

| Module | What it does |
|--------|--------------|
| `config.py` | Reads settings from environment variables or `.env` |
| `taskqueue.py` | The only module that talks to Redis: tasks, jobs, heartbeats, live output, events |
| `llm.py` | Calls the local model server (Ollama, or llama.cpp when `MODEL_PROVIDER=llama_cpp`); turns its errors into a `LaptopProblem` |
| `worker.py` | The worker loop, readiness check, heartbeat thread, model switching |
| `repo.py` | Reads a folder or git URL: picks files, splits long ones, reads PDFs and pictures |
| `review.py` | The code review prompt and the parsing of its JSON reply |
| `ask.py` | The prompts for asking about one file and for combining many answers |
| `orchestrator.py` | Starts jobs of every kind, runs the combining stage, builds reports |
| `discovery.py`, `host.py` | Finding the host on the local network |
| `cli.py` | Sends one prompt from a terminal |
| `app/server.py` | The dashboard's local web server and its API |
| `app/node.py` | This laptop's connection, worker and host duties |
| `app/views.py` | Shapes what is in Redis for the page |
| `app/static/` | The page: `index.html`, `app.css`, `app.js` |

## What happens to a task

1. `add_task` writes its record, its data and a stream entry in one transaction.
2. A free worker claims it with `claim`. Redis gives each entry to one worker.
3. The worker runs the model. The reply is streamed to Redis in small pieces so
   the dashboard can show it live.
4. One of three things happens:
   - it worked: `complete` stores the result;
   - the task itself was bad (for example invalid JSON twice): `fail`;
   - the laptop could not run the model: `release` puts it back in the queue,
     and the laptop rests before trying again. After four hand-backs it is failed.
5. If the worker dies while holding it, its heartbeat expires after 15 seconds
   and the next free worker takes the task over. After three such deaths the
   task is failed.

## What is stored in Redis

| Key | Type | Holds |
|-----|------|-------|
| `work`, `work:vision` | stream | Queue entries. The second is for tasks that include a picture |
| `task:{id}` | hash | Status, laptop, model, times, result, raw reply, prompt, error |
| `payload:{id}` | string | The task's data, as JSON |
| `job:{id}` | set | The ids of a job's tasks |
| `jobmeta:{id}` | hash | A job's kind, source, question, status and summary |
| `jobs` | sorted set | Job ids by start time |
| `worker:{name}` | string, expires in 15 s | Heartbeat: model, installed models, readiness |
| `control:{name}` | list | Instructions for a worker, such as a model change |
| `out:{id}` | list | A task's live output while it runs |
| `events` | stream | The activity feed |

Real work uses Redis database 0. The tests use database 15.

## Looking inside Redis

```bash
docker exec -it redis redis-cli -a YOUR_PASSWORD
```

| Command | Shows |
|---------|-------|
| `KEYS worker:*` | Laptops that are alive |
| `XLEN work` | How many tasks have ever been queued |
| `XPENDING work workers` | Tasks claimed but not finished |
| `HGETALL task:<id>` | Everything about one task |
| `XREVRANGE events + - COUNT 10` | The latest activity |
| `MONITOR` | Every command, live. Press Ctrl+C to stop |

## Tests

```bash
python -m pytest
```

Redis must be running. There are 198 tests. The ones for the model and for a
whole cluster use stand-ins for Ollama, so they do not need a GPU.

## Rules

- Never commit `.env` or a password.
- Do not put results or timings in the README unless they were measured.
- Run the tests before every commit.
- Use `taskqueue.connect()` to open Redis, never `redis.Redis(...)` directly: it
  sets the read timeout the worker needs.
