from models.contracts import MemoryProfile, SessionRecord, SessionSummary
from store.base import VersionConflict


class InMemoryStore:
    """Process-local store. Used by tests and local dev without DynamoDB."""

    def __init__(self) -> None:
        self._mem: dict[str, MemoryProfile] = {}
        self._mem_version: dict[str, int] = {}
        # candidate_id -> {session_id -> SessionRecord}
        self._sessions: dict[str, dict[str, SessionRecord]] = {}

    def get_memory(self, candidate_id: str) -> MemoryProfile | None:
        return self._mem.get(candidate_id)

    def get_memory_with_version(self, candidate_id: str) -> tuple[MemoryProfile | None, int]:
        return self._mem.get(candidate_id), self._mem_version.get(candidate_id, 0)

    def put_memory(self, profile: MemoryProfile) -> None:
        # Unconditional path: still bumps the version monotonically so a
        # later put_memory_cas (which reads this value first) behaves
        # sanely, but performs no race check itself.
        cid = profile.candidate_id
        self._mem[cid] = profile
        self._mem_version[cid] = self._mem_version.get(cid, 0) + 1

    def put_memory_cas(self, profile: MemoryProfile, expected_version: int) -> None:
        cid = profile.candidate_id
        current_version = self._mem_version.get(cid, 0)
        if current_version != expected_version:
            raise VersionConflict(
                f"memory for {cid} is at version {current_version}, expected {expected_version}"
            )
        self._mem[cid] = profile
        self._mem_version[cid] = current_version + 1

    def put_session(self, record: SessionRecord) -> None:
        self._sessions.setdefault(record.candidate_id, {})[record.session_id] = record

    def list_sessions(self, candidate_id: str) -> list[SessionSummary]:
        records = self._sessions.get(candidate_id, {}).values()
        summaries = [r.summary() for r in records]
        # Newest first — by date, then session_id as a stable tiebreaker.
        summaries.sort(key=lambda s: (s.date, s.session_id), reverse=True)
        return summaries

    def get_session(self, candidate_id: str, session_id: str) -> SessionRecord | None:
        return self._sessions.get(candidate_id, {}).get(session_id)
