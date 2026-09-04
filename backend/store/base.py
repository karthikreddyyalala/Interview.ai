from typing import Protocol

from models.contracts import MemoryProfile, SessionRecord, SessionSummary


class VersionConflict(Exception):
    """Raised by a compare-and-swap write when the stored version has moved
    since it was read — another writer won the race.

    This is the conflict signal for optimistic locking: the store layer owns
    detecting the race (via a DynamoDB ConditionExpression in production, or
    an equivalent version check in the in-memory test/local-dev store) and
    raises this instead of silently overwriting. Callers that must survive a
    concurrent writer for the same key catch this and decide how to react —
    typically: re-read, recompute, retry, bounded.
    """


class MemoryStore(Protocol):
    """Persistence seam for cross-session memory.

    The whole product differentiator (memory that reshapes future sessions)
    depends on this surviving past a single browser, so it lives behind a
    swappable interface: InMemoryStore for tests/local, DynamoMemoryStore in
    production.
    """

    def get_memory(self, candidate_id: str) -> MemoryProfile | None: ...

    def get_memory_with_version(self, candidate_id: str) -> tuple[MemoryProfile | None, int]:
        """Like get_memory, but also returns the item's current version (0 if
        the item does not exist yet). Round-trip the version through
        put_memory_cas to perform a safe optimistic-locked read-modify-write.
        """
        ...

    def put_memory(self, profile: MemoryProfile) -> None:
        """Unconditional overwrite — fine for seeding/local-dev/one-shot
        callers, but NOT safe against a concurrent writer for the same
        candidate. The finalize route (which races real user traffic across
        two tabs / retried requests) must use put_memory_cas instead."""
        ...

    def put_memory_cas(self, profile: MemoryProfile, expected_version: int) -> None:
        """Compare-and-swap write: succeeds only if the stored version still
        equals expected_version (as returned by get_memory_with_version).
        Raises VersionConflict, performing no write, if it has moved."""
        ...

    # Per-session records power the dashboard's reviewable session history.
    # put_session's own index-upsert race (see DynamoMemoryStore) is handled
    # internally by each implementation — it never touches an LLM, so it is
    # cheap to retry entirely inside the store rather than pushing that onto
    # every caller.
    def put_session(self, record: SessionRecord) -> None: ...

    def list_sessions(self, candidate_id: str) -> list[SessionSummary]: ...

    def get_session(self, candidate_id: str, session_id: str) -> SessionRecord | None: ...
