import { useCallback, useRef, useState } from "react";
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
  const cancelledRef = useRef(false);

  const load = useCallback(() => {
    cancelledRef.current = false;
    setState({ phase: "loading" });
    api
      .coachAnswer(input)
      .then((data) => {
        if (!cancelledRef.current) setState({ phase: "ready", data });
      })
      .catch(() => {
        if (!cancelledRef.current) setState({ phase: "error" });
      });
    return () => {
      cancelledRef.current = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [input.question, input.transcript, input.weaknessTags]);

  return { state, load };
}
