# Interviewer.ai

AI mock interview platform with a real-time video avatar interviewer, a five-agent reasoning pipeline, resume/JD-driven personalization, and cross-session memory that reshapes future interviews around your actual weak spots.

**Live app:** https://dvbk879zy1q2l.cloudfront.net

## Why this exists

I was using Claude's and ChatGPT's voice mode to practice for interviews. No matter what I said, it kept telling me "that's great," "you're acing it." It felt good in the moment, but it was building a false sense of confidence — the kind that falls apart the second an actual interviewer pushes back on a vague answer instead of validating it.

That gap is the whole reason this exists. Practice that only ever agrees with you isn't practice, it's a confidence trick. So instead of another voice mode, I set out to build something closer to a real interviewer: a video avatar, real voice, and infrastructure to back it — and after looking at what the existing competitors in this space (Revarta, OphyAI, Final Round AI, HireMindPro, and others) actually shipped, I built around two things almost none of them do:

1. **It remembers.** A dedicated Memory Agent aggregates weaknesses across every session and feeds them straight back into the next session's question plan — not a score trend line, an actual change in what gets asked next.
2. **It pushes back.** The Interviewer Agent is instructed to never accept a vague answer, to probe with "why" and "how," and is explicitly banned from saying "great answer" unless the response actually meets the rubric — the exact behavior I wished the voice-mode tools I was using had, instead of cheerleading me into a false sense of readiness.

Every evaluated answer also gets a `wouldSurviveRealInterview` verdict with reasoning — a sharper, more honest signal than a 1–10 score, and the thing this product leads with instead of a numeric grade.

## Architecture

The avatar and voice are what make a session feel real, but they're not the point — the reasoning underneath them is. That's a five-agent pipeline orchestrated with LangGraph, each agent with exactly one job:

| Agent | Responsibility |
|---|---|
| **Intake** | Parses resume + job description into a structured profile (skills, experience, project depth, resume-to-JD gaps). Structured output only, never free text. |
| **Planner** | Builds an ordered question plan from the intake profile and the candidate's memory profile, weighting weak areas from prior sessions higher. Runs once per session. |
| **Interviewer** | Holds live session state, asks questions, and decides in real time whether to follow up, push back, or advance — never passively accepts a vague answer. |
| **Evaluator** | Scores each answer against a rubric (STAR for behavioral, correctness/complexity/edge-cases for technical) and outputs a `wouldSurviveRealInterview` verdict with reasoning. |
| **Memory** | A retrieval/write layer, not a chat agent. Aggregates evaluations into a persistent profile of recurring weaknesses, strengths, and improvement trend that the Planner reads at the start of every future session. |

A sixth agent, **Coach**, reworks a candidate's own weak answer into a model answer grounded in their actual content — an on-demand addition to the core five.

Every agent's output is validated against a Pydantic schema before it's passed to the next stage; no raw LLM text crosses an agent boundary.

## Engineering depth

Things here that go beyond a demo wrapper around an LLM call:

- **Structured output is enforced, not hoped for.** Every agent call routes through a single choke point (`backend/llm/client.py`) that parses and validates the response against a Pydantic schema, with retry-on-parse-failure and retry-on-schema-failure — not a bare try/except. No raw LLM text is ever passed between agents or to the frontend.
- **Prompts are constrained to testable behavior, not vibes.** The Interviewer's prompt bans specific validating phrases outright ("great answer," "interesting"), defines concrete vagueness criteria per question type, and caps a progressive follow-up arc — STAR-based for behavioral, "can we do better" for technical, scope-then-tradeoffs for system design.
- **The pipeline is tested at three separate layers**: per-agent unit tests (a fake LLM verifying plumbing), LangGraph-level tests (verifying state transitions with scripted decisions), and an end-to-end suite that asserts on the *actual prompt text* the Planner receives after a prior session — proving the cross-session memory loop changes what gets asked, not just that the code paths exist.
- **A gated eval harness with golden datasets** (`backend/evals/`) regression-tests judgment quality — vague-vs-strong answers, two-session memory aggregation — skipped by default since real runs cost Bedrock tokens, triggered explicitly via `INTERVIEWAI_RUN_LLM_EVALS=1`.
- **Deliberate cost/latency tiering, not "call the biggest model everywhere."** Haiku handles structured extraction (Intake, Memory); Sonnet is reserved for the agents that actually carry the product's judgment (Planner, Interviewer, Evaluator, Coach) — enforced by a test asserting Opus is never a default anywhere in config.
- **Every optional integration degrades gracefully.** Tavus, auth, and the persistence backend are all feature-flagged; the app is fully demoable with zero AWS credentials via a local mock engine, and falls back to a stylized avatar if no Tavus key is configured — the video layer was built to never block the agent logic underneath it.

## Stack

- **Frontend:** React + TypeScript + Vite, Tailwind CSS, Zustand, React Router
- **Backend:** FastAPI on AWS Lambda (Mangum), LangGraph for agent orchestration
- **Model:** Claude on Amazon Bedrock (Sonnet for reasoning-heavy agents, Haiku for structured extraction)
- **Persistence:** DynamoDB (session state, cross-session memory profiles)
- **Auth:** AWS Cognito
- **Video avatar:** Tavus (via Daily.co WebRTC), optional and gated — the app degrades gracefully to a stylized avatar if unconfigured
- **Voice:** browser-native Speech Recognition/Synthesis APIs, with local in-browser TTS (Kokoro) for higher quality
- **Infra:** API Gateway, S3 + CloudFront for the static frontend, deployed via plain shell scripts (no Terraform/CDK)

## Project structure

```
src/
  components/   Pure UI components
  hooks/        External API integration hooks (Tavus, speech, PDF)
  lib/          API client, auth wrapper, mock engine
  types/        Shared TypeScript contracts, mirrors backend Pydantic models
  pages/        Route-level screens
  stores/       Zustand stores

backend/
  agents/       intake.py, planner.py, interviewer.py, evaluator.py, memory.py, coach.py
  graph/        LangGraph wiring for session start, live turns, and session end
  models/       Pydantic schemas
  routes/       FastAPI route handlers
  prompts/      One versioned system prompt per agent
  store/        Pluggable persistence (in-memory for tests, DynamoDB in production)

deploy/         Shell scripts for IAM setup, Cognito, Lambda packaging, and deployment
docs/           Architecture specs and diagrams
```

## Running locally

**Backend**

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env   # fill in AWS/Bedrock credentials
uvicorn app:app --reload
```

**Frontend**

```bash
npm install
npm run dev
```

By default the frontend runs against a local mock engine (`VITE_USE_MOCK`) so the UI is demoable without any backend or AWS credentials. Set `VITE_USE_MOCK=false` and `VITE_API_BASE` to exercise the real agent pipeline.

## Testing

```bash
cd backend && pytest            # unit, graph, and end-to-end tests
npm test                        # frontend unit tests
```

Evaluation harnesses that make real LLM calls (`backend/evals/`) are gated behind `INTERVIEWAI_RUN_LLM_EVALS=1` since they incur Bedrock cost; they cover judgment-quality checks like vague-vs-strong answers and two-session memory aggregation.

## Deployment

The app is deployed serverlessly on AWS: CloudFront + S3 for the frontend, API Gateway + a single Lambda for the backend, DynamoDB for state, Bedrock for inference. See [deploy/README.md](deploy/README.md) for the full setup and deploy scripts.
