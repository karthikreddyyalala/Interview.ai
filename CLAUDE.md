# Interviewer.ai — CLAUDE.md

## Project overview
AI mock interview platform with a realistic video avatar interviewer, multi-agent reasoning, resume/JD personalization, and cross-session memory that adapts future interviews to past weak spots.

The wedge against existing competitors (Revarta, OphyAI, HireMindPro, Final Round AI): most are voice-only or low-fidelity avatar, and most score a single session without real adaptive memory across sessions. This product differentiates on (1) realistic lip-synced video avatar and (2) a genuine multi-agent training loop that gets harder on your weak areas over time, not just a repeated question bank.

Stack: React + TypeScript + Vite frontend, FastAPI backend, LangGraph for agent orchestration, Claude (Bedrock) for reasoning agents, Deepgram for STT, ElevenLabs for TTS, Tavus for the streaming video avatar, DynamoDB for session/progress state, Pinecone for semantic memory retrieval, Supabase for auth and relational data.

## Competitive positioning (use this for launch posts, README, pitch)

Researched gaps across the current market (Revarta, OphyAI, Final Round AI, HireMindPro, Linkjob AI, mockinterviews.dev, Google Interview Warmup, and others), confirmed independently across reviews and user threads, not just vendor marketing:

1. **No tool persists weak spots across sessions in a way that reshapes future practice.** Most products show a score trend line at best. None rebuild the next session's question plan around your specific recurring failure patterns. This is the Memory Agent, the single biggest differentiator, do not cut it for speed.
2. **Coverage is fragmented.** Technical-focused tools lack behavioral/soft-skill depth, and vice versa, forcing users to stitch together two or three tools. One engine doing behavioral, technical, and system design with the same rigor is a real gap.
3. **Real adaptive follow-up questions are the most requested, least delivered feature.** Users explicitly complain that AI interviewers don't probe deeper or ask smart follow-ups the way a real interviewer does. This is exactly what the Interviewer Agent's "push back, don't accept vague answers" instruction targets, demo this clip specifically.
4. **Personalization is mostly cosmetic.** Most tools take a job title, not a real resume-to-JD gap analysis. The Intake Agent's structured gap extraction is more rigorous than what most competitors actually ship.
5. **Reliability is a recurring complaint, not a feature.** Crashes mid-session and lost progress show up repeatedly in reviews. This is a bar to clear technically (handle disconnects, autosave session state), not something to market, but do not skip it.
6. **Nobody combines a high-fidelity responsive video avatar with the adaptive agent loop.** Most "video" tools are webcam record-and-review, not a live conversational avatar. This is the most visually demoable differentiator.

**Honest launch framing:** lead with "most AI mock interviewers forget you between sessions, mine doesn't" and "it pushes back on vague answers instead of saying great answer" rather than "I built an AI mock interviewer." The first framing is specific and credible. The second invites "isn't this just like the other ten" comments, since the space is genuinely crowded.

## Agent architecture (this is the core of the product, build this first, ignore avatar until this works in text)

Five agents in a LangGraph pipeline. Each agent has ONE job. Do not let responsibilities blur between them.

1. **Intake Agent** — parses resume + job description into structured JSON (skills, years of experience, project depth, gaps between JD requirements and resume evidence). Output must conform to the `IntakeProfile` schema below. No free text output, ever.

2. **Planner Agent** — takes `IntakeProfile` + `MemoryProfile` (see below) and produces an ordered `QuestionPlan`: a sequence of behavioral/technical/system-design questions with target difficulty, with weak areas from prior sessions weighted higher. Runs once at session start, not per-turn.

3. **Interviewer Agent** — the real-time conversational agent. Holds live session state, asks questions from the plan, listens to the candidate's answer, and decides: follow up, push back, or move to next question. Must NOT accept vague answers passively. System prompt must explicitly instruct it to probe with "why" and "how" at least once per answer before moving on, and to never say "great answer" unless the answer actually meets the rubric.

4. **Evaluator Agent** — runs after each answer (or batched at session end for v1 simplicity). Scores against a rubric (STAR structure for behavioral, correctness/complexity/edge-case-handling for technical). Writes structured `AnswerEvaluation` objects, never prose-only feedback. Must also output `wouldSurviveRealInterview`, a sharper and more honest signal than a 1-10 score, this is the novel feature, do not drop it for a simpler numeric-only score.

5. **Memory Agent** — not a chat agent, a retrieval/write layer. After each session, aggregates `AnswerEvaluation` records into a `MemoryProfile` (recurring weak topics, mistake patterns, improvement trend). The Planner reads this at the start of every new session. This is what makes the "keeps training, keeps improving" requirement real instead of cosmetic — do not skip this agent to ship faster, it is the actual differentiator.

