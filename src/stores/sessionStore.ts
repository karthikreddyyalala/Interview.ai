import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";
import type {
  AnswerEvaluation,
  IntakeProfile,
  InterviewDecision,
  InterviewLevel,
  InterviewMode,
  MemoryProfile,
  PlannedQuestion,
  QuestionPlan,
} from "@/types/contracts";
import { api } from "@/lib/api";
import { getCandidateId } from "@/lib/identity";

export type Speaker = "interviewer" | "candidate";
export type MessageKind = "question" | "answer" | "follow_up";

export interface ChatMessage {
  id: string;
  speaker: Speaker;
  kind: MessageKind;
  text: string;
  questionId: string;
  weighted?: boolean;
}

export type SessionStatus = "idle" | "starting" | "live" | "thinking" | "wrapping" | "complete";

interface SessionState {
  status: SessionStatus;
  role: string;
  mode: InterviewMode;
  level: InterviewLevel;
  candidateName: string;
  useVideo: boolean;
  // True during the opening small-talk exchange (before Q1). Warm-up answers
  // are acknowledged, never scored.
  warmup: boolean;
  profile: IntakeProfile | null;
  plan: QuestionPlan | null;
  currentIdx: number;
  followUpCount: number;
  messages: ChatMessage[];
  evaluations: AnswerEvaluation[];
  priorMemory: MemoryProfile | null;
  updatedMemory: MemoryProfile | null;
  justRestored: boolean;
  turnError: string | null;
  // True for the duration of a finalizeSession call (initial or retry). The
  // backend's finalize isn't idempotent for duplicate identical calls — each
  // one independently re-runs the Memory Agent merge and writes — so this
  // guards against a double-click firing two concurrent finalize requests.
  finalizing: boolean;

  loadMemory: () => Promise<void>;
  start: (input: {
    resumeText: string;
    jdText: string;
    role: string;
    mode: InterviewMode;
    level: InterviewLevel;
    candidateName: string;
    useVideo: boolean;
  }) => Promise<void>;
  submitAnswer: (text: string) => Promise<void>;
  retryFinalize: () => Promise<void>;
  clearRestored: () => void;
  clearTurnError: () => void;
  reset: () => void;
}

const TRANSITIONS = [
  "Got it, thanks.",
  "Okay, that's helpful.",
  "Makes sense.",
  "Alright, appreciate that.",
  "Good — let me shift gears.",
];

function pickTransition(): string {
  return TRANSITIONS[Math.floor(Math.random() * TRANSITIONS.length)];
}

// First thing the interviewer says — warm, by name, opening the small talk.
function warmGreeting(name: string): string {
  const who = name.trim() ? ` ${name.trim().split(/\s+/)[0]}` : "";
  return `Hi${who}, good to meet you — thanks for making the time. Before we dive in, how are you doing today?`;
}

// Said after the candidate's small-talk reply, then straight into Q1.
function warmIntro(mode: InterviewMode, level: InterviewLevel): string {
  const modeDesc: Record<InterviewMode, string> = {
    full: `a few questions across behavioral, technical, and system design`,
    behavioral: `a few behavioral questions`,
    technical: `a few technical questions`,
    system_design: `a couple of system design questions`,
  };
  return `Glad to hear it. So today I'll take you through ${modeDesc[mode]} — pitched at the ${level} level. Just talk me through your thinking out loud, and I'll follow up where I'm curious. Let's start with the first one.`;
}

// UUID message ids so rehydrating a saved session never collides with new
// messages generated after the resume.
const mkId = (): string =>
  typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `m-${Date.now()}-${Math.random().toString(36).slice(2)}`;

function currentQuestion(plan: QuestionPlan | null, idx: number): PlannedQuestion | null {
  return plan?.questions[idx] ?? null;
}

function hasMemory(m: MemoryProfile | null): boolean {
  return !!m && m.recurringWeaknesses.length > 0;
}

