import json

import pytest

from models.contracts import (
    AnswerEvaluation,
    MemoryProfile,
    PlannedQuestion,
    RecurringWeakness,
    SessionRecord,
    TrendPoint,
)
from store.base import VersionConflict
from store.in_memory import InMemoryStore


def _record(candidate_id: str, session_id: str, *, survived: int = 1, total: int = 2,
            mode: str = "full", level: str = "mid", date: str = "2026-07-10") -> SessionRecord:
    evals = [
        AnswerEvaluation(
            questionId=f"q{i}",
            transcript=f"Q: question {i}\nA: answer {i}",
            rubricScores={"structure": 3.0},
            weaknessTags=[] if i < survived else ["vague-impact"],
            followUpCount=0,
            wouldSurviveRealInterview=i < survived,
            survivalReasoning="reasoning",
        )
        for i in range(total)
    ]
    questions = [
        PlannedQuestion(id=f"q{i}", type="behavioral", prompt=f"question {i}",
                        targetDifficulty=3, weightedFromWeakness=False)
        for i in range(total)
    ]
    return SessionRecord(
        sessionId=session_id, candidateId=candidate_id, date=date,
        mode=mode, level=level, questions=questions, evaluations=evals,
    )


def _profile(candidate_id: str) -> MemoryProfile:
    return MemoryProfile(
        candidateId=candidate_id,
        recurringWeaknesses=[RecurringWeakness(tag="no-edge-cases", frequency=2, lastSeen="2026-06-22")],
        improvementTrend=[TrendPoint(sessionDate="2026-06-22", avgScore=3.25)],
        strongAreas=["ownership"],
    )


def test_in_memory_round_trip():
    store = InMemoryStore()
    assert store.get_memory("cand-1") is None

    store.put_memory(_profile("cand-1"))
    loaded = store.get_memory("cand-1")
    assert loaded is not None
    assert loaded.candidate_id == "cand-1"
    assert loaded.recurring_weaknesses[0].frequency == 2
    assert loaded.improvement_trend[0].avg_score == 3.25


def test_in_memory_isolates_candidates():
    store = InMemoryStore()
    store.put_memory(_profile("cand-1"))
    assert store.get_memory("cand-2") is None


def test_in_memory_overwrites_on_put():
    store = InMemoryStore()
    store.put_memory(_profile("cand-1"))
    updated = _profile("cand-1")
    updated.recurring_weaknesses[0].frequency = 9
    store.put_memory(updated)
    assert store.get_memory("cand-1").recurring_weaknesses[0].frequency == 9


def test_put_and_get_session_round_trip():
    store = InMemoryStore()
    assert store.get_session("cand-1", "s1") is None

    store.put_session(_record("cand-1", "s1", survived=2, total=3))
    loaded = store.get_session("cand-1", "s1")
    assert loaded is not None
    assert loaded.session_id == "s1"
    assert len(loaded.questions) == 3
    assert len(loaded.evaluations) == 3
    assert loaded.evaluations[0].transcript.startswith("Q: question 0")


def test_list_sessions_returns_summaries_newest_first():
    store = InMemoryStore()
    store.put_session(_record("cand-1", "s1", survived=1, total=2, date="2026-07-01"))
    store.put_session(_record("cand-1", "s2", survived=3, total=3, date="2026-07-05"))

    summaries = store.list_sessions("cand-1")
    assert [s.session_id for s in summaries] == ["s2", "s1"]  # newest first
    assert summaries[0].survived == 3 and summaries[0].total == 3
    assert summaries[1].mode == "full" and summaries[1].level == "mid"


def test_list_sessions_isolates_candidates():
    store = InMemoryStore()
    store.put_session(_record("cand-1", "s1"))
    assert store.list_sessions("cand-2") == []
    assert store.get_session("cand-2", "s1") is None


def test_sessions_and_memory_do_not_collide():
    store = InMemoryStore()
    store.put_memory(_profile("cand-1"))
    store.put_session(_record("cand-1", "s1"))
    assert store.get_memory("cand-1") is not None
    assert len(store.list_sessions("cand-1")) == 1


# --- optimistic locking: InMemoryStore memory-profile CAS ------------------
# This is the interface both stores must honor so routes/session.py's
# finalize handler can do a safe read-modify-write around the (expensive,
# LLM-backed) Memory Agent call without silently clobbering a concurrent
# finalize for the same candidate.

