import json
from typing import Any

from models.contracts import MemoryProfile, SessionRecord, SessionSummary
from store.base import VersionConflict

# put_session's index upsert never calls an LLM, so it's cheap to retry
# end-to-end (re-read, re-merge the summary list, re-write) entirely inside
# the store. Contention is same-candidate/same-few-seconds at worst, not
# adversarial, so a small bound is proportionate — it exists to fail loudly
# on a genuine bug, not to survive hammering.
_MAX_INDEX_WRITE_ATTEMPTS = 5


class DynamoMemoryStore:
    """DynamoDB-backed memory store.

    Everything is stored as a single JSON string attribute per item. This avoids
    DynamoDB's float/Decimal friction entirely (rubric scores and avgScore are
    floats) and keeps reads/writes a clean Pydantic round-trip.

    The table has only a partition key (`candidateId` S) and the runtime role is
    scoped to GetItem/PutItem, so session history is layered on with derived
    partition keys instead of a new table or a sort key:
      - memory profile:  candidateId
      - session index:   "{candidateId}#index"     -> JSON list of SessionSummary
      - full session:    "{candidateId}#session#{sessionId}" -> JSON SessionRecord
    The index item is summaries only (small), so it never approaches the 400KB
    item limit; full transcripts live in their own per-session items.

    Both the memory-profile item and the session-index item carry a `version`
    (N) attribute used for optimistic locking: put_memory_cas and the internal
    index writer use ConditionExpression to fail the write (raising
    VersionConflict) if the version has moved since it was read, instead of
    silently overwriting a concurrent writer's update.
    """

    def __init__(self, table_name: str, region: str, resource: Any = None) -> None:
        if resource is None:
            import boto3

            resource = boto3.resource("dynamodb", region_name=region)
        self._table = resource.Table(table_name)

    @staticmethod
    def _index_key(candidate_id: str) -> str:
        return f"{candidate_id}#index"

    @staticmethod
    def _session_key(candidate_id: str, session_id: str) -> str:
        return f"{candidate_id}#session#{session_id}"

    def get_memory(self, candidate_id: str) -> MemoryProfile | None:
        profile, _version = self.get_memory_with_version(candidate_id)
        return profile

    def get_memory_with_version(self, candidate_id: str) -> tuple[MemoryProfile | None, int]:
        resp = self._table.get_item(Key={"candidateId": candidate_id})
        item = resp.get("Item")
        if not item:
            return None, 0
        return MemoryProfile.model_validate_json(item["profileJson"]), int(item.get("version", 0))

    def put_memory(self, profile: MemoryProfile) -> None:
        # Unconditional path (seeding/local-dev callers). Still bumps the
        # version so a later put_memory_cas sees a sane starting point, but
        # performs no race check itself.
        _existing, version = self.get_memory_with_version(profile.candidate_id)
        self._table.put_item(
            Item={
                "candidateId": profile.candidate_id,
                "profileJson": profile.model_dump_json(by_alias=True),
                "version": version + 1,
            }
        )

    def put_memory_cas(self, profile: MemoryProfile, expected_version: int) -> None:
        from botocore.exceptions import ClientError

        item = {
            "candidateId": profile.candidate_id,
            "profileJson": profile.model_dump_json(by_alias=True),
            "version": expected_version + 1,
        }
        try:
            if expected_version == 0:
                self._table.put_item(Item=item, ConditionExpression="attribute_not_exists(version)")
            else:
                self._table.put_item(
                    Item=item,
                    ConditionExpression="version = :expected",
                    ExpressionAttributeValues={":expected": expected_version},
                )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise VersionConflict(
                    f"memory for {profile.candidate_id} changed since it was read "
                    f"(expected version {expected_version})"
                ) from exc
            raise

    def put_session(self, record: SessionRecord) -> None:
        # The full record lives at its own key — no read-modify-write, no race.
        self._table.put_item(
            Item={
                "candidateId": self._session_key(record.candidate_id, record.session_id),
                "recordJson": record.model_dump_json(by_alias=True),
            }
        )
        self._upsert_index(record)

    def _upsert_index(self, record: SessionRecord) -> None:
        candidate_id = record.candidate_id
        for attempt in range(_MAX_INDEX_WRITE_ATTEMPTS):
            summaries, version = self._read_index_versioned(candidate_id)
            summaries = [s for s in summaries if s.session_id != record.session_id]
            summaries.append(record.summary())
            try:
                self._write_index_cas(candidate_id, summaries, expected_version=version)
                return
            except VersionConflict:
                if attempt == _MAX_INDEX_WRITE_ATTEMPTS - 1:
                    raise
                continue

    def _read_index_versioned(self, candidate_id: str) -> tuple[list[SessionSummary], int]:
        resp = self._table.get_item(Key={"candidateId": self._index_key(candidate_id)})
        item = resp.get("Item")
        if not item:
            return [], 0
        summaries = [SessionSummary.model_validate(s) for s in json.loads(item["sessionsJson"])]
        return summaries, int(item.get("version", 0))

    def _write_index_cas(self, candidate_id: str, summaries: list[SessionSummary], expected_version: int) -> None:
        from botocore.exceptions import ClientError

        item = {
            "candidateId": self._index_key(candidate_id),
            "sessionsJson": json.dumps([s.model_dump(by_alias=True) for s in summaries]),
            "version": expected_version + 1,
        }
        try:
            if expected_version == 0:
                self._table.put_item(Item=item, ConditionExpression="attribute_not_exists(version)")
            else:
                self._table.put_item(
                    Item=item,
                    ConditionExpression="version = :expected",
                    ExpressionAttributeValues={":expected": expected_version},
                )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise VersionConflict(
                    f"session index for {candidate_id} changed since it was read "
                    f"(expected version {expected_version})"
                ) from exc
            raise

    def _read_index(self, candidate_id: str) -> list[SessionSummary]:
        summaries, _version = self._read_index_versioned(candidate_id)
        return summaries

    def list_sessions(self, candidate_id: str) -> list[SessionSummary]:
        summaries = self._read_index(candidate_id)
        summaries.sort(key=lambda s: (s.date, s.session_id), reverse=True)
        return summaries

    def get_session(self, candidate_id: str, session_id: str) -> SessionRecord | None:
        resp = self._table.get_item(
            Key={"candidateId": self._session_key(candidate_id, session_id)}
        )
        item = resp.get("Item")
        if not item:
            return None
        return SessionRecord.model_validate_json(item["recordJson"])
