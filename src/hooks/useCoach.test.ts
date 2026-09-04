import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import type { CoachResponse, PlannedQuestion } from "@/types/contracts";

vi.mock("@/lib/api", () => ({
  api: { coachAnswer: vi.fn() },
}));

import { api } from "@/lib/api";
import { useCoach } from "./useCoach";

function question(): PlannedQuestion {
  return {
    id: "q1",
    type: "behavioral",
    prompt: "Tell me about a time you disagreed with a teammate.",
    targetDifficulty: 3,
    weightedFromWeakness: false,
  };
}

describe("useCoach", () => {
  beforeEach(() => {
    vi.mocked(api.coachAnswer).mockReset();
  });

  it("starts in the loading phase", () => {
    vi.mocked(api.coachAnswer).mockReturnValue(new Promise(() => {})); // never resolves
    const { result } = renderHook(() =>
      useCoach({ question: question(), transcript: "t", weaknessTags: [] })
    );
    expect(result.current.state.phase).toBe("loading");
  });

  it("transitions loading -> ready with the resolved data on success", async () => {
    const response: CoachResponse = { modelAnswer: "A model 5/5 answer.", improvements: ["Be specific"] };
    vi.mocked(api.coachAnswer).mockResolvedValueOnce(response);

    const { result } = renderHook(() =>
      useCoach({ question: question(), transcript: "t", weaknessTags: [] })
    );

    act(() => {
      result.current.load();
    });

    await waitFor(() => expect(result.current.state.phase).toBe("ready"));
    const state = result.current.state;
    if (state.phase !== "ready") throw new Error("expected ready phase");
    expect(state.data).toEqual(response);
  });

  it("transitions loading -> error when the call rejects, and retry via load() can recover", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    vi.mocked(api.coachAnswer).mockRejectedValueOnce(new Error("network down"));

    const { result } = renderHook(() =>
      useCoach({ question: question(), transcript: "t", weaknessTags: [] })
    );

    act(() => {
      result.current.load();
    });

    await waitFor(() => expect(result.current.state.phase).toBe("error"));
    expect(errorSpy).toHaveBeenCalled();

    const response: CoachResponse = { modelAnswer: "Recovered.", improvements: [] };
    vi.mocked(api.coachAnswer).mockResolvedValueOnce(response);

    act(() => {
      result.current.load();
    });

    await waitFor(() => expect(result.current.state.phase).toBe("ready"));
    errorSpy.mockRestore();
  });

  // Regression test: a shared/reset-on-every-call "cancelled" ref would let a
  // second load() call silently undo the first call's cancellation, letting a
  // slow first request's stale response overwrite a faster second request's
  // correct result. This is exactly the shape of React StrictMode's dev-only
  // mount -> effect cleanup -> mount double-invoke of `useEffect(load, [])`.
  it("does not let a cancelled call's late response overwrite a newer call's result", async () => {
    let resolveFirst!: (v: CoachResponse) => void;
    let resolveSecond!: (v: CoachResponse) => void;
    const first = new Promise<CoachResponse>((res) => {
      resolveFirst = res;
    });
    const second = new Promise<CoachResponse>((res) => {
      resolveSecond = res;
    });

    vi.mocked(api.coachAnswer).mockReturnValueOnce(first).mockReturnValueOnce(second);

    const { result } = renderHook(() =>
      useCoach({ question: question(), transcript: "t", weaknessTags: [] })
    );

    let cancelFirst!: () => void;
    act(() => {
      cancelFirst = result.current.load(); // call #1, fires request A
    });

    // Simulate the effect's cleanup running (StrictMode remount, or a real
    // unmount/prop-change) before request A has resolved.
    act(() => {
      cancelFirst();
    });

    act(() => {
      result.current.load(); // call #2, fires request B — independent flag
    });

    // Request B (the newer call) resolves first, as it normally would.
    await act(async () => {
      resolveSecond({ modelAnswer: "correct (B)", improvements: [] });
      await second;
    });
    await waitFor(() => expect(result.current.state.phase).toBe("ready"));

    let state = result.current.state;
    if (state.phase !== "ready") throw new Error("expected ready phase");
    expect(state.data.modelAnswer).toBe("correct (B)");

    // Now the stale, cancelled request A resolves late.
    await act(async () => {
      resolveFirst({ modelAnswer: "stale (A)", improvements: [] });
      await first;
    });

    // A's cancelled response must not have clobbered B's result.
    state = result.current.state;
    if (state.phase !== "ready") throw new Error("expected ready phase");
    expect(state.data.modelAnswer).toBe("correct (B)");
  });
});
