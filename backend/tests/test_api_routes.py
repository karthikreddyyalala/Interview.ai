import pytest
from fastapi.testclient import TestClient

import auth
from app import create_app
from store.in_memory import InMemoryStore


@pytest.fixture(autouse=True)
def _clear_auth_cache():
    auth._settings.cache_clear()
    yield
    auth._settings.cache_clear()


class _FakeLLM:
    """Returns a canned payload selected by the requested schema name."""

    def __init__(self, payloads: dict):
        self._payloads = payloads

    def structured(self, *, model, system, user, schema, max_tokens=2000):
        return schema.model_validate(self._payloads[schema.__name__])


_INTAKE = {
    "candidateSkills": ["Go", "Postgres"],
    "yearsExperience": 4,
    "projectHighlights": [
        {"title": "Pricing engine", "description": "Cut p99 latency", "technologies": ["Go"]}
    ],
    "targetRole": "Software Engineer",
    "targetCompany": "Stripe",
    "jdRequirements": ["distributed systems"],
    "resumeToJdGaps": ["thin incident postmortem evidence"],
}

_PLAN = {
    "sessionId": "sess-1",
    "questions": [
        {
            "id": "q0",
            "type": "behavioral",
            "prompt": "Tell me about a deadline you owned.",
            "targetDifficulty": 3,
            "weightedFromWeakness": False,
        }
    ],
}

_DECISION_FOLLOW_UP = {
    "action": "follow_up",
    "followUpPrompt": "What did YOU specifically do?",
    "currentQuestionId": "q0",
}

_DECISION_ADVANCE = {
    "action": "advance",
    "followUpPrompt": None,
    "currentQuestionId": "q0",
}

_EVAL = {
    "questionId": "q0",
    "transcript": "I led a 4-person team and cut latency 80%.",
    "rubricScores": {"structure": 4.0, "specificity": 4.0, "impact": 4.0, "ownership": 4.0},
    "weaknessTags": [],
    "followUpCount": 1,
    "wouldSurviveRealInterview": True,
    "survivalReasoning": "Specific, quantified, clear ownership.",
}

_MEMORY = {
    "candidateId": "local-dev",
    "recurringWeaknesses": [{"tag": "no-edge-cases", "frequency": 1, "lastSeen": "2026-06-22"}],
    "improvementTrend": [{"sessionDate": "2026-06-22", "avgScore": 4.0}],
    "strongAreas": ["ownership"],
}

_QUESTION_BODY = {
    "id": "q0",
    "type": "behavioral",
    "prompt": "Tell me about a deadline you owned.",
    "targetDifficulty": 3,
    "weightedFromWeakness": False,
}


def _client(payloads: dict, store: InMemoryStore | None = None) -> TestClient:
    return TestClient(create_app(llm=_FakeLLM(payloads), store=store or InMemoryStore()))


