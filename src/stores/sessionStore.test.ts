import { describe, it, expect, vi, beforeEach } from "vitest";
import { useSessionStore } from "./sessionStore";
import type { TurnResult } from "@/lib/api";
import type { AnswerEvaluation, QuestionPlan, InterviewDecision, MemoryProfile } from "@/types/contracts";

// ---- Mocks ----------------------------------------------------------------

vi.mock("@/lib/api", () => ({
  api: {
    submitAnswer: vi.fn(),
    startSession: vi.fn(),
    getMemory: vi.fn(),
    finalizeSession: vi.fn(),
  },
}));

vi.mock("@/lib/identity", () => ({
  getCandidateId: () => "test-candidate-id",
}));

// ---- Fixtures -------------------------------------------------------------

const MOCK_PLAN: QuestionPlan = {
  sessionId: "test-session",
  questions: [
    {
      id: "q1",
      type: "behavioral",
      prompt: "Tell me about a time you had to debug a critical production issue.",
      targetDifficulty: 3,
      weightedFromWeakness: false,
    },
    {
      id: "q2",
      type: "technical",
      prompt: "Walk me through how a hash map works under the hood.",
      targetDifficulty: 3,
      weightedFromWeakness: false,
    },
  ],
};

const FOLLOW_UP_DECISION: InterviewDecision = {
  action: "follow_up",
  followUpPrompt: "What was the measurable impact?",
  currentQuestionId: "q1",
};

const ADVANCE_DECISION: InterviewDecision = {
  action: "advance",
  followUpPrompt: null,
  currentQuestionId: "q1",
};

const COMPLETE_DECISION: InterviewDecision = {
  action: "complete",
  followUpPrompt: null,
  currentQuestionId: "q2",
};

const MOCK_EVALUATION: AnswerEvaluation = {
  questionId: "q2",
  transcript: "My final answer.",
  rubricScores: { structure: 4, specificity: 3, impact: 4, ownership: 3 },
  weaknessTags: [],
  followUpCount: 0,
  wouldSurviveRealInterview: true,
  survivalReasoning: "Concrete example with measurable outcome.",
};

const MOCK_MEMORY: MemoryProfile = {
  candidateId: "test-candidate-id",
  recurringWeaknesses: [],
  improvementTrend: [{ sessionDate: "2026-09-03", avgScore: 3.5 }],
  strongAreas: ["debugging"],
};

function seedLiveSession() {
  useSessionStore.setState({
    status: "live",
    role: "sde",
    profile: null,
    plan: MOCK_PLAN,
    currentIdx: 0,
    followUpCount: 0,
    messages: [],
    evaluations: [],
    priorMemory: null,
    updatedMemory: null,
    justRestored: false,
    turnError: null,
  });
}

// ---- Tests ----------------------------------------------------------------

