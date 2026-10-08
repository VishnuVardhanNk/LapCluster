# LapClusters demo video script

Target length: about 3 minutes 30 seconds. Recorded on one laptop.

Each scene lists what to do on screen and what to say. The words are written to
be spoken, so read them aloud once before recording and change anything that
does not sound like you.

## Before you record

1. Start everything:
   - `docker start redis`
   - open Ollama
   - `.venv\Scripts\python.exe -m lapclusters`
2. Warm the model so the first answer is not slow: on the Work tab choose
   **Prompt**, ask "Say hello", and wait for the answer.
3. Open **History** and click **Clear history**, so the screen starts clean.
4. Click **Leave** so the recording starts on the landing page. Your password
   stays filled in.
5. Set the browser zoom to 110% or 125% so text is readable in the video, and
   close other tabs.
6. Have a file explorer window open on the `examples` folder, ready to drag
   `invoice.png`.
7. Record the browser window only, at 1080p.

If a teammate's laptop is available and updated, have them join during scene 3.
It makes scenes 3 and 7 stronger. The script works without it.

---

## Scene 1 — The problem (0:00 to 0:25)

**On screen:** the landing page. Let the diagram animate. Do not click.

**Say:**
"Small teams want to use AI on their own code and documents. But cloud AI costs
money for every question, and it means sending private files to someone else's
servers. Running a model on your own laptop fixes both problems, but one laptop
is slow. This is LapClusters. It pools the laptops your team already owns into
one private AI cluster."

## Scene 2 — What it is (0:25 to 0:45)

**On screen:** move the mouse over the three points on the left, then the diagram.

**Say:**
"Every laptop runs its own copy of Gemma 4, an open model from Google, through
Ollama. A shared queue splits a job into small tasks, hands them to whichever
laptop is free, and pieces the answers back together. Nothing leaves the room,
and there is no bill per question."

## Scene 3 — Starting a cluster (0:45 to 1:05)

**On screen:** point at step 1 (Ollama running, model shown), step 2 (the
password), then click **Host a cluster on this laptop**. The cluster screen opens.
Point at the laptop card on the left.

**Say:**
"Getting started takes three steps. The app checks that this laptop can run the
model, I enter the cluster password, and I choose to host. Teammates open the
same app on their laptops and pick this cluster from a list. Nobody types an IP
address; the laptops find each other on the network. Each laptop shows up here
with its model and what it is doing."

*If a teammate joins now, add:* "There, a second laptop has just joined."

## Scene 4 — Asking a question across files (1:05 to 1:55)

**On screen:**
1. Click **Ask about files**.
2. In the folder box type `examples/sales-pack`.
3. In the question box type: `How many units were sold in the month of the
   spring campaign, and what was the revenue that month?`
4. Click **Ask**. Watch the tasks appear and finish.

**Say while it runs:**
"This folder has a short report, a chart saved as a picture, a PDF memo and a
photo. I am asking one question of all of them. Each file becomes a task. The
report says the campaign ran in the month with the tallest bar, and that each
unit cost forty rupees. The actual numbers are only in the chart. So the report
and its picture are sent to the model together."

**When the answer appears, say:**
"Three hundred and ten units in March, and twelve thousand four hundred rupees.
It read the number from the picture, the price from the text, and combined them."

## Scene 5 — Seeing how the answer was produced (1:55 to 2:20)

**On screen:** click the `report.md` row. In the side panel click **Prompt**,
then **Timeline**, then **Answer**. Close the panel.

**Say:**
"Nothing here is a black box. For every task I can open the exact prompt that
was sent, the model's raw reply, and a timeline of which laptop took it and how
long it ran. While a task is running, the reply streams in live."

## Scene 6 — Attaching a picture (2:20 to 2:40)

**On screen:** drag `invoice.png` from the file explorer onto the form. In the
question box type `Who is the customer and what is the total due?` and press Enter.

**Say:**
"I can also just drop a file in, or paste a screenshot. Here is an invoice as a
picture."

**When the answer appears:** "Kovai Traders, twelve thousand eight hundred rupees."

## Scene 7 — Code review and writing code (2:40 to 3:05)

**On screen:**
1. Click **Review code**, type `tests/fixtures/sample_repo`, click **Start review**.
2. While it runs, click the **Results** tab when the first findings appear.
3. Optionally: click **Write code**, type `A Python function that checks whether
   a string is a palindrome`, press Enter, and show the answer.

**Say:**
"The same cluster reviews code. This small repository has bugs planted in it,
and each file is reviewed separately. It finds the SQL injection, the division by
zero and the missing await, ranked by severity. Long files are split into parts
so nothing is skipped. It can also answer a single prompt or write code."

## Scene 8 — What happens when a laptop fails (3:05 to 3:25)

**On screen:** point at the laptop card. Then open the **Activity** tab and the
**History** tab.

**Say:**
"Laptops are unreliable, so the cluster expects that. If a laptop drops out
mid-task, another one takes its work over. If a laptop cannot run its model, it
hands the task back instead of failing it, and its card says exactly what is
wrong. Every job is kept in history with its time and the laptops that took part."

*If you ran the two-laptop test, add:* "In our test on an eight-file review, one
laptop took about a hundred and thirty-five seconds and two laptops took
eighty-nine."

## Scene 9 — Close (3:25 to 3:40)

**On screen:** back to the **Work** tab, or the landing page.

**Say:**
"LapClusters is open source, built with Gemma 4, Ollama, Redis and Python. It
turns the laptops you already own into a private AI cluster. Thank you."

---

## Things to get right

- **Only say what the video shows or what was measured.** The two-laptop timing
  in scene 8 came from one test of a small job on an earlier version, so say "in
  our test", not "it is one and a half times faster".
- **Do not say it works with any model or any file.** Scanned PDFs are skipped,
  and pictures need a model that can see.
- **If an answer comes out wrong on a take, record again.** The model does not
  give the same answer every time. Do not cut in an answer from another run.
- **The first request after starting is slow** while Ollama loads the model,
  which is why step 2 of the preparation warms it up.

## After recording

1. Upload to YouTube as Public or Unlisted.
2. Put the link in `README.md` under "Demo Video".
3. Tick "Demo video added" in the README checklist.