def test_in_memory_get_memory_with_version_reports_zero_for_missing():
    store = InMemoryStore()
    profile, version = store.get_memory_with_version("cand-1")
    assert profile is None
    assert version == 0


def test_in_memory_put_memory_cas_succeeds_with_correct_version():
    store = InMemoryStore()
    store.put_memory(_profile("cand-1"))
    _profile_loaded, version = store.get_memory_with_version("cand-1")

    updated = _profile("cand-1")
    updated.recurring_weaknesses[0].frequency = 9
    store.put_memory_cas(updated, expected_version=version)

    loaded, new_version = store.get_memory_with_version("cand-1")
    assert loaded.recurring_weaknesses[0].frequency == 9
    assert new_version == version + 1


def test_in_memory_put_memory_cas_rejects_stale_version():
    """Reproduces the race: two finalize calls read the same starting state,
    then both try to write. The second writer's expected_version no longer
    matches once the first writer's update has landed, so it must be
    rejected rather than silently overwriting the first writer's update."""
    store = InMemoryStore()
    store.put_memory(_profile("cand-1"))  # version -> 1
    _profile_a, version_a = store.get_memory_with_version("cand-1")  # both readers see version 1
    _profile_b, version_b = store.get_memory_with_version("cand-1")
    assert version_a == version_b

    # Writer A finishes its (slow, LLM-backed) compute first and writes.
    writer_a_update = _profile("cand-1")
    writer_a_update.recurring_weaknesses[0].frequency = 5
    store.put_memory_cas(writer_a_update, expected_version=version_a)

    # Writer B computed against the now-stale starting state; its write must
    # be rejected, not silently clobber writer A's update.
    writer_b_update = _profile("cand-1")
    writer_b_update.recurring_weaknesses[0].frequency = 999
    with pytest.raises(VersionConflict):
        store.put_memory_cas(writer_b_update, expected_version=version_b)

    # Writer A's contribution survived.
    assert store.get_memory("cand-1").recurring_weaknesses[0].frequency == 5


def test_in_memory_put_memory_cas_first_write_uses_version_zero():
    store = InMemoryStore()
    store.put_memory_cas(_profile("cand-1"), expected_version=0)
    loaded, version = store.get_memory_with_version("cand-1")
    assert loaded is not None
    assert version == 1


def test_in_memory_put_memory_cas_exhausted_retries_raise():
    """A caller retrying a bounded number of times must see a raised
    VersionConflict on the final attempt, not a silent no-op."""
    store = InMemoryStore()
    store.put_memory(_profile("cand-1"))
    _profile_read, stale_version = store.get_memory_with_version("cand-1")

    max_attempts = 3
    conflict = None
    for attempt in range(max_attempts):
        try:
            # Every attempt races against a concurrent writer that always
            # wins first, so every attempt's expected_version is stale.
            store.put_memory(_profile("cand-1"))
            store.put_memory_cas(_profile("cand-1"), expected_version=stale_version)
            break
        except VersionConflict as exc:
            conflict = exc
            continue
    assert conflict is not None


# --- DynamoMemoryStore with an in-memory table double ---------------------

class _FakeTable:
    """Minimal boto3 Table stand-in: get_item / put_item on a dict keyed by PK.

    Also evaluates the two ConditionExpression forms the store actually uses,
    raising the real botocore.exceptions.ClientError shape (Error.Code ==
    "ConditionalCheckFailedException") on failure, so tests exercise the same
    exception-handling path the production DynamoDB client would hit.
    """

    def __init__(self):
        self.items: dict[str, dict] = {}

    def get_item(self, Key):
        item = self.items.get(Key["candidateId"])
        return {"Item": item} if item is not None else {}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None):
        key = Item["candidateId"]
        if ConditionExpression == "attribute_not_exists(version)":
            if key in self.items and "version" in self.items[key]:
                raise self._conditional_check_failed()
        elif ConditionExpression == "version = :expected":
            current = self.items.get(key)
            current_version = current.get("version") if current else None
            expected = (ExpressionAttributeValues or {}).get(":expected")
            if current_version != expected:
                raise self._conditional_check_failed()
        self.items[key] = Item

    @staticmethod
    def _conditional_check_failed():
        from botocore.exceptions import ClientError

        return ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "The conditional request failed"}},
            "PutItem",
        )


