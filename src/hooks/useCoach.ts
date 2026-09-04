import { useCallback, useState } from "react";
import { api } from "@/lib/api";
import type { CoachResponse, PlannedQuestion } from "@/types/contracts";

// Wraps the /api/coach call (rewrites the candidate's answer into a 5/5
// model answer) behind a hook so CoachPanel stays pure UI, per the
// components/ = no API calls rule. Mirrors useTavus's shape: state via
// useState, plus an exposed action (`load`) the component drives itself
// (once on mount, again on retry).

export type CoachState =
  | { phase: "loading" }
  | { phase: "error" }
  | { phase: "ready"; data: CoachResponse };

export function useCoach(input: {
  question: PlannedQuestion;
  transcript: string;
  weaknessTags: string[];
}) {
  const [state, setState] = useState<CoachState>({ phase: "loading" });

  // Each call to `load()` gets its OWN `cancelled` flag, scoped to that call's
  // closure — not a single ref shared/reset across calls. That per-call
  // isolation matters under StrictMode, which double-invokes
  // `useEffect(load, [])` in dev: mount -> load() #1 fires request A ->
  // cleanup calls #1's returned canceller (its own `cancelled` flips true)
  // -> mount again -> load() #2 fires request B with its own independent
  // flag. If a shared ref were reset to false at the top of #2, #1's
  // cancellation would be silently undone and a stale response from A could
  // overwrite B's result.
  const load = useCallback(() => {
    setState({ phase: "loading" });
    let cancelled = false;
    api
      .coachAnswer(input)
      .then((data) => {
        if (!cancelled) setState({ phase: "ready", data });
      })
      .catch((err) => {
        console.error("[useCoach] coachAnswer failed:", err);
        if (!cancelled) setState({ phase: "error" });
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [input.question, input.transcript, input.weaknessTags]);

  return { state, load };
}