describe("sessionStore — turnError", () => {
  beforeEach(async () => {
    // Dynamically import after mocks are registered so vi.mock is applied
    const { api } = await import("@/lib/api");
    vi.mocked(api.submitAnswer).mockReset();
    vi.mocked(api.finalizeSession).mockReset();

    useSessionStore.setState({
      status: "idle",
      role: "",
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
    });
  });

  describe("clearTurnError", () => {
    it("sets turnError to null", () => {
      useSessionStore.setState({ turnError: "Something exploded." });
      useSessionStore.getState().clearTurnError();
      expect(useSessionStore.getState().turnError).toBeNull();
    });

    it("is a no-op when turnError is already null", () => {
      useSessionStore.setState({ turnError: null });
      useSessionStore.getState().clearTurnError();
      expect(useSessionStore.getState().turnError).toBeNull();
    });
  });

  describe("submitAnswer — network failure", () => {
    it("sets turnError to the error message when api.submitAnswer throws", async () => {
      seedLiveSession();
      const { api } = await import("@/lib/api");
      vi.mocked(api.submitAnswer).mockRejectedValueOnce(
        new Error("Request failed (503)")
      );

      await useSessionStore.getState().submitAnswer("I used a binary search.");

      expect(useSessionStore.getState().turnError).toBe(
        "Request failed (503)"
      );
    });

    it("falls back to a generic message for non-Error throws", async () => {
      seedLiveSession();
      const { api } = await import("@/lib/api");
      vi.mocked(api.submitAnswer).mockRejectedValueOnce("just a string error");

      await useSessionStore.getState().submitAnswer("My answer.");

      expect(useSessionStore.getState().turnError).toBe(
        "Request failed — please try again."
      );
    });

    it("restores status to 'live' after a failed turn", async () => {
      seedLiveSession();
      const { api } = await import("@/lib/api");
      vi.mocked(api.submitAnswer).mockRejectedValueOnce(new Error("timeout"));

      await useSessionStore.getState().submitAnswer("My answer.");

      expect(useSessionStore.getState().status).toBe("live");
    });

    it("rolls back the optimistically-added candidate message on failure", async () => {
      seedLiveSession();
      const { api } = await import("@/lib/api");
      vi.mocked(api.submitAnswer).mockRejectedValueOnce(new Error("network"));

      await useSessionStore.getState().submitAnswer("Some response.");

      // The candidate message should NOT appear — it was rolled back.
      const msgs = useSessionStore.getState().messages;
      expect(msgs.every((m) => m.speaker !== "candidate")).toBe(true);
    });

    it("does not advance the question index on failure", async () => {
      seedLiveSession();
      const { api } = await import("@/lib/api");
      vi.mocked(api.submitAnswer).mockRejectedValueOnce(new Error("error"));

      await useSessionStore.getState().submitAnswer("answer");

      expect(useSessionStore.getState().currentIdx).toBe(0);
    });
  });

  describe("submitAnswer — successful turn clears prior error", () => {
    it("sets turnError to null when a subsequent submission succeeds", async () => {
      seedLiveSession();
      useSessionStore.setState({ turnError: "previous error" });
      const { api } = await import("@/lib/api");
      vi.mocked(api.submitAnswer).mockResolvedValueOnce({
        decision: FOLLOW_UP_DECISION,
      });

      await useSessionStore.getState().submitAnswer("retried answer");

      expect(useSessionStore.getState().turnError).toBeNull();
    });

    it("clears turnError at the start of a new submission even before the API responds", async () => {
      seedLiveSession();
      useSessionStore.setState({ turnError: "stale error" });
      const { api } = await import("@/lib/api");

      // Resolve on the next tick so we can inspect state mid-flight.
      let resolveCall!: (v: TurnResult) => void;
      vi.mocked(api.submitAnswer).mockReturnValueOnce(
        new Promise<TurnResult>((r) => { resolveCall = r; })
      );

      const submitPromise = useSessionStore.getState().submitAnswer("answer");

      // Immediately after submitAnswer is called, status is "thinking" and
      // turnError should already be cleared.
      expect(useSessionStore.getState().turnError).toBeNull();
      expect(useSessionStore.getState().status).toBe("thinking");

      // Let the API call resolve so we don't leak an unresolved promise.
      resolveCall({ decision: FOLLOW_UP_DECISION });
      await submitPromise;
    });
  });

  describe("submitAnswer — advance action", () => {
    it("increments currentIdx on an advance decision", async () => {
      seedLiveSession();
      const { api } = await import("@/lib/api");
      vi.mocked(api.submitAnswer).mockResolvedValueOnce({
        decision: ADVANCE_DECISION,
        evaluation: {
          questionId: "q1",
          transcript: "My answer.",
          rubricScores: { structure: 4, specificity: 3, impact: 4, ownership: 3 },
          weaknessTags: [],
          followUpCount: 0,
          wouldSurviveRealInterview: true,
          survivalReasoning: "Concrete example with measurable outcome.",
        },
      });

      await useSessionStore.getState().submitAnswer("My answer.");

      expect(useSessionStore.getState().currentIdx).toBe(1);
      expect(useSessionStore.getState().status).toBe("live");
    });
  });

  describe("submitAnswer — finalize failure (session/finalize 503, etc.)", () => {
    function seedFinalQuestionSession() {
      useSessionStore.setState({
        status: "live",
        role: "sde",
        mode: "full",
        level: "mid",
        profile: null,
        plan: MOCK_PLAN,
        currentIdx: 1, // last question — the decision below completes the session
        followUpCount: 0,
        messages: [],
        evaluations: [],
        priorMemory: null,
        updatedMemory: null,
        justRestored: false,
        turnError: null,
      });
    }

    it("surfaces a retryable error instead of leaving the session silently stuck", async () => {
      seedFinalQuestionSession();
      const { api } = await import("@/lib/api");
      vi.mocked(api.submitAnswer).mockResolvedValueOnce({
        decision: COMPLETE_DECISION,
        evaluation: MOCK_EVALUATION,
      });
      vi.mocked(api.finalizeSession).mockRejectedValueOnce(new Error("Request failed (503)"));

      // The bug: today nothing catches this rejection, so it either propagates
      // as an unhandled rejection out of submitAnswer, or (once fixed) resolves
      // cleanly with the failure surfaced in state instead.
      await expect(
        useSessionStore.getState().submitAnswer("My final answer.")
      ).resolves.toBeUndefined();

      const s = useSessionStore.getState();
      // Never silently pretend success.
      expect(s.status).not.toBe("complete");
      // The failure must be visible to the user, not swallowed.
      expect(s.turnError).toBe("Request failed (503)");
      // No memory update should be recorded from a failed finalize.
      expect(s.updatedMemory).toBeNull();
    });

    it("falls back to a generic message when finalizeSession rejects with a non-Error", async () => {
      seedFinalQuestionSession();
      const { api } = await import("@/lib/api");
      vi.mocked(api.submitAnswer).mockResolvedValueOnce({
        decision: COMPLETE_DECISION,
        evaluation: MOCK_EVALUATION,
      });
      vi.mocked(api.finalizeSession).mockRejectedValueOnce("just a string error");

      await useSessionStore.getState().submitAnswer("My final answer.");

      expect(useSessionStore.getState().turnError).toBe(
        "Request failed — please try again."
      );
    });
  });

  describe("retryFinalize", () => {
    function seedWrappingWithError() {
      useSessionStore.setState({
        status: "wrapping",
        role: "sde",
        mode: "full",
        level: "mid",
        profile: null,
        plan: MOCK_PLAN,
        currentIdx: 1,
        followUpCount: 0,
        messages: [],
        evaluations: [MOCK_EVALUATION],
        priorMemory: null,
        updatedMemory: null,
        justRestored: false,
        turnError: "Request failed (503)",
      });
    }

    it("retries finalizeSession and completes the session on success", async () => {
      seedWrappingWithError();
      const { api } = await import("@/lib/api");
      vi.mocked(api.finalizeSession).mockResolvedValueOnce(MOCK_MEMORY);

      await useSessionStore.getState().retryFinalize();

      const s = useSessionStore.getState();
      expect(s.status).toBe("complete");
      expect(s.turnError).toBeNull();
      expect(s.updatedMemory).toEqual(MOCK_MEMORY);
    });

    it("surfaces the error again if the retry also fails", async () => {
      seedWrappingWithError();
      const { api } = await import("@/lib/api");
      vi.mocked(api.finalizeSession).mockRejectedValueOnce(new Error("still down"));

      await useSessionStore.getState().retryFinalize();

      const s = useSessionStore.getState();
      expect(s.status).not.toBe("complete");
      expect(s.turnError).toBe("still down");
    });

    it("is a no-op when not in the wrapping state", async () => {
      seedFinalQuestionSession(); // status: "live"
      const { api } = await import("@/lib/api");

      await useSessionStore.getState().retryFinalize();

      expect(api.finalizeSession).not.toHaveBeenCalled();
    });

    function seedFinalQuestionSession() {
      useSessionStore.setState({
        status: "live",
        plan: MOCK_PLAN,
        currentIdx: 1,
        evaluations: [],
        turnError: null,
      });
    }

    it("ignores a second retryFinalize call while one is already in flight (no duplicate finalize)", async () => {
      seedWrappingWithError();
      const { api } = await import("@/lib/api");
      let resolveCall!: (v: MemoryProfile) => void;
      vi.mocked(api.finalizeSession).mockReturnValueOnce(
        new Promise<MemoryProfile>((r) => { resolveCall = r; })
      );

      // Two near-simultaneous clicks of the RETRY button — the backend's
      // finalize is not idempotent for duplicate identical calls (it re-runs
      // the Memory Agent merge and writes each time), so a second in-flight
      // call must be a no-op, not a second network request.
      const first = useSessionStore.getState().retryFinalize();
      const second = useSessionStore.getState().retryFinalize();

      resolveCall(MOCK_MEMORY);
      await Promise.all([first, second]);

      expect(api.finalizeSession).toHaveBeenCalledTimes(1);
      expect(useSessionStore.getState().status).toBe("complete");
    });

    it("exposes a finalizing flag that is true only while the call is in flight", async () => {
      seedWrappingWithError();
      const { api } = await import("@/lib/api");
      let resolveCall!: (v: MemoryProfile) => void;
      vi.mocked(api.finalizeSession).mockReturnValueOnce(
        new Promise<MemoryProfile>((r) => { resolveCall = r; })
      );

      expect(useSessionStore.getState().finalizing).toBe(false);
      const promise = useSessionStore.getState().retryFinalize();
      expect(useSessionStore.getState().finalizing).toBe(true);

      resolveCall(MOCK_MEMORY);
      await promise;

      expect(useSessionStore.getState().finalizing).toBe(false);
    });
  });

  describe("persistence — a failed finalize must not look like a successful one after reload", () => {
    beforeEach(() => {
      localStorage.clear();
    });

    function readPersisted(): { state: Record<string, unknown> } {
      const raw = localStorage.getItem("crucible.session.v1");
      expect(raw).not.toBeNull();
      return JSON.parse(raw!);
    }

    it("does not persist status as 'complete' when wrapping ended in a finalize error", async () => {
      useSessionStore.setState({
        status: "live",
        role: "sde",
        mode: "full",
        level: "mid",
        profile: null,
        plan: MOCK_PLAN,
        currentIdx: 1,
        followUpCount: 0,
        messages: [],
        evaluations: [],
        priorMemory: null,
        updatedMemory: null,
        justRestored: false,
        turnError: null,
      });
      const { api } = await import("@/lib/api");
      vi.mocked(api.submitAnswer).mockResolvedValueOnce({
        decision: COMPLETE_DECISION,
        evaluation: MOCK_EVALUATION,
      });
      vi.mocked(api.finalizeSession).mockRejectedValueOnce(new Error("Request failed (503)"));

      await useSessionStore.getState().submitAnswer("My final answer.");

      // Sanity: the in-memory store is in the expected failed-wrapping state.
      expect(useSessionStore.getState().status).toBe("wrapping");
      expect(useSessionStore.getState().turnError).toBe("Request failed (503)");

      // The bug: partialize unconditionally remaps "wrapping" -> "complete"
      // on every write, and never persists turnError — so a reload after a
      // failed finalize would rehydrate as a normal completed session (the
      // "status === complete" effect fires and navigates to /results) with
      // no error and no way to retry. That's silently WRONG, not just stuck.
      const persisted = readPersisted();
      expect(persisted.state.status).not.toBe("complete");
      expect(persisted.state.turnError).toBe("Request failed (503)");
    });

    it("still persists status as 'complete' when finalize actually succeeded", async () => {
      useSessionStore.setState({
        status: "live",
        role: "sde",
        mode: "full",
        level: "mid",
        profile: null,
        plan: MOCK_PLAN,
        currentIdx: 1,
        followUpCount: 0,
        messages: [],
        evaluations: [],
        priorMemory: null,
        updatedMemory: null,
        justRestored: false,
        turnError: null,
      });
      const { api } = await import("@/lib/api");
      vi.mocked(api.submitAnswer).mockResolvedValueOnce({
        decision: COMPLETE_DECISION,
        evaluation: MOCK_EVALUATION,
      });
      vi.mocked(api.finalizeSession).mockResolvedValueOnce(MOCK_MEMORY);

      await useSessionStore.getState().submitAnswer("My final answer.");

      expect(useSessionStore.getState().status).toBe("complete");
      const persisted = readPersisted();
      expect(persisted.state.status).toBe("complete");
    });
  });

  describe("warm-up exchange", () => {
    function seedWarmup() {
      useSessionStore.setState({
        status: "live",
        warmup: true,
        mode: "full",
        level: "mid",
        plan: MOCK_PLAN,
        currentIdx: 0,
        followUpCount: 0,
        messages: [
          { id: "g", speaker: "interviewer", kind: "question", text: "Hi Karthik, how are you?", questionId: "intro" },
        ],
        evaluations: [],
        turnError: null,
      });
    }

    it("treats the small-talk reply as unscored and moves into Q1", async () => {
      seedWarmup();
      const { api } = await import("@/lib/api");

      await useSessionStore.getState().submitAnswer("I'm doing great, thanks!");

      const s = useSessionStore.getState();
      expect(s.warmup).toBe(false);
      expect(s.evaluations).toHaveLength(0); // never scored
      expect(api.submitAnswer).not.toHaveBeenCalled(); // no pipeline call
      // the last message is the first real question
      const last = s.messages[s.messages.length - 1];
      expect(last.speaker).toBe("interviewer");
      expect(last.questionId).toBe("q1");
      expect(last.text).toContain(MOCK_PLAN.questions[0].prompt);
    });

    it("records the candidate's small-talk under the intro, not a question", async () => {
      seedWarmup();
      await useSessionStore.getState().submitAnswer("Good!");
      const candidateMsgs = useSessionStore.getState().messages.filter((m) => m.speaker === "candidate");
      expect(candidateMsgs).toHaveLength(1);
      expect(candidateMsgs[0].questionId).toBe("intro");
    });
  });

  describe("start — clears stale finalize-error state from a prior session", () => {
    // turnError is persisted to localStorage as of the finalize-error fix
    // above (finalizing is not persisted, always rehydrates false). Without
    // resetting turnError here, a leftover
    // "Request failed (503)" from a previous session's failed finalize would
    // otherwise bleed into the error banner of a brand-new session.
    it("resets turnError and finalizing when starting a new session", async () => {
      useSessionStore.setState({ turnError: "stale finalize error", finalizing: true });
      const { api } = await import("@/lib/api");
      vi.mocked(api.getMemory).mockResolvedValue({
        candidateId: "test-candidate-id",
        recurringWeaknesses: [],
        improvementTrend: [],
        strongAreas: [],
      });
      vi.mocked(api.startSession).mockResolvedValueOnce({
        profile: {
          candidateSkills: [],
          yearsExperience: 3,
          projectHighlights: [],
          targetRole: "sde",
          jdRequirements: [],
          resumeToJdGaps: [],
        },
        plan: MOCK_PLAN,
      });

      await useSessionStore.getState().start({
        resumeText: "resume",
        jdText: "jd",
        role: "sde",
        mode: "full",
        level: "mid",
        candidateName: "Karthik",
        useVideo: false,
      });

      const s = useSessionStore.getState();
      expect(s.turnError).toBeNull();
      expect(s.finalizing).toBe(false);
    });
  });
});