## Core data contracts (define these before writing any agent code)

```typescript
interface IntakeProfile {
  candidateSkills: string[];
  yearsExperience: number;
  projectHighlights: { title: string; description: string; technologies: string[] }[];
  targetRole: string;
  targetCompany?: string;
  jdRequirements: string[];
  resumeToJdGaps: string[];
}

interface QuestionPlan {
  sessionId: string;
  questions: {
    id: string;
    type: "behavioral" | "technical" | "system_design";
    prompt: string;
    targetDifficulty: 1 | 2 | 3 | 4 | 5;
    weightedFromWeakness: boolean; // true if pulled from MemoryProfile weak areas
  }[];
}

interface AnswerEvaluation {
  questionId: string;
  transcript: string;
  rubricScores: Record<string, number>; // e.g. { structure: 3, depth: 2, specificity: 4 }
  weaknessTags: string[]; // e.g. ["vague-impact", "no-edge-cases", "rambling"]
  followUpCount: number;
  wouldSurviveRealInterview: boolean; // would this answer hold up to a real interviewer's follow-up grilling
  survivalReasoning: string; // one or two sentences, why it would or wouldn't hold up
}

interface MemoryProfile {
  candidateId: string;
  recurringWeaknesses: { tag: string; frequency: number; lastSeen: string }[];
  improvementTrend: { sessionDate: string; avgScore: number }[];
  strongAreas: string[];
}
```

## Git rules
- NEVER mention AI, Claude, or "generated" in commit messages
- Use conventional commits: feat:, fix:, refactor:, chore:
- Commit author: Karthik
- Example: `feat: add memory agent weakness aggregation`

## Code style
- TypeScript strict mode, no `any` types ever
- Functional components only, no class components
- Custom hooks for all external API integrations (useDeepgram, useTavus, useClaudeAgent)
- Co-locate types with their component files
- No barrel exports (no index.ts re-exports)
- Tailwind for all styling, no inline styles except dynamic values
- Python backend: type hints everywhere, Pydantic models for every agent input/output (mirrors the TypeScript contracts above)

## Folder structure (enforce strictly)
```
src/
  components/       # Pure UI, no API calls
  hooks/            # All external API hooks
  lib/              # API clients (deepgram.ts, tavus.ts, claude.ts)
  types/            # Shared TypeScript interfaces (mirror backend Pydantic models)
  pages/            # Route-level components
  stores/           # Zustand stores

backend/
  agents/           # intake.py, planner.py, interviewer.py, evaluator.py, memory.py
  graph/            # LangGraph definition wiring the 5 agents together
  models/           # Pydantic schemas matching the TS contracts exactly
  routes/           # FastAPI route handlers
  prompts/          # One .txt or .md system prompt per agent, version controlled
```

## Design rules (read .claude/skills before any UI work)
- Dark theme only, no light mode toggle
- Font: Syne for headings, JetBrains Mono for data/scores
- Color palette: bg #0A0A0C, accent #4A7CFF, success #00D4AA, warning #FF6B3D
- No purple gradients, no generic AI aesthetics, no Inter font
- Every animation must serve a functional purpose
- Score/feedback UI should read like a real evaluation rubric, not a gamified badge system

## Build order (do not reorder, each step validates the next)
1. Intake Agent + Planner Agent, text-only, no voice/video — validate personalization quality first
2. Interviewer Agent + Evaluator Agent, still text-only — validate the conversation feels hard and adaptive
3. Memory Agent wired in, run two fake sessions back to back, confirm session 2 actually targets session 1 weaknesses
4. Add voice (Deepgram STT + ElevenLabs TTS) via Pipecat or LiveKit Agents for turn-taking/barge-in
5. Add Tavus streaming avatar last — this is the most replaceable layer, do not let it block the agent logic

## Hard rules, do not violate
- Never let the Interviewer Agent's system prompt be soft. It must be instructed to push back on vague answers, not validate them. This is the entire value proposition.
- Never skip the Memory Agent to save time. A version without it is just another single-session mock interviewer, indistinguishable from existing competitors.
- Never block on the avatar provider. If Tavus/HeyGen integration stalls, ship with audio-only and a static portrait; the avatar is cosmetic relative to the agent pipeline.
- Every agent's output must validate against its Pydantic/TypeScript schema before being passed to the next agent. No raw LLM text passed between agents.
- Never reduce feedback to a numeric score alone. `wouldSurviveRealInterview` plus reasoning is the differentiated feedback format, surface it prominently in the UI, not buried below a score widget.