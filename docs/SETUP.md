# Setting up a laptop

Do this once on every laptop. One laptop is the **host**: it also runs Redis.

## Check the laptop

- Memory: 16 GB is comfortable for `gemma4:e4b`. With 8 GB, use `gemma4:e2b`.
- Free disk: about 10 GB.
- Every laptop must be on the same network. A phone hotspot works. Some campus
  and office Wi-Fi networks stop laptops from reaching each other.

## Every laptop

### 1. Install Git, Python and Ollama

- Git: https://git-scm.com/downloads
- Python 3.11 or newer: https://www.python.org/downloads/ (on Windows, tick "Add
  Python to PATH")
- Ollama: https://ollama.com/download

### 2. Get the model

```bash
ollama pull gemma4:e4b
```

The download is several gigabytes, so do it on good Wi-Fi. Check it answers:

```bash
ollama run gemma4:e4b "Say hello in one sentence."
```

If you already have Gemma 4 as a GGUF file, you can import it instead of
downloading. In the folder that holds the file, create a file named `Modelfile`
containing `FROM ./your-file.gguf`, then run:

```bash
ollama create gemma4:e4b -f Modelfile
```

A model imported this way reads text only, unless its image projector was
imported with it.

### 3. Get the project

```bash
git clone https://github.com/VishnuVardhanNk/LapCluster.git
```

```bash
cd LapCluster
```

```bash
python -m pip install -r requirements.txt
```

To update later, run `git pull` and the `pip install` line again. Every laptop
in a cluster must be on the same version.

## The host laptop only

### 4. Start Redis with a password

Install Docker Desktop from https://www.docker.com/products/docker-desktop/ and
start it. Pick a password and share it with the team privately, not in the
repository.

```bash
docker run -d --name redis -p 6379:6379 redis:7 redis-server --requirepass YOUR_PASSWORD
```

After a restart of the laptop, bring it back with `docker start redis`.

### 5. Let other laptops in

In a PowerShell window opened with "Run as administrator":

```bash
New-NetFirewallRule -DisplayName "LapClusters Redis" -Direction Inbound -Protocol TCP -LocalPort 6379 -Action Allow
```

```bash
New-NetFirewallRule -DisplayName "LapClusters discovery" -Direction Inbound -Protocol UDP -LocalPort 47600 -Action Allow
```

If other laptops still cannot connect, check for a rule that blocks Docker. It is
created when Windows asks whether Docker may accept connections and the prompt is
cancelled:

```bash
Get-NetFirewallRule -Direction Inbound -Action Block | Where-Object DisplayName -match "docker"
```

## Start

On every laptop:

```bash
python -m lapclusters
```

The app opens in the browser. Enter the cluster password, then:

- on the host, click **Host a cluster on this laptop**;
- on every other laptop, click **Join** next to the host's name.

The app remembers this, so next time it reconnects by itself.

## If something goes wrong

| What you see | What to do |
|--------------|------------|
| "Ollama: Not running" on the first screen | Start Ollama. The screen updates by itself. |
| "This cluster needs a password" or "That password was not accepted" | Type the host's Redis password. Tick "Show the password" to check it. |
| No cluster in the Join list | Make sure the host has clicked Host and both laptops are on the same network. Otherwise use "Enter an address instead" with the host's IP address. |
| A laptop card says "Needs updating" | On that laptop run `git pull` and `python -m pip install -r requirements.txt`, then start the app again. |
| A laptop card says its model is not installed | Choose an installed model from that card, or run `ollama pull` for the one it names. |
| Tasks with pictures are waiting | No connected laptop has a model that can see. Switch one to such a model. |
