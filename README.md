# Interviewer.ai

AI mock interview platform with a real-time video avatar interviewer, a five-agent reasoning pipeline, resume/JD-driven personalization, and cross-session memory that reshapes future interviews around your actual weak spots.

**Live app:** https://dvbk879zy1q2l.cloudfront.net

## Why this exists

Most AI mock interviewers forget you the moment the session ends, and they validate whatever you say instead of pushing back. This project is built around two bets that most competitors in this space (Revarta, OphyAI, Final Round AI, HireMindPro, and others) don't make:

1. **It remembers.** A dedicated Memory Agent aggregates weaknesses across every session and feeds them straight back into the next session's question plan — not a score trend line, an actual change in what gets asked next.
2. **It pushes back.** The Interviewer Agent is instructed to never accept a vague answer, to probe with "why" and "how," and is explicitly banned from saying "great answer" unless the response actually meets the rubric.

Every evaluated answer also gets a `wouldSurviveRealInterview` verdict with reasoning — a sharper, more honest signal than a 1–10 score, and the thing this product leads with instead of a numeric grade.

## Architecture

A five-agent pipeline orchestrated with LangGraph, each agent with exactly one job:

| Agent | Responsibility |
|---|---|
| **Intake** | Parses resume + job description into a structured profile (skills, experience, project depth, resume-to-JD gaps). Structured output only, never free text. |
| **Planner** | Builds an ordered question plan from the intake profile and the candidate's memory profile, weighting weak areas from prior sessions higher. Runs once per session. |
| **Interviewer** | Holds live session state, asks questions, and decides in real time whether to follow up, push back, or advance — never passively accepts a vague answer. |
| **Evaluator** | Scores each answer against a rubric (STAR for behavioral, correctness/complexity/edge-cases for technical) and outputs a `wouldSurviveRealInterview` verdict with reasoning. |
| **Memory** | A retrieval/write layer, not a chat agent. Aggregates evaluations into a persistent profile of recurring weaknesses, strengths, and improvement trend that the Planner reads at the start of every future session. |

A sixth agent, **Coach**, reworks a candidate's own weak answer into a model answer grounded in their actual content — an on-demand addition to the core five.

Every agent's output is validated against a Pydantic schema before it's passed to the next stage; no raw LLM text crosses an agent boundary.

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