class _FakeResource:
    def __init__(self, table):
        self._table = table

    def Table(self, _name):
        return self._table


def _dynamo_store():
    from store.dynamo import DynamoMemoryStore
    return DynamoMemoryStore(table_name="t", region="us-west-2", resource=_FakeResource(_FakeTable()))


def test_dynamo_session_round_trip_and_index():
    store = _dynamo_store()
    assert store.get_session("cand-1", "s1") is None
    assert store.list_sessions("cand-1") == []

    store.put_session(_record("cand-1", "s1", survived=1, total=2, date="2026-07-01"))
    store.put_session(_record("cand-1", "s2", survived=3, total=3, date="2026-07-05"))

    listed = store.list_sessions("cand-1")
    assert [s.session_id for s in listed] == ["s2", "s1"]
    assert listed[0].survived == 3 and listed[0].total == 3

    full = store.get_session("cand-1", "s2")
    assert full is not None and len(full.questions) == 3


def test_dynamo_sessions_do_not_collide_with_memory():
    store = _dynamo_store()
    store.put_memory(_profile("cand-1"))
    store.put_session(_record("cand-1", "s1"))
    assert store.get_memory("cand-1") is not None
    assert store.get_memory("cand-1").candidate_id == "cand-1"
    assert len(store.list_sessions("cand-1")) == 1


def test_dynamo_reput_session_updates_index_without_duplicates():
    store = _dynamo_store()
    store.put_session(_record("cand-1", "s1", survived=1, total=2))
    store.put_session(_record("cand-1", "s1", survived=2, total=2))  # same id, re-put
    listed = store.list_sessions("cand-1")
    assert len(listed) == 1
    assert listed[0].survived == 2


# --- optimistic locking: DynamoMemoryStore memory-profile CAS --------------

def test_dynamo_get_memory_with_version_reports_zero_for_missing():
    store = _dynamo_store()
    profile, version = store.get_memory_with_version("cand-1")
    assert profile is None
    assert version == 0


def test_dynamo_put_memory_cas_succeeds_with_correct_version():
    store = _dynamo_store()
    store.put_memory(_profile("cand-1"))
    _loaded, version = store.get_memory_with_version("cand-1")

    updated = _profile("cand-1")
    updated.recurring_weaknesses[0].frequency = 9
    store.put_memory_cas(updated, expected_version=version)

    loaded, new_version = store.get_memory_with_version("cand-1")
    assert loaded.recurring_weaknesses[0].frequency == 9
    assert new_version == version + 1


def test_dynamo_put_memory_cas_rejects_stale_version():
    """Deterministic race reproduction: read state A (get the version), then
    directly mutate the underlying fake table to simulate a second writer
    completing in between, then attempt to write A's derived update using
    the now-stale version. Must be rejected, not silently clobber the
    intervening write."""
    store = _dynamo_store()
    store.put_memory(_profile("cand-1"))
    _read_a, version_a = store.get_memory_with_version("cand-1")

    # A concurrent writer completes here, going straight through the table
    # (as if a second process/request finished first).
    concurrent_update = _profile("cand-1")
    concurrent_update.recurring_weaknesses[0].frequency = 42
    store.put_memory_cas(concurrent_update, expected_version=version_a)

    # Our own write, computed from the now-stale `version_a`, must be
    # rejected rather than overwriting the concurrent writer's update.
    stale_update = _profile("cand-1")
    stale_update.recurring_weaknesses[0].frequency = 999
    with pytest.raises(VersionConflict):
        store.put_memory_cas(stale_update, expected_version=version_a)

    assert store.get_memory("cand-1").recurring_weaknesses[0].frequency == 42


def test_dynamo_put_memory_cas_first_write_uses_version_zero():
    store = _dynamo_store()
    store.put_memory_cas(_profile("cand-1"), expected_version=0)
    loaded, version = store.get_memory_with_version("cand-1")
    assert loaded is not None
    assert version == 1


