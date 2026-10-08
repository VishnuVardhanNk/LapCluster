# Team setup

Do this before the Hack Day. Installing tools and downloading the model ahead of
time is allowed; the project code itself must be written during the event.

## What we are building

**Problem.** Small teams and students want to use AI on their own code, but cloud
AI costs money per request and means sending private code to someone else's
servers. A single laptop running a local model is private and free, but slow:
reviewing a whole repository file by file can take a very long time.

**Our answer.** Pool the team's laptops into a private AI cluster. Every laptop
runs its own copy of Gemma 4, and a shared Redis queue hands out work. The demo
job is a whole-repository code review: one task per file, done in parallel, then
merged into one report. We show the time on one laptop versus four.

**One-line pitch.** Turn the laptops you already own into a private AI cluster,
with no cloud bill and no code leaving the room.

**Tracks we enter.** Main track (Best Open-Source AI Project) and the optional
Gemma 4 challenge. Gemma 4 is the model every worker runs.

## Check your laptop first

- Memory: 16 GB recommended for `gemma4:e4b`. With 8 GB, use `gemma4:e2b`.
- Free disk: about 15 GB.
- Tell the team which model your laptop can run.

## Everyone installs

### 1. Git, with your own identity

Download: https://git-scm.com/downloads

Commit history is reviewed, so each person must commit under their own name and
the email tied to their GitHub account.

```bash
git config --global user.name "Your Name"
git config --global user.email "your-github-email@example.com"
```

### 2. Python 3.11 or newer

Download: https://www.python.org/downloads/ (on Windows, tick "Add Python to PATH").

```bash
python --version
```

### 3. Ollama and the Gemma 4 model

Download: https://ollama.com/download (use a recent version, 0.22.0 or newer).

```bash
ollama pull gemma4:e4b
```

The download is several gigabytes, so do it on good Wi-Fi. Then check it answers:

```bash
ollama run gemma4:e4b "Say hello in one sentence."
```

### 4. A GitHub account

Send your GitHub username to the team lead so you can be added to the repository.

### 5. A code editor

VS Code is fine: https://code.visualstudio.com/

### 6. Tailscale (network fallback)

Download: https://tailscale.com/download

Only needed if the venue Wi-Fi stops laptops from reaching each other. Everyone
signs in to the same Tailscale network.

## Host laptop only

One laptop runs Redis, the orchestrator and the dashboard.

### 7. Docker Desktop

Download: https://www.docker.com/products/docker-desktop/

### 8. Start Redis with a password

Pick a password and share it with the team privately, not in the repository.

```bash
docker run -d --name redis -p 6379:6379 redis:7 redis-server --requirepass CHOOSE_A_PASSWORD
```

### 9. Allow other laptops to reach Redis

Windows Firewall must allow inbound connections on TCP port 6379 for the network
you are on. Windows may prompt for this when Docker starts; otherwise add an
inbound rule for port 6379 in "Windows Defender Firewall with Advanced Security".

Find the host laptop's IP address:

```bash
ipconfig
```

## Connectivity test (do this at the venue, early)

From each other laptop, with the host's IP address:

Windows PowerShell:

```bash
Test-NetConnection HOST_IP -Port 6379
```

macOS or Linux:

```bash
nc -vz HOST_IP 6379
```

If this fails, switch everyone to a phone hotspot or to Tailscale and retry.

## Joining the cluster (every laptop except the host)

Get the code and install its packages:

```bash
git clone https://github.com/VishnuVardhanNk/LapCluster.git
```

```bash
cd LapCluster
```

```bash
python -m pip install -r requirements.txt
```

Create a file named `.env` in the project folder with one line, using the host's
IP address and the Redis password the host shared:

```env
REDIS_URL=redis://:PASSWORD@HOST_IP:6379/0
```

Start your worker and leave it running:

```bash
python -m lapclusters.worker
```

It should print `Worker <your-computer-name> ready, model gemma4:e4b`. If it
prints `Cannot reach Redis`, run the connectivity test above.
