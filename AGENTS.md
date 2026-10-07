# AGENTS.md

## Hacktoberfest Hack Day — Coimbatore 2026

This file is the single source of truth for coding agents working in this repository, including Claude Code, OpenAI Codex, Gemini CLI, Cursor, Windsurf, GitHub Copilot, Aider, RooCode, and other agentic development tools.

Read this file before making changes to the repository.

## 1. Project Context

This repository contains a project built for **Hacktoberfest Hack Day — Coimbatore 2026**, organized by INIT CLUB × iDEA CLUB in collaboration with Major League Hacking (MLH).

The project should be developed as a functional hackathon submission and should clearly communicate:

- The problem being solved
- Why the problem was selected
- The proposed solution
- Innovation and differentiation
- Technical implementation
- Work completed during the hackathon
- Open-source and AI usage
- Setup and usage
- Challenges and learnings

The repository must remain suitable for final submission.

## 2. Development Principles

Agents working in this repository must:

- Understand the existing project before modifying it.
- Prefer simple, maintainable solutions over unnecessary complexity.
- Preserve existing functionality unless a change explicitly requires it.
- Follow the project's existing architecture and conventions.
- Keep implementations focused on the hackathon problem.
- Avoid introducing unnecessary dependencies.
- Use environment variables for secrets and credentials.
- Never hardcode API keys, tokens, passwords, or private credentials.
- Keep commits focused and meaningful.
- Do not fabricate functionality, results, benchmarks, integrations, or claims.

## 3. Repository Structure

The repository may follow a structure similar to:

```text
.
├── README.md
├── AGENTS.md
├── .gitignore
├── .env.example
├── src/
├── public/
├── docs/
└── ...
```

The actual project structure takes precedence over this example.

Do not restructure the repository unnecessarily.

## 4. README Requirements

`README.md` is the primary project submission document.

Agents must keep it accurate and aligned with the actual implementation.

The README should contain:

### Project Title and Pitch

Project name and a concise description of what it does.

### Team

Team name, members, and contributions.

### Problem Statement

The problem being addressed, its target users or context, and why the team selected the problem.

### Solution

The proposed solution, how it addresses the problem, and its key features.

### Innovation and Differentiation

What makes the approach novel or different from existing or conventional solutions.

### Technical Implementation

Architecture, technology stack, major components, AI or ML models, APIs, infrastructure, and important technical decisions.

### Implementation During the Hackathon

What the team actually built during the Hack Day and the major contributions completed during the event.

### Open Source and AI Usage

Open-source libraries, frameworks, models, datasets, APIs, and other external components used in the project, including relevant attribution.

### Setup and Usage

Prerequisites, installation, environment variables, commands, and instructions required to run the project.

### Challenges and Learnings

Important technical or product challenges encountered during development and what the team learned from them.

### Credits and License

External resources, contributors, dependencies, and project licensing information.

Do not add judging criteria, judging scores, internal judging procedures, volunteer information, room allocations, or other organizer-only information to the project README.

## 5. AI and Open-Source Requirements

When AI or open-source components are used:

- Document the model, framework, library, API, or dataset.
- Explain its role in the system.
- Include appropriate attribution and licensing information.
- Do not claim an external component was developed by the team.
- Do not hide significant external dependencies.
- Keep AI integrations meaningful to the project.

If an AI model is used, document where it is used and what function it performs.

## 6. Hackathon Development Requirements

The project should represent work substantially developed during the Hack Day.

Agents must not:

- Present an unrelated pre-existing project as newly built.
- Invent implementation history.
- Remove evidence of existing dependencies or external components.
- Misrepresent external work as team work.
- Add fabricated metrics or results.

Existing libraries, frameworks, APIs, datasets, models, and open-source components may be used where appropriate.

## 7. Secrets and Environment Variables

Never commit secrets.

Use environment variables for credentials and configuration.

Provide required variables through:

```text
.env.example
```

The actual `.env` file must remain untracked.

Examples:

```env
API_KEY=
DATABASE_URL=
MODEL_API_KEY=
```

Never place real credentials in source code, documentation, commits, or configuration files intended for version control.

## 8. Code Quality

Agents should:

- Follow the language and framework conventions already used by the project.
- Keep functions and modules focused.
- Avoid unnecessary abstraction.
- Handle errors appropriately.
- Validate external input where relevant.
- Keep configuration separate from application logic.
- Remove unused code and dependencies when encountered.
- Avoid temporary debugging code in the final submission.

## 9. Testing and Verification

Before declaring a feature complete:

1. Run the relevant tests.
2. Verify the application starts successfully.
3. Verify the affected functionality manually where practical.
4. Check that required environment variables are documented.
5. Ensure the README remains consistent with the implementation.

Do not claim that functionality works without verifying it.

## 10. Changes to the Repository

Before modifying an unfamiliar area:

- Inspect the relevant files.
- Understand how the component is currently used.
- Check for existing utilities or abstractions.
- Make the smallest appropriate change.

Do not rewrite working components merely for stylistic preference.

## 11. Submission Readiness Checklist

Before the final submission, verify:

- [ ] Project builds or runs successfully
- [ ] Core functionality works
- [ ] README is complete and accurate
- [ ] Problem and reason for selecting it are documented
- [ ] Solution and key features are documented
- [ ] Innovation and differentiation are explained
- [ ] Architecture is documented
- [ ] Technical implementation is documented
- [ ] Hackathon-built work is documented
- [ ] Team contributions are documented
- [ ] AI and open-source components are documented
- [ ] Setup instructions work
- [ ] Environment variables are documented
- [ ] Challenges and learnings are documented
- [ ] Credits are included
- [ ] License is included
- [ ] No secrets are committed
- [ ] No fabricated claims are present
- [ ] Repository contains no unnecessary files or dependencies

## 12. Agent Behavior

When asked to modify the project:

1. Inspect the relevant code and repository structure.
2. Understand the existing implementation.
3. Make the requested change.
4. Test or verify the change.
5. Update the README when the change materially affects project functionality or documented setup.
6. Report what changed and what was verified.

When asked to add a feature, do not modify unrelated parts of the project.

When asked to prepare the project for submission, prioritize correctness, reproducibility, documentation, and repository cleanliness.