export const useSessionStore = create<SessionState>()(
  persist(
    (set, get) => {
      // Shared by the initial finalize attempt (end of submitAnswer) and
      // retryFinalize: calls the backend, and on failure — a real, reachable
      // outcome (retry-budget-exhausted memory-profile conflicts, other
      // transient agent errors) — surfaces it the same way turn-submission
      // failures already are (turnError + a way to retry) instead of leaving
      // the session stuck on "wrapping" with no feedback.
      const runFinalize = async () => {
        // In-flight guard: a duplicate call (e.g. a double-click on RETRY)
        // must be a no-op, not a second concurrent finalize request.
        if (get().finalizing) return;
        set({ finalizing: true });
        const state = get();
        try {
          const updated = await api.finalizeSession({
            candidateId: getCandidateId(),
            evaluations: state.evaluations,
            sessionId: state.plan!.sessionId,
            mode: state.mode,
            level: state.level,
            questions: state.plan!.questions,
          });
          set({ status: "complete", updatedMemory: updated, turnError: null, finalizing: false });
        } catch (e) {
          set({
            turnError: e instanceof Error ? e.message : "Request failed — please try again.",
            finalizing: false,
          });
        }
      };

      return {
      status: "idle",
      role: "sde",
      mode: "full",
      level: "mid",
      candidateName: "",
      useVideo: false,
      warmup: false,
      profile: null,
      plan: null,
      currentIdx: 0,
      followUpCount: 0,
      messages: [],
      evaluations: [],
      priorMemory: null,
      updatedMemory: null,
      justRestored: false,
      turnError: null,
      finalizing: false,

      loadMemory: async () => {
        const memory = await api.getMemory(getCandidateId());
        set({ priorMemory: hasMemory(memory) ? memory : null });
      },

      start: async ({ resumeText, jdText, role, mode, level, candidateName, useVideo }) => {
        set({
          status: "starting", role, mode, level, candidateName, useVideo,
          warmup: false, messages: [], evaluations: [], updatedMemory: null,
          // A leftover finalize error from a prior session (turnError is
          // persisted, see the partialize comment below) must not bleed
          // into a brand-new session's UI. finalizing itself always
          // rehydrates to false (it's not persisted), so clearing it here
          // is just defensive symmetry with turnError.
          turnError: null, finalizing: false,
        });
        try {
        const candidateId = getCandidateId();
        const prior = await api.getMemory(candidateId);
        const { profile, plan } = await api.startSession({
          resumeText,
          jdText,
          role,
          candidateId,
          mode,
          level,
        });
        // Open with the warm greeting only — the first question comes after the
        // candidate's small-talk reply (handled in submitAnswer while warmup).
        set({
          status: "live",
          profile,
          plan,
          priorMemory: hasMemory(prior) ? prior : null,
          currentIdx: 0,
          followUpCount: 0,
          justRestored: false,
          warmup: true,
          messages: [
            {
              id: mkId(),
              speaker: "interviewer",
              kind: "question",
              text: warmGreeting(candidateName),
              questionId: "intro",
            },
          ],
        });
        } catch (e) {
          set({ status: "idle" });
          throw e;
        }
      },

      submitAnswer: async (text) => {
        const state = get();
        const question = currentQuestion(state.plan, state.currentIdx);
        if (!question || state.status !== "live") return;

        // Warm-up exchange: the candidate's reply to "how are you doing?" is
        // small talk — acknowledge it and move into Q1, never score it.
        if (state.warmup) {
          const first = state.plan!.questions[0];
          set((s) => ({
            warmup: false,
            messages: [
              ...s.messages,
              { id: mkId(), speaker: "candidate", kind: "answer", text, questionId: "intro" },
              {
                id: mkId(),
                speaker: "interviewer",
                kind: "question",
                text: `${warmIntro(state.mode, state.level)}\n\n${first.prompt}`,
                questionId: first.id,
                weighted: first.weightedFromWeakness,
              },
            ],
          }));
          return;
        }

        set({
          status: "thinking",
          turnError: null,
          messages: [
            ...state.messages,
            { id: mkId(), speaker: "candidate", kind: "answer", text, questionId: question.id },
          ],
        });

        const isLast = state.currentIdx === state.plan!.questions.length - 1;

        // Build the full conversation thread for this question so the Evaluator
        // has complete context (initial answer + any follow-up exchanges), not
        // just the final reply in isolation.
        const priorMsgs = state.messages.filter((m) => m.questionId === question.id);
        const thread =
          priorMsgs.length > 0
            ? priorMsgs
                .map((m) => `${m.speaker === "interviewer" ? "Q" : "A"}: ${m.text}`)
                .join("\n") + `\nA: ${text}`
            : text;

        let decision: InterviewDecision;
        let evaluation: AnswerEvaluation | undefined;
        try {
          ({ decision, evaluation } = await api.submitAnswer({
            question,
            answer: thread,
            followUpCount: state.followUpCount,
            isLast,
          }));
        } catch (e) {
          set((s) => ({
            status: "live",
            turnError: e instanceof Error ? e.message : "Request failed — please try again.",
            messages: s.messages.slice(0, -1),
          }));
          return;
        }

        if (decision.action === "follow_up" && decision.followUpPrompt) {
          set((s) => ({
            status: "live",
            followUpCount: s.followUpCount + 1,
            messages: [
              ...s.messages,
              {
                id: mkId(),
                speaker: "interviewer",
                kind: "follow_up",
                text: decision.followUpPrompt!,
                questionId: question.id,
              },
            ],
          }));
          return;
        }

        // advance or complete — record evaluation
        const evals = evaluation ? [...state.evaluations, evaluation] : state.evaluations;

        if (decision.action === "complete") {
          const closing: ChatMessage = {
            id: mkId(),
            speaker: "interviewer",
            kind: "question",
            text: "That's everything I wanted to cover. Give me a moment to pull together your evaluation.",
            questionId: question.id,
          };
          set((s) => ({
            status: "wrapping",
            messages: [...s.messages, closing],
            evaluations: evals,
          }));
          // Backend persists the aggregated memory (DynamoDB in real mode,
          // localStorage in mock mode) — it's the source of truth now. On
          // failure (a real, reachable 503 from a memory-profile locking
          // conflict, or other transient errors) runFinalize surfaces
          // turnError and leaves status at "wrapping" so retryFinalize can
          // be called again, instead of hanging forever with no feedback.
          await runFinalize();
          return;
        }

        const nextIdx = state.currentIdx + 1;
        const next = state.plan!.questions[nextIdx];
        const connector = pickTransition();
        set((s) => ({
          status: "live",
          currentIdx: nextIdx,
          followUpCount: 0,
          evaluations: evals,
          messages: [
            ...s.messages,
            {
              id: mkId(),
              speaker: "interviewer",
              kind: "question",
              text: `${connector}\n\n${next.prompt}`,
              questionId: next.id,
              weighted: next.weightedFromWeakness,
            },
          ],
        }));
      },

      retryFinalize: async () => {
        if (get().status !== "wrapping" || get().finalizing) return;
        set({ turnError: null });
        await runFinalize();
      },

      clearRestored: () => set({ justRestored: false }),
      clearTurnError: () => set({ turnError: null }),

      reset: () =>
        set({
          status: "idle",
          profile: null,
          plan: null,
          currentIdx: 0,
          followUpCount: 0,
          warmup: false,
          messages: [],
          evaluations: [],
          updatedMemory: null,
          justRestored: false,
          turnError: null,
          finalizing: false,
        }),
      };
    },
    {
      name: "crucible.session.v1",
      storage: createJSONStorage(() => localStorage),
      // Persist only session data, never the action fns. Transient statuses
      // are corrected at save time: a reload during "thinking" (request in
      // flight, now lost) resumes as "live" so the answer can be resubmitted;
      // "starting" (no plan yet) falls back to "idle". "wrapping" is assumed
      // to have finished by the time of reload and resumes as "complete" —
      // UNLESS it ended in an unresolved finalize error (turnError set), in
      // which case that would be lying about a failed finalize looking
      // successful (silently wrong, not just stuck) — so it stays "wrapping"
      // and turnError is persisted alongside it so the retry UI reappears.
      partialize: (s) => ({
        status:
          s.status === "thinking" ? "live"
          : s.status === "starting" ? "idle"
          : s.status === "wrapping" && !s.turnError ? "complete"
          : s.status,
        role: s.role,
        mode: s.mode,
        level: s.level,
        candidateName: s.candidateName,
        useVideo: s.useVideo,
        warmup: s.warmup,
        profile: s.profile,
        plan: s.plan,
        currentIdx: s.currentIdx,
        followUpCount: s.followUpCount,
        messages: s.messages,
        evaluations: s.evaluations,
        priorMemory: s.priorMemory,
        updatedMemory: s.updatedMemory,
        turnError: s.turnError,
      }),
      onRehydrateStorage: () => (state) => {
        // Flag a mid-interview resume so the UI can acknowledge it.
        if (state && state.status === "live" && state.messages.length > 0) {
          state.justRestored = true;
        }
      },
    }
  )
);
