# LapClusters

> Turn the laptops your team already owns into a private AI cluster that reviews a whole code repository in parallel, with no cloud bill and no code leaving the room.

## Team

**Team Name:** [Team Name]


| Member | Contribution   |
| ------ | -------------- |
| [Name] | [Contribution] |
| [Name] | [Contribution] |
| [Name] | [Contribution] |
| [Name] | [Contribution] |


## Problem Statement

### The Problem

Small teams and students want to use AI on their own code, but cloud AI costs money per request and means sending private code to someone else's servers. A single laptop running a local model is private and free, but slow: reviewing a whole repository file by file can take a very long time.

### Why We Chose This Problem

[Explain why the team selected this problem and why solving it is important.]

## Solution

LapClusters pools a team's laptops into one private cluster. Every laptop runs its own copy of Gemma 4, and a shared Redis queue hands out work. For a code review, the orchestrator creates one task per file, the laptops review files in parallel, and the results are merged into a single report.

### Key Features

- [Feature 1]
- [Feature 2]
- [Feature 3]
- [Feature 4]

## Innovation and Differentiation

[Explain what is innovative about the approach and how it differs from existing or conventional solutions.]

## Technical Implementation

### Architecture

[Add the system architecture or workflow Mermaid diagram here.]

### Technology Stack


| Category        | Technologies                |
| --------------- | --------------------------- |
| Frontend        | [Technologies / N/A]        |
| Backend         | [Technologies / N/A]        |
| Database        | [Technologies / N/A]        |
| AI / ML         | [Models / frameworks / N/A] |
| Infrastructure  | [Technologies / N/A]        |
| APIs / Services | [Services / N/A]            |


If a category or technology is not implemented in the project, specify `N/A` instead of leaving the field blank.

### How It Works

[Explain the major components of the system and how they interact.]

### Technical Decisions

[Explain important architectural, algorithmic, or engineering decisions made during development.]

## Implementation During the Hackathon

[Describe what the team built during the Hack Day and the major functionality or components completed during the event.]

### Team Contributions

- **[Member Name]:** [Contribution]
- **[Member Name]:** [Contribution]
- **[Member Name]:** [Contribution]
- **[Member Name]:** [Contribution]

## Working Application

**Live Application:** [Live URL]

[Briefly explain how the deployed application can be accessed and what functionality can be tested.]

The submitted application should be functional and accessible through the provided link where applicable.

## Demo Video

**Demo Video:** [Video URL]

[Provide a short demonstration of the working project, covering the main user flow and important functionality.]

## Open Source and AI Usage

### AI / Models

- **[Model]:** [How it is used]

### Open Source Components

- **[Library / Framework]:** [Purpose]
- **[Dataset]:** [Purpose]
- **[API / Service]:** [Purpose]

[Include relevant licenses, attribution, and acknowledgements for external components.]

## Setup and Usage

### Prerequisites

- Python 3.11 or newer
- Docker Desktop (to run Redis)
- Ollama with the `gemma4:e4b` model pulled

### Installation

```bash
git clone https://github.com/VishnuVardhanNk/LapCluster.git
cd LapCluster
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
docker run -d --name redis -p 6379:6379 redis:7
ollama pull gemma4:e4b
```

On macOS or Linux, activate the environment with `source .venv/bin/activate`.

### Environment Variables

Copy `.env.example` to `.env` and adjust if needed.

```env
REDIS_URL=redis://localhost:6379/0
OLLAMA_URL=http://localhost:11434
MODEL=gemma4:e4b
WORKER_NAME=
```

### Running the Project

Start a worker:

```bash
python -m lapclusters.worker
```

### Usage

In a second terminal, send a prompt to the cluster:

```bash
python -m lapclusters.cli "Explain what a task queue is in one sentence."
```

The answer is printed once a worker has processed it.

Run the tests with `python -m pytest`.

## Devpost Submission

**Devpost Project:** [Devpost Project URL]

[Add the link to the team's Devpost submission. Ensure the Devpost project page is complete and contains the required project information, links, media, and team details.]

## Credits and License

### Credits

[Credit libraries, frameworks, datasets, models, APIs, contributors, and other external resources used.]

### License

[License name and/or link.]

## Submission Checklist

- [ ] Project title and description added
- [ ] All team members listed
- [ ] Problem clearly explained
- [ ] Reason for choosing the problem explained
- [ ] Solution and key features documented
- [ ] Innovation and differentiation explained
- [ ] Architecture included
- [ ] Technical implementation documented
- [ ] Work completed during the hackathon documented
- [ ] Team contributions documented
- [ ] Working application is functional
- [ ] Live application link added where applicable
- [ ] Demo video added
- [ ] AI and open-source components documented
- [ ] Setup and usage instructions tested
- [ ] Challenges and learnings documented
- [ ] Devpost submission completed
- [ ] Devpost link added
- [ ] Credits added
- [ ] License added
- [ ] Repository is organized and complete
