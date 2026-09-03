"""Runnable accuracy eval for the Interviewer + Evaluator agents.

Measures decision accuracy against backend/evals/golden/interview_turn_cases.json
by calling the real InterviewerAgent and EvaluatorAgent (real Bedrock calls, real
prompts) rather than reimplementing the call. Writes a JSON report per variant to
backend/evals/results/interview_turn_<variant>.json.

Usage:
    python -m evals.run_accuracy_eval [variant_label]

Costs real money (Bedrock Sonnet calls) — run deliberately, not in CI.
"""

import json
import sys
import time
from pathlib import Path
from statistics import mean

from anthropic import AnthropicBedrock

from config.settings import Settings
from llm.client import LLMClient
from models.contracts import PlannedQuestion
from agents.interviewer import InterviewerAgent
from agents.evaluator import EvaluatorAgent

CASES_FILE = Path(__file__).parent / "golden" / "interview_turn_cases.json"
RESULTS_DIR = Path(__file__).parent / "results"

# Anthropic list pricing for claude-sonnet-4-6 ($/MTok). Bedrock pricing may
# differ slightly; this is an estimate, not a billed figure.
PRICE_IN_PER_MTOK = 3.00
PRICE_OUT_PER_MTOK = 15.00


class _TrackedMessages:
    def __init__(self, real_messages, usage_log: list[dict]):
        self._real = real_messages
        self._usage_log = usage_log

    def create(self, **kwargs):
        response = self._real.create(**kwargs)
        self._usage_log.append({
            "model": kwargs.get("model"),
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        })
        return response


class _TrackedClient:
    def __init__(self, real_client, usage_log: list[dict]):
        self.messages = _TrackedMessages(real_client.messages, usage_log)


def main(variant_label: str = "baseline", cases_file: Path = CASES_FILE) -> dict:
    settings = Settings()
    usage_log: list[dict] = []
    real_client = AnthropicBedrock(aws_region=settings.aws_region)
    llm = LLMClient(client=_TrackedClient(real_client, usage_log), region=settings.aws_region)

    interviewer = InterviewerAgent(llm=llm, model=settings.interviewer_model)
    evaluator = EvaluatorAgent(llm=llm, model=settings.evaluator_model)

    cases = json.loads(cases_file.read_text())
    results = []

    print(f"Running {len(cases)} cases against interviewer_model={settings.interviewer_model} "
          f"evaluator_model={settings.evaluator_model} region={settings.aws_region}\n")

    for case in cases:
        question = PlannedQuestion.model_validate(case["question"])

        t0 = time.monotonic()
        decision = interviewer.run_turn(
            question=question,
            candidate_answer=case["candidate_answer"],
            follow_up_count=case["follow_up_count"],
            is_last_question=case["is_last_question"],
        )
        interviewer_latency = time.monotonic() - t0
        action_match = decision.action == case["expect_action"]

        would_survive_actual = None
        would_survive_match = None
        evaluator_latency = None
        if case["expect_would_survive"] is not None:
            t1 = time.monotonic()
            evaluation = evaluator.run(
                question=question,
                transcript=case["candidate_answer"],
                follow_up_count=case["follow_up_count"],
            )
            evaluator_latency = time.monotonic() - t1
            would_survive_actual = evaluation.would_survive_real_interview
            would_survive_match = would_survive_actual == case["expect_would_survive"]

        row = {
            "case": case["case"],
            "type": case["question"]["type"],
            "expected_action": case["expect_action"],
            "actual_action": decision.action,
            "action_match": action_match,
            "expected_would_survive": case["expect_would_survive"],
            "actual_would_survive": would_survive_actual,
            "would_survive_match": would_survive_match,
            "interviewer_latency_s": round(interviewer_latency, 2),
            "evaluator_latency_s": round(evaluator_latency, 2) if evaluator_latency is not None else None,
        }
        results.append(row)

        ok = action_match and (would_survive_match is None or would_survive_match)
        detail = f"action={decision.action} (expected {case['expect_action']})"
        if would_survive_match is not None:
            detail += f", survive={would_survive_actual} (expected {case['expect_would_survive']})"
        print(f"  {'PASS' if ok else 'FAIL'}  {case['case']}: {detail}")

    action_accuracy = mean(1.0 if r["action_match"] else 0.0 for r in results)
    survive_cases = [r for r in results if r["would_survive_match"] is not None]
    survive_accuracy = (
        mean(1.0 if r["would_survive_match"] else 0.0 for r in survive_cases)
        if survive_cases else None
    )

    total_input = sum(u["input_tokens"] for u in usage_log)
    total_output = sum(u["output_tokens"] for u in usage_log)
    estimated_cost = (total_input / 1_000_000) * PRICE_IN_PER_MTOK + (
        total_output / 1_000_000
    ) * PRICE_OUT_PER_MTOK

    summary = {
        "variant": variant_label,
        "case_count": len(results),
        "action_accuracy": round(action_accuracy, 4),
        "action_correct": sum(1 for r in results if r["action_match"]),
        "survive_accuracy": round(survive_accuracy, 4) if survive_accuracy is not None else None,
        "survive_correct": sum(1 for r in survive_cases if r["would_survive_match"]),
        "survive_case_count": len(survive_cases),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "estimated_cost_usd": round(estimated_cost, 4),
        "results": results,
    }

    RESULTS_DIR.mkdir(exist_ok=True)
    out_file = RESULTS_DIR / f"interview_turn_{variant_label}.json"
    out_file.write_text(json.dumps(summary, indent=2))

    print()
    print(f"Interviewer action accuracy: {action_accuracy:.1%} "
          f"({summary['action_correct']}/{summary['case_count']})")
    if survive_accuracy is not None:
        print(f"Evaluator wouldSurviveRealInterview accuracy: {survive_accuracy:.1%} "
              f"({summary['survive_correct']}/{summary['survive_case_count']})")
    print(f"Tokens: {total_input} in / {total_output} out — est. cost ${estimated_cost:.4f}")
    print(f"Results written to {out_file}")

    return summary


if __name__ == "__main__":
    label = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    file_arg = Path(sys.argv[2]) if len(sys.argv) > 2 else CASES_FILE
    main(label, file_arg)