def test_start_session_returns_profile_and_plan():
    client = _client({"IntakeProfile": _INTAKE, "QuestionPlan": _PLAN})
    res = client.post(
        "/api/session/start",
        json={"resumeText": "resume here", "jdText": "jd here", "role": "sde"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["profile"]["targetRole"] == "Software Engineer"
    assert body["profile"]["resumeToJdGaps"]  # camelCase preserved
    assert body["plan"]["questions"][0]["id"] == "q0"


def test_turn_follow_up_returns_no_evaluation():
    client = _client({"InterviewDecision": _DECISION_FOLLOW_UP})
    res = client.post(
        "/api/session/turn",
        json={
            "question": _QUESTION_BODY,
            "answer": "We just got it done.",
            "followUpCount": 0,
            "isLast": False,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["decision"]["action"] == "follow_up"
    assert body["decision"]["followUpPrompt"]
    assert body["evaluation"] is None


def test_turn_advance_returns_evaluation():
    client = _client({"InterviewDecision": _DECISION_ADVANCE, "AnswerEvaluation": _EVAL})
    res = client.post(
        "/api/session/turn",
        json={
            "question": _QUESTION_BODY,
            "answer": "I led a 4-person team and cut latency 80%.",
            "followUpCount": 1,
            "isLast": False,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["decision"]["action"] == "advance"
    assert body["evaluation"]["wouldSurviveRealInterview"] is True
    assert body["evaluation"]["survivalReasoning"]


def test_finalize_returns_memory_profile():
    client = _client({"MemoryProfile": _MEMORY})
    res = client.post(
        "/api/session/finalize",
        json={"evaluations": [_EVAL], "priorMemory": None},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["candidateId"] == "local-dev"
    assert body["recurringWeaknesses"][0]["tag"] == "no-edge-cases"
    assert body["improvementTrend"][0]["avgScore"] == 4.0


def test_healthcheck():
    client = _client({})
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_auth_required_rejects_missing_token(monkeypatch):
    monkeypatch.setenv("INTERVIEWAI_AUTH_REQUIRED", "true")
    monkeypatch.setenv("INTERVIEWAI_COGNITO_USER_POOL_ID", "us-west-2_test")
    monkeypatch.setenv("INTERVIEWAI_COGNITO_CLIENT_ID", "testclient")
    auth._settings.cache_clear()
    client = _client({"IntakeProfile": _INTAKE, "QuestionPlan": _PLAN})
    res = client.post("/api/session/start",
                      json={"resumeText": "r", "jdText": "j", "role": "sde"})
    assert res.status_code == 401


def test_health_public_even_when_auth_required(monkeypatch):
    monkeypatch.setenv("INTERVIEWAI_AUTH_REQUIRED", "true")
    monkeypatch.setenv("INTERVIEWAI_COGNITO_USER_POOL_ID", "us-west-2_test")
    monkeypatch.setenv("INTERVIEWAI_COGNITO_CLIENT_ID", "testclient")
    auth._settings.cache_clear()
    client = _client({})
    res = client.get("/api/health")
    assert res.status_code == 200


def test_finalize_persists_memory_to_store():
    store = InMemoryStore()
    client = _client({"MemoryProfile": {**_MEMORY, "candidateId": "cand-xyz"}}, store=store)
    res = client.post(
        "/api/session/finalize",
        json={"candidateId": "cand-xyz", "evaluations": [_EVAL]},
    )
    assert res.status_code == 200
    # the aggregated profile is now durable in the store
    saved = store.get_memory("cand-xyz")
    assert saved is not None
    assert saved.recurring_weaknesses[0].tag == "no-edge-cases"


class _RacingStoreOnce(InMemoryStore):
    """Wraps InMemoryStore to inject exactly one concurrent write right after
    the first get_memory_with_version() call, simulating a second finalize
    request for the same candidate that completes in the gap between our
    read and our write (two browser tabs, or a retried request that already
    succeeded server-side)."""

    def __init__(self, race_profile):
        super().__init__()
        self._race_profile = race_profile
        self._raced = False

    def get_memory_with_version(self, candidate_id):
        result = super().get_memory_with_version(candidate_id)
        if not self._raced:
            self._raced = True
            super().put_memory(self._race_profile)
        return result


class _AlwaysRacingStore(InMemoryStore):
    """Every read is immediately followed by a concurrent write, so every
    put_memory_cas attempt is guaranteed to conflict — used to prove the
    retry loop is bounded rather than infinite."""

    def __init__(self, race_profile):
        super().__init__()
        self._race_profile = race_profile

    def get_memory_with_version(self, candidate_id):
        result = super().get_memory_with_version(candidate_id)
        super().put_memory(self._race_profile)
        return result


class _CountingMemoryLLM(_FakeLLM):
    """Like _FakeLLM but records the `user` prompt passed to every
    MemoryProfile (Memory Agent) call, so tests can assert both how many
    times the (expensive, LLM-backed) recompute ran and what state it saw."""

    def __init__(self, payloads: dict):
        super().__init__(payloads)
        self.memory_calls: list[str] = []

    def structured(self, *, model, system, user, schema, max_tokens=2000):
        if schema.__name__ == "MemoryProfile":
            self.memory_calls.append(user)
        return super().structured(model=model, system=system, user=user, schema=schema, max_tokens=max_tokens)


def test_finalize_retries_after_one_conflict_and_does_not_lose_data():
    """The core fix: a version conflict on put_memory must not be swallowed
    or clobber the concurrent write. finalize should re-read fresh state,
    re-run the Memory Agent against it, and succeed on retry."""
    from models.contracts import MemoryProfile

    race_profile = MemoryProfile.model_validate(
        {
            "candidateId": "cand-race",
            "recurringWeaknesses": [{"tag": "concurrent-writer-tag", "frequency": 1, "lastSeen": "2026-08-01"}],
            "improvementTrend": [],
            "strongAreas": [],
        }
    )
    store = _RacingStoreOnce(race_profile)
    llm = _CountingMemoryLLM({"MemoryProfile": {**_MEMORY, "candidateId": "cand-race"}})
    app = create_app(llm=llm, store=store)
    client = TestClient(app)

    res = client.post(
        "/api/session/finalize",
        json={"candidateId": "cand-race", "evaluations": [_EVAL]},
    )
    assert res.status_code == 200

    # The Memory Agent had to be re-run against fresh state after the
    # conflict — this is the deliberate design decision (the "modify" step
    # genuinely depends on the post-race state), not an accident.
    assert len(llm.memory_calls) == 2
    assert "concurrent-writer-tag" not in llm.memory_calls[0]
    assert "concurrent-writer-tag" in llm.memory_calls[1]

    # Final state is the second (post-conflict) computation's result, not a
    # silent no-op and not the race profile alone.
    saved = store.get_memory("cand-race")
    assert saved is not None
    assert saved.recurring_weaknesses[0].tag == "no-edge-cases"


def test_finalize_gives_up_loudly_after_exhausting_retries():
    """If conflicts keep happening past the bound, finalize must fail loudly
    (surface as an error) rather than silently drop the update or loop
    forever."""
    from models.contracts import MemoryProfile

    race_profile = MemoryProfile.model_validate(
        {
            "candidateId": "cand-hammered",
            "recurringWeaknesses": [],
            "improvementTrend": [],
            "strongAreas": [],
        }
    )
    store = _AlwaysRacingStore(race_profile)
    llm = _CountingMemoryLLM({"MemoryProfile": {**_MEMORY, "candidateId": "cand-hammered"}})
    app = create_app(llm=llm, store=store)
    client = TestClient(app, raise_server_exceptions=False)

    res = client.post(
        "/api/session/finalize",
        json={"candidateId": "cand-hammered", "evaluations": [_EVAL]},
    )

    assert res.status_code == 503  # clean, readable, retryable — not a hang or a 500 stack trace
    # Bounded: a small, explicit number of attempts, not infinite.
    assert 1 < len(llm.memory_calls) <= 5


def test_get_memory_returns_persisted_profile():
    store = InMemoryStore()
    client = _client({"MemoryProfile": {**_MEMORY, "candidateId": "cand-abc"}}, store=store)
    # nothing yet -> empty profile, not 404
    empty = client.get("/api/memory/cand-abc")
    assert empty.status_code == 200
    assert empty.json()["candidateId"] == "cand-abc"
    assert empty.json()["recurringWeaknesses"] == []

    # after finalize it returns the saved profile
    client.post("/api/session/finalize", json={"candidateId": "cand-abc", "evaluations": [_EVAL]})
    loaded = client.get("/api/memory/cand-abc")
    assert loaded.status_code == 200
    assert loaded.json()["recurringWeaknesses"][0]["tag"] == "no-edge-cases"


def test_start_loads_prior_memory_from_store():
    """Planner must receive the candidate's persisted weaknesses, proving the
    cross-session loop is server-side, not browser-side."""
    store = InMemoryStore()
    captured: dict = {}

    class _CapturingLLM(_FakeLLM):
        def structured(self, *, model, system, user, schema, max_tokens=2000):
            if schema.__name__ == "QuestionPlan":
                captured["planner_user"] = user
            return super().structured(
                model=model, system=system, user=user, schema=schema, max_tokens=max_tokens
            )

    # seed a prior memory for this candidate
    from models.contracts import MemoryProfile

    store.put_memory(
        MemoryProfile.model_validate(
            {
                "candidateId": "returning",
                "recurringWeaknesses": [{"tag": "no-edge-cases", "frequency": 3, "lastSeen": "2026-06-01"}],
                "improvementTrend": [],
                "strongAreas": [],
            }
        )
    )

    app = create_app(llm=_CapturingLLM({"IntakeProfile": _INTAKE, "QuestionPlan": _PLAN}), store=store)
    client = TestClient(app)
    res = client.post(
        "/api/session/start",
        json={"resumeText": "r", "jdText": "j", "role": "sde", "candidateId": "returning"},
    )
    assert res.status_code == 200
    # the planner prompt carried the persisted weakness
    assert "no-edge-cases" in captured["planner_user"]


class _RaisingLLM:
    """Simulates an agent call that fails even after retries are exhausted."""

    def structured(self, *, model, system, user, schema, max_tokens=2000):
        raise RuntimeError("bedrock throttled: too many requests")


def test_turn_agent_failure_returns_clean_503():
    # A raised agent error must surface as a clean, retryable JSON error the
    # browser can read — never an opaque crash that hangs the interview.
    app = create_app(llm=_RaisingLLM(), store=InMemoryStore())
    client = TestClient(app, raise_server_exceptions=False)
    res = client.post(
        "/api/session/turn",
        json={
            "question": _QUESTION_BODY,
            "answer": "some answer",
            "followUpCount": 0,
            "isLast": False,
        },
    )
    assert res.status_code == 503
    body = res.json()
    assert "detail" in body
    # Human-readable, not a stack trace or "Internal Server Error"
    assert "interviewer" in body["detail"].lower() or "try again" in body["detail"].lower()


def test_start_session_failure_returns_clean_503():
    app = create_app(llm=_RaisingLLM(), store=InMemoryStore())
    client = TestClient(app, raise_server_exceptions=False)
    res = client.post(
        "/api/session/start",
        json={"resumeText": "r", "jdText": "j", "role": "sde"},
    )
    assert res.status_code == 503
    assert "detail" in res.json()


# --- per-session history ---------------------------------------------------

_QUESTIONS_BODY = [
    {"id": "q0", "type": "behavioral", "prompt": "Tell me about a deadline.",
     "targetDifficulty": 3, "weightedFromWeakness": False},
    {"id": "q1", "type": "technical", "prompt": "Explain a hash map.",
     "targetDifficulty": 3, "weightedFromWeakness": False},
]

_EVALS_BODY = [
    {"questionId": "q0", "transcript": "Q: Tell me...\nA: I shipped it.",
     "rubricScores": {"structure": 4.0}, "weaknessTags": [], "followUpCount": 0,
     "wouldSurviveRealInterview": True, "survivalReasoning": "Solid."},
    {"questionId": "q1", "transcript": "Q: Explain...\nA: buckets.",
     "rubricScores": {"correctness": 2.0}, "weaknessTags": ["shallow-depth"],
     "followUpCount": 1, "wouldSurviveRealInterview": False, "survivalReasoning": "Thin."},
]


def _finalize_body(session_id="sess-1"):
    return {
        "candidateId": "local-dev",
        "sessionId": session_id,
        "mode": "full",
        "level": "mid",
        "questions": _QUESTIONS_BODY,
        "evaluations": _EVALS_BODY,
    }


def test_finalize_persists_session_and_lists_it():
    store = InMemoryStore()
    client = _client({"MemoryProfile": _MEMORY}, store=store)

    fin = client.post("/api/session/finalize", json=_finalize_body("sess-1"))
    assert fin.status_code == 200

    listed = client.get("/api/sessions")
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["sessionId"] == "sess-1"
    assert rows[0]["survived"] == 1 and rows[0]["total"] == 2
    assert rows[0]["mode"] == "full" and rows[0]["level"] == "mid"


def test_get_session_returns_full_record_with_transcripts():
    store = InMemoryStore()
    client = _client({"MemoryProfile": _MEMORY}, store=store)
    client.post("/api/session/finalize", json=_finalize_body("sess-abc"))

    res = client.get("/api/sessions/sess-abc")
    assert res.status_code == 200
    body = res.json()
    assert body["sessionId"] == "sess-abc"
    assert len(body["questions"]) == 2
    assert body["evaluations"][0]["transcript"].startswith("Q: Tell me")
    assert body["evaluations"][1]["wouldSurviveRealInterview"] is False


def test_get_missing_session_returns_404():
    client = _client({"MemoryProfile": _MEMORY}, store=InMemoryStore())
    res = client.get("/api/sessions/does-not-exist")
    assert res.status_code == 404


def test_sessions_list_empty_when_none():
    client = _client({"MemoryProfile": _MEMORY}, store=InMemoryStore())
    res = client.get("/api/sessions")
    assert res.status_code == 200
    assert res.json() == []


# --- coach (model answer) --------------------------------------------------

_COACH_PAYLOAD = {
    "modelAnswer": "When our service had duplicate alerts, I owned it. I added Redis "
                   "idempotency and cut duplicates from 4% to 0.3% in a month.",
    "improvements": ["Imposed STAR structure.", "Added a measurable 4%->0.3% impact."],
}


def test_coach_returns_model_answer_and_improvements():
    client = _client({"CoachResponse": _COACH_PAYLOAD})
    res = client.post(
        "/api/coach",
        json={
            "question": _QUESTION_BODY,
            "transcript": "Q: Tell me...\nA: It got better.",
            "weaknessTags": ["vague-impact", "no-star-structure"],
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert "0.3%" in body["modelAnswer"]
    assert len(body["improvements"]) == 2


def test_coach_works_without_weakness_tags():
    client = _client({"CoachResponse": _COACH_PAYLOAD})
    res = client.post(
        "/api/coach",
        json={"question": _QUESTION_BODY, "transcript": "Some answer."},
    )
    assert res.status_code == 200
    assert res.json()["modelAnswer"]


# --- tavus avatar session --------------------------------------------------

class _FakeTavus:
    def __init__(self):
        self.calls = 0

    def create_conversation(self, *, replica_id, persona_id="", conversation_name="Crucible interview"):
        self.calls += 1
        from llm.tavus import TavusResult
        return TavusResult(conversation_id="conv-123", conversation_url="https://tavus.daily.co/room-abc")


def test_avatar_session_disabled_by_default():
    # No Tavus key configured -> endpoint reports disabled, UI falls back.
    client = _client({})
    res = client.post("/api/avatar/session")
    assert res.status_code == 200
    body = res.json()
    assert body["enabled"] is False
    assert body["conversationUrl"] is None


def test_avatar_session_returns_url_when_enabled():
    from config.settings import Settings
    settings = Settings(tavus_api_key="k", tavus_replica_id="r")
    app = create_app(
        llm=_FakeLLM({}), store=InMemoryStore(), settings=settings, tavus_client=_FakeTavus()
    )
    client = TestClient(app)
    res = client.post("/api/avatar/session")
    assert res.status_code == 200
    body = res.json()
    assert body["enabled"] is True
    assert body["conversationUrl"] == "https://tavus.daily.co/room-abc"


class _FakeTavusEnd(_FakeTavus):
    def __init__(self):
        super().__init__()
        self.ended = []

    def end_conversation(self, conversation_id):
        self.ended.append(conversation_id)


def test_avatar_end_calls_tavus_when_enabled():
    from config.settings import Settings
    settings = Settings(tavus_api_key="k", tavus_replica_id="r")
    fake = _FakeTavusEnd()
    app = create_app(llm=_FakeLLM({}), store=InMemoryStore(), settings=settings, tavus_client=fake)
    client = TestClient(app)
    res = client.post("/api/avatar/end", json={"conversationId": "conv-123"})
    assert res.status_code == 200
    assert res.json()["ended"] is True
    assert fake.ended == ["conv-123"]


def test_avatar_end_noop_when_disabled():
    client = _client({})
    res = client.post("/api/avatar/end", json={"conversationId": "conv-123"})
    assert res.status_code == 200
    assert res.json()["ended"] is False