def test_dynamo_put_memory_cas_upgrades_legacy_item_without_version_attribute():
    """A pre-fix item written before `version` existed reads back as version
    0 (via .get("version", 0)) even though the item is present. Because
    nothing has changed since that read, a put_memory_cas(expected_version=0)
    against it must succeed exactly once, stamping a version onto it — this
    is why the "doesn't exist yet" condition keys off
    attribute_not_exists(version), not attribute_not_exists(candidateId)."""
    store = _dynamo_store()
    # Simulate a legacy item written by the pre-fix code: no version attribute.
    store._table.put_item(
        Item={"candidateId": "cand-legacy", "profileJson": _profile("cand-legacy").model_dump_json(by_alias=True)}
    )
    loaded, version = store.get_memory_with_version("cand-legacy")
    assert loaded is not None
    assert version == 0

    updated = _profile("cand-legacy")
    updated.recurring_weaknesses[0].frequency = 7
    store.put_memory_cas(updated, expected_version=0)  # upgrades it in place
    assert store.get_memory("cand-legacy").recurring_weaknesses[0].frequency == 7

    # A second writer that also read the legacy (version-less) state and
    # computed its own update must now be rejected — the item has since
    # gained a version, so its stale expected_version=0 no longer holds.
    stale_update = _profile("cand-legacy")
    stale_update.recurring_weaknesses[0].frequency = 999
    with pytest.raises(VersionConflict):
        store.put_memory_cas(stale_update, expected_version=0)


# --- optimistic locking: DynamoMemoryStore session-index CAS ---------------
# put_session's index upsert is a read-modify-write of a single JSON-blob
# item (see DynamoMemoryStore docstring): two concurrent put_session calls
# for the same candidate can otherwise race and lose one session's entry
# from the summary list, even though each full SessionRecord (stored under
# its own key) survives untouched. InMemoryStore has no equivalent bug: it
# keeps each session in its own dict entry, so concurrent put_session calls
# for different session_ids don't share mutable state.

def test_dynamo_index_cas_rejects_stale_version():
    store = _dynamo_store()
    store.put_session(_record("cand-1", "s1"))
    summaries, version = store._read_index_versioned("cand-1")

    # A concurrent writer appends its own session and completes first.
    store._write_index_cas("cand-1", summaries + [_record("cand-1", "s-other").summary()], expected_version=version)

    # Our write, still holding the pre-race version, must be rejected.
    with pytest.raises(VersionConflict):
        store._write_index_cas("cand-1", summaries + [_record("cand-1", "s2").summary()], expected_version=version)

    # Nothing lost: the concurrent writer's entry is intact.
    assert {s.session_id for s in store.list_sessions("cand-1")} == {"s1", "s-other"}


def test_dynamo_put_session_recovers_from_one_conflict_and_loses_nothing():
    """Proves the retry loop inside put_session's index upsert actually
    recovers from a conflict — not just detects it — landing on a final
    state that contains both the concurrent writer's session and our own."""
    store = _dynamo_store()
    store.put_session(_record("cand-1", "s1"))

    real_read = store._read_index_versioned
    calls = {"n": 0}

    def racing_read(candidate_id):
        calls["n"] += 1
        summaries, version = real_read(candidate_id)
        if calls["n"] == 1:
            # A concurrent finalize call wins the race right after our read.
            store._write_index_cas(
                candidate_id, summaries + [_record(candidate_id, "s-other").summary()], expected_version=version
            )
        return summaries, version

    store._read_index_versioned = racing_read

    store.put_session(_record("cand-1", "s2"))  # must retry once and still succeed
    assert calls["n"] == 2  # exactly one retry — not a naive infinite loop

    store._read_index_versioned = real_read  # unpatch before reading back the result
    ids = {s.session_id for s in store.list_sessions("cand-1")}
    assert ids == {"s1", "s-other", "s2"}


def test_dynamo_put_session_index_conflict_exhausted_raises():
    """If a competing writer keeps winning past the retry bound, put_session
    must fail loudly (raise) rather than silently drop the index update."""
    store = _dynamo_store()
    store.put_session(_record("cand-1", "s1"))

    real_read = store._read_index_versioned

    def always_racing_read(candidate_id):
        summaries, version = real_read(candidate_id)
        # Someone else always wins the race, forever.
        store._write_index_cas(
            candidate_id, summaries + [_record(candidate_id, f"s-race-{version}").summary()], expected_version=version
        )
        return summaries, version

    store._read_index_versioned = always_racing_read

    with pytest.raises(VersionConflict):
        store.put_session(_record("cand-1", "s2"))
