# LapClusters demo video script

Read the plain text aloud. The lines in brackets are what to do on screen while
you say it. About three minutes.

Before recording: start the app, ask one "Say hello" prompt so the model is
warm, clear the history, and click Leave so you begin on the landing page.

---

**[Show the landing page.]**

Hi, we are team ReLUactivation, and this is LapClusters.

Small teams want to use AI on their own code and documents. But cloud AI costs
money for every question, and you have to send your private files to someone
else's servers. You can run a model on your own laptop instead, but one laptop
is slow.

LapClusters fixes that. It joins the laptops your team already has into one
private AI cluster.

**[Point at the diagram.]**

Every laptop runs its own copy of Gemma 4 using Ollama. A shared queue splits a
big job into small tasks, gives each task to whichever laptop is free, and then
puts the answers together. Nothing leaves the room, and there is no bill.

**[Point at the three steps, then click "Host a cluster on this laptop".]**

Starting is simple. The app checks that my laptop can run the model. I enter the
cluster password. Then I click Host. My teammates open the same app, and they
see my cluster in a list and click Join. Nobody has to type an IP address.

**[Point at the laptop card on the left.]**

Every laptop in the cluster shows up here, with the model it is running and what
it is working on right now.

**[Click "Ask about files". Type `examples/sales-pack` in the folder box. Type
the question: "How many units were sold in the month of the spring campaign, and
what was the revenue that month?" Click Ask.]**

Now let me show it working. This folder has a report, a chart saved as a
picture, a PDF and a photo. I am asking one question about all of them.

**[Watch the tasks run.]**

Each file becomes one task. The report only says the campaign was in the month
with the tallest bar, and that each unit cost forty rupees. The real numbers are
only inside the chart picture. So LapClusters sends the report and its picture
to the model together.

**[The answer appears.]**

And here is the answer. Three hundred and ten units in March, and twelve
thousand four hundred rupees. It read the number from the picture and the price
from the text.

**[Click the `report.md` row. Click Prompt, then Timeline, then close the panel.]**

Everything is transparent. For any task I can see the exact prompt we sent, the
model's reply, and a timeline showing which laptop did it and how long it took.

**[Drag `examples/invoice.png` onto the form. Type "Who is the customer and
what is the total due?" Press Enter.]**

I can also just drag a file in. This is a picture of an invoice.

**[The answer appears.]**

It reads the picture and tells me the customer is Kovai Traders and the total is
twelve thousand eight hundred rupees.

**[Click "Review code". Type `tests/fixtures/sample_repo`. Click Start review.
When it finishes, click the Results tab.]**

It also reviews code. This small project has bugs we planted on purpose. Each
file is checked separately, and the problems are listed by how serious they are.
Long files are split into parts, so nothing is skipped.

**[Click the Activity tab, then the History tab.]**

Laptops can fail, so we planned for that. If a laptop drops out in the middle of
a task, another laptop takes over its work. If a laptop cannot run its model, it
gives the task back instead of failing it, and its card tells you exactly what
is wrong. And every job is saved here in History.

**[Open the project's GitHub page and scroll to the architecture diagram in the
README. Keep it on screen for this whole part.]**

Now a little about how it is built.

The whole project is written in Python. The AI model is Gemma 4, an open model
from Google. Each laptop runs it locally with Ollama, so the model never talks
to the internet.

The heart of the system is Redis, which runs in Docker on the host laptop. We
use a feature called Redis Streams as our task queue. When I start a job, the
host splits it into tasks and adds them to the stream. Every laptop reads from
that same stream, and Redis makes sure each task goes to exactly one laptop, so
no work is done twice.

A laptop only asks for a new task when it is free. That means a faster laptop
simply takes more tasks. We did not have to write any scheduling logic.

The file itself travels inside the task, through Redis. So the other laptops do
not need a copy of the project, and they do not need internet. They only need to
reach the host.

Each laptop also sends a small heartbeat to Redis every few seconds. If the
heartbeats stop, the cluster knows that laptop is gone and gives its task to
someone else.

To find each other, the laptops use a small broadcast message on the local
network. That is why nobody types an IP address.

The dashboard is a small web server written with FastAPI, with a page made in
plain HTML, CSS and JavaScript. Every laptop runs its own copy, and they all
read the same state from Redis. For PDFs we use a library called pypdf, and for
pictures we use Pillow.

**[Go back to the app, on the Work tab.]**

LapClusters is open source. It turns the laptops you already own into a private
AI cluster.

Thank you.

---

After uploading to YouTube, put the link in `README.md` under "Demo Video".
