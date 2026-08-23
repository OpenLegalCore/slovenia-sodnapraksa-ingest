"""Direct PostgreSQL/Qdrant functions and the single linear run."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from . import IngestError
from .config import Settings
from .document import Chunk, Document, build_chunks, normalize_document
from .source import _internal_id, _timestamp, discover_documents, embed_texts, fetch_document, request_json_with_retry

QDRANT_FILTER_BATCH_SIZE = 256
QDRANT_SCROLL_LIMIT = 256
# Defensive ceiling for filtered reconciliation scrolling: 1,048,576 points per identity group.
QDRANT_SCROLL_MAX_PAGES = 4096


@dataclass(frozen=True)
class ReconciliationPlan:
    postgres_updates: tuple[Document, ...]
    embedding_chunks: tuple[Chunk, ...]
    payload_updates: tuple[Chunk, ...]
    stale_point_ids: frozenset[str]
    embedding_input_bytes: int


def pg_connect(settings: Settings, *, read_only: bool = False):
    connection = psycopg.connect(
        settings.database_url,
        row_factory=dict_row,
        options="-c default_transaction_read_only=on" if read_only else "",
    )
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database() AS name")
        row = cursor.fetchone()
    if not row or row["name"] != settings.expected_database:
        connection.close()
        raise IngestError("connected PostgreSQL database identity does not match")
    return connection


def _table(settings: Settings):
    return sql.Identifier(settings.expected_schema, "sodnapraksa_documents")


def pg_preflight(settings: Settings) -> dict[str, object]:
    required = set(
        "evidencna_stevilka unid internal_id database_name datum_zadnje_spremembe content_hash "  # noqa: SIM905
        "status deleted error raw_api normalized updated_at".split()
    )
    with pg_connect(settings, read_only=True) as connection, connection.cursor() as cursor:
        cursor.execute("SHOW transaction_read_only")
        if cursor.fetchone()["transaction_read_only"] != "on":
            raise IngestError("PostgreSQL preflight session is not read-only")
        cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name='sodnapraksa_documents'",
            (settings.expected_schema,),
        )
        columns = {row["column_name"] for row in cursor.fetchall()}
        if missing := required - columns:
            raise IngestError("PostgreSQL table is missing required columns: " + ",".join(sorted(missing)))
        cursor.execute(
            "SELECT a.attname FROM pg_constraint c JOIN pg_class r ON r.oid=c.conrelid "
            "JOIN pg_namespace n ON n.oid=r.relnamespace "
            "JOIN pg_attribute a ON a.attrelid=r.oid AND a.attnum=ANY(c.conkey) "
            "WHERE c.contype='p' AND n.nspname=%s AND r.relname='sodnapraksa_documents'",
            (settings.expected_schema,),
        )
        if [row["attname"] for row in cursor.fetchall()] != ["evidencna_stevilka"]:
            raise IngestError("PostgreSQL primary key contract does not match")
        connection.rollback()
    return {"database": settings.expected_database, "schema": settings.expected_schema}


def pg_load_listing_states(settings: Settings, identities: list[str]) -> dict[str, dict[str, object]]:
    if not identities:
        return {}
    query = sql.SQL(
        "SELECT evidencna_stevilka, unid, internal_id, database_name, datum_zadnje_spremembe, "
        "COALESCE(content_hash,'') content_hash, COALESCE(normalized->>'metadata_hash','') metadata_hash, "
        "status, deleted, error, raw_api IS NOT NULL has_raw_api, normalized IS NOT NULL has_normalized "
        "FROM {} WHERE evidencna_stevilka=ANY(%s)"
    ).format(_table(settings))
    with pg_connect(settings, read_only=True) as connection, connection.cursor() as cursor:
        cursor.execute(query, (identities,))
        rows = cursor.fetchall()
        connection.rollback()
    return {row["evidencna_stevilka"]: dict(row) for row in rows}


def pg_load_stored_bodies(settings: Settings, identities: list[str]) -> dict[str, dict[str, object]]:
    if not identities:
        return {}
    query = sql.SQL(
        "SELECT evidencna_stevilka, unid, internal_id, database_name, datum_zadnje_spremembe, "
        "COALESCE(content_hash,'') content_hash, COALESCE(normalized->>'metadata_hash','') metadata_hash, "
        "status, deleted, error, raw_api, raw_api IS NOT NULL has_raw_api, normalized IS NOT NULL has_normalized "
        "FROM {} WHERE evidencna_stevilka=ANY(%s)"
    ).format(_table(settings))
    with pg_connect(settings, read_only=True) as connection, connection.cursor() as cursor:
        cursor.execute(query, (identities,))
        rows = cursor.fetchall()
        connection.rollback()
    return {row["evidencna_stevilka"]: dict(row) for row in rows}


def pg_save_document(settings: Settings, document: Document) -> bool:
    if not settings.allow_writes:
        raise IngestError("PostgreSQL writes are not authorized")
    n = document.normalized
    query = sql.SQL(
        """
        INSERT INTO {} (evidencna_stevilka,unid,internal_id,database_name,title,url,feed_url,
          sodisce,oddelek,ecli,datum_odlocbe,datum_nastanka,datum_zadnje_spremembe,content_hash,
          status,deleted,error,raw_api,normalized,discovered_at,scraped_at,checked_at,stored_at,updated_at)
        VALUES (%(id)s,%(unid)s,%(internal)s,%(database)s,%(title)s,%(url)s,%(feed)s,%(court)s,
          %(department)s,%(ecli)s,NULLIF(%(decision_date)s,'')::timestamptz,
          NULLIF(%(created)s,'')::timestamptz,NULLIF(%(modified)s,'')::timestamptz,%(content)s,
          'STORED',FALSE,NULL,%(raw)s,%(normalized)s,now(),now(),now(),now(),now())
        ON CONFLICT (evidencna_stevilka) DO UPDATE SET unid=EXCLUDED.unid,
          internal_id=EXCLUDED.internal_id,database_name=EXCLUDED.database_name,title=EXCLUDED.title,
          url=EXCLUDED.url,feed_url=EXCLUDED.feed_url,sodisce=EXCLUDED.sodisce,
          oddelek=EXCLUDED.oddelek,ecli=EXCLUDED.ecli,datum_odlocbe=EXCLUDED.datum_odlocbe,
          datum_nastanka=EXCLUDED.datum_nastanka,datum_zadnje_spremembe=EXCLUDED.datum_zadnje_spremembe,
          content_hash=EXCLUDED.content_hash,status='STORED',deleted=FALSE,error=NULL,
          raw_api=EXCLUDED.raw_api,normalized=EXCLUDED.normalized,checked_at=now(),updated_at=now()
        RETURNING evidencna_stevilka
        """
    ).format(_table(settings))
    values = {
        "id": document.evidencna,
        "unid": document.unid,
        "internal": document.internal_id,
        "database": document.database,
        "title": n["title"],
        "url": n["url"],
        "feed": n["feed_url"],
        "court": n["sodisce"],
        "department": n["oddelek"],
        "ecli": n["ecli"],
        "decision_date": n["datum_odlocbe"],
        "created": n["datum_nastanka"],
        "modified": n["datum_zadnje_spremembe"],
        "content": document.content_hash,
        "raw": Jsonb(document.raw),
        "normalized": Jsonb(document.normalized),
    }
    with pg_connect(settings) as connection, connection.cursor() as cursor:
        cursor.execute(query, values)
        changed = cursor.fetchone() is not None
        connection.commit()
    return changed


def _qdrant_request(
    settings: Settings, method: str, path: str, body: dict[str, object] | None = None, *, write: bool = False
) -> dict[str, object]:
    if write and not settings.allow_writes:
        raise IngestError("Qdrant writes are not authorized")
    headers = {"api-key": settings.qdrant_api_key} if settings.qdrant_api_key else None
    response = request_json_with_retry(
        method,
        f"{settings.qdrant_url}/collections/{settings.qdrant_collection}/{path.lstrip('/')}",
        payload=body,
        headers=headers,
        label="Qdrant",
    )
    if response.get("status") != "ok":
        raise IngestError("Qdrant returned a non-success status")
    return response


def qdrant_preflight(settings: Settings) -> dict[str, object]:
    if settings.embedding_model != "text-embedding-3-large" or settings.embedding_dimensions != 3072:
        raise IngestError("embedding model contract does not match the established collection")
    result = _qdrant_request(settings, "GET", "").get("result")
    if not isinstance(result, dict) or result.get("status") != "green" or result.get("optimizer_status") != "ok":
        raise IngestError("Qdrant collection is not healthy")
    vectors = result.get("config", {}).get("params", {}).get("vectors")
    if not isinstance(vectors, dict) or vectors.get("size") != settings.embedding_dimensions or vectors.get("distance") != "Cosine":
        raise IngestError("Qdrant vector contract does not match")
    schema = result.get("payload_schema")
    expected = dict.fromkeys(("evidencna_stevilka", "chunk_id", "chunk_hash"), "keyword")
    expected["chunk_index"] = "integer"
    if not isinstance(schema, dict) or any(
        not isinstance(schema.get(key), dict) or schema[key].get("data_type") != value for key, value in expected.items()
    ):
        raise IngestError("Qdrant payload index contract does not match")
    return {"collection": settings.qdrant_collection, "dimensions": settings.embedding_dimensions, "distance": "Cosine"}


def qdrant_documents_points(settings: Settings, identities: list[str]) -> dict[str, dict[str, dict[str, object]]]:
    if len(set(identities)) != len(identities):
        raise IngestError("Qdrant lookup received duplicate document identities")
    documents: dict[str, dict[str, dict[str, object]]] = {identity: {} for identity in identities}
    for index in range(0, len(identities), QDRANT_FILTER_BATCH_SIZE):
        batch = identities[index : index + QDRANT_FILTER_BATCH_SIZE]
        offset: int | str | None = None
        offset_type: type[int] | type[str] | None = None
        seen_offsets: set[int | str] = set()
        for _page in range(QDRANT_SCROLL_MAX_PAGES):
            body: dict[str, object] = {
                "filter": {"must": [{"key": "evidencna_stevilka", "match": {"any": batch}}]},
                "limit": QDRANT_SCROLL_LIMIT,
                "with_payload": True,
                "with_vector": False,
            }
            if offset is not None:
                body["offset"] = offset
            result = _qdrant_request(settings, "POST", "points/scroll", body).get("result")
            if not isinstance(result, dict) or not isinstance(result.get("points"), list):
                raise IngestError("Qdrant scroll returned invalid data")
            rows = result["points"]
            for row in rows:
                payload = row.get("payload") if isinstance(row, dict) else None
                if not isinstance(payload, dict):
                    raise IngestError("Qdrant point payload is invalid")
                identity = str(payload.get("evidencna_stevilka") or "")
                point_id = str(row.get("id") or "")
                if identity not in batch or not point_id or point_id in documents[identity]:
                    raise IngestError("Qdrant returned an invalid or duplicate point")
                documents[identity][point_id] = dict(payload)
            next_offset = result.get("next_page_offset")
            if next_offset is None:
                break
            if not rows:
                raise IngestError("Qdrant scroll did not advance")
            if type(next_offset) is int:
                if not 0 <= next_offset <= 2**64 - 1:
                    raise IngestError("Qdrant scroll returned an invalid offset")
            elif isinstance(next_offset, str):
                try:
                    if str(UUID(next_offset)) != next_offset:
                        raise ValueError
                except ValueError as exc:
                    raise IngestError("Qdrant scroll returned an invalid offset") from exc
            else:
                raise IngestError("Qdrant scroll returned an invalid offset")
            if offset_type is not None and type(next_offset) is not offset_type:
                raise IngestError("Qdrant scroll offset type changed")
            if next_offset in seen_offsets:
                raise IngestError("Qdrant scroll offset repeated")
            offset_type = type(next_offset)
            seen_offsets.add(next_offset)
            offset = next_offset
        else:
            raise IngestError("Qdrant scroll page limit exceeded")
    return documents


def qdrant_upsert(settings: Settings, pairs: list[tuple[Chunk, list[float]]]) -> None:
    if pairs:
        _qdrant_request(
            settings,
            "PUT",
            "points?wait=true",
            {"points": [{"id": chunk.point_id, "vector": vector, "payload": chunk.payload} for chunk, vector in pairs]},
            write=True,
        )


def qdrant_overwrite_payload(settings: Settings, chunk: Chunk) -> None:
    _qdrant_request(settings, "PUT", "points/payload?wait=true", {"points": [chunk.point_id], "payload": chunk.payload}, write=True)


def qdrant_delete_points(settings: Settings, point_ids: set[str]) -> None:
    if point_ids:
        _qdrant_request(settings, "POST", "points/delete?wait=true", {"points": sorted(point_ids)}, write=True)


def acquire_lock(path: Path) -> TextIO:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def read_checkpoint(settings: Settings) -> datetime:
    if not settings.checkpoint_path.exists():
        return settings.initial_since
    try:
        value = json.loads(settings.checkpoint_path.read_text(encoding="utf-8"))
        if (
            value.get("version") != 1
            or value.get("source") != "dateModified-closed-open-v1"
            or value.get("overlap_days") != settings.overlap.days
        ):
            raise ValueError
        parsed = datetime.fromisoformat(value["last_successful_end"].replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(UTC).replace(microsecond=0)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise IngestError("checkpoint is invalid") from exc


def write_checkpoint(settings: Settings, end: datetime) -> None:
    settings.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "source": "dateModified-closed-open-v1",
        "overlap_days": settings.overlap.days,
        "last_successful_end": end.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=settings.checkpoint_path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, settings.checkpoint_path)
        directory = os.open(settings.checkpoint_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _listing_identity_version(discovered: dict[str, object]) -> tuple[str, str, str, datetime]:
    evidencna = str(discovered.get("registryNumber") or "").strip()
    unid = str(discovered.get("unid") or "").strip()
    internal = _internal_id(unid)
    if not evidencna or not unid or not internal:
        raise IngestError("source discovery identity is incomplete")
    return evidencna, unid, internal, _timestamp(discovered.get("dateModified"), "dateModified")


def _state_allows_reconstruction(discovered: dict[str, object], state: dict[str, object] | None) -> bool:
    if state is None:
        return False
    evidencna, unid, internal, listing_modified = _listing_identity_version(discovered)
    stored_modified = state.get("datum_zadnje_spremembe")
    return (
        state.get("evidencna_stevilka") == evidencna
        and state.get("unid") == unid
        and state.get("internal_id") == internal
        and isinstance(state.get("database_name"), str)
        and bool(state["database_name"])
        and isinstance(stored_modified, datetime)
        and stored_modified.tzinfo is not None
        and stored_modified.astimezone(UTC) >= listing_modified
        and isinstance(state.get("content_hash"), str)
        and bool(state["content_hash"])
        and isinstance(state.get("metadata_hash"), str)
        and bool(state["metadata_hash"])
        and state.get("status") == "STORED"
        and state.get("deleted") is False
        and state.get("error") is None
        and state.get("has_raw_api") is True
        and state.get("has_normalized") is True
    )


def _reconstruct_stored_document(settings: Settings, discovered: dict[str, object], state: dict[str, object] | None) -> Document | None:
    if not _state_allows_reconstruction(discovered, state):
        return None
    raw = state.get("raw_api")
    if not isinstance(raw, dict):
        return None
    try:
        document = normalize_document(raw, discovered, settings.source_base_url)
        stored_modified = state["datum_zadnje_spremembe"].astimezone(UTC)
        document_modified = _timestamp(document.normalized.get("datum_zadnje_spremembe"), "dateModified")
    except (AttributeError, IngestError, KeyError, TypeError):
        return None
    expected = (
        state.get("unid"),
        state.get("internal_id"),
        state.get("database_name"),
        state.get("content_hash"),
        state.get("metadata_hash"),
        stored_modified,
    )
    actual = (
        document.unid,
        document.internal_id,
        document.database,
        document.content_hash,
        document.metadata_hash,
        document_modified,
    )
    return document if actual == expected else None


def _compatible_vector(settings: Settings, payload: dict[str, object]) -> bool:
    model = str(payload.get("embedding_model") or "")
    dimensions = payload.get("embedding_dimensions")
    if not model and dimensions in (None, ""):
        return settings.embedding_model == "text-embedding-3-large" and settings.embedding_dimensions == 3072
    return model == settings.embedding_model and dimensions == settings.embedding_dimensions


def _plan_reconciliation(
    settings: Settings,
    documents: list[Document],
    existing: dict[str, dict[str, object]],
    current: dict[str, dict[str, dict[str, object]]],
    detail_ids: set[str],
) -> ReconciliationPlan:
    postgres_updates: list[Document] = []
    embedding_chunks: list[Chunk] = []
    payload_updates: list[Chunk] = []
    stale: set[str] = set()
    all_point_ids: set[str] = set()
    for document in documents:
        chunks = build_chunks(document, settings.embedding_model, settings.embedding_dimensions)
        desired = {chunk.point_id: chunk for chunk in chunks}
        if len(desired) != len(chunks) or all_point_ids.intersection(desired):
            raise IngestError("selected documents produce a point identity collision")
        all_point_ids.update(desired)
        actual_points = current[document.evidencna]
        stale.update(set(actual_points) - set(desired))
        for point_id, chunk in desired.items():
            payload = actual_points.get(point_id)
            if payload is None or not _compatible_vector(settings, payload):
                embedding_chunks.append(chunk)
            else:
                if payload != chunk.payload:
                    payload_updates.append(chunk)
        state = existing.get(document.evidencna)
        actual = state and (state["content_hash"], state["metadata_hash"], state["status"], state["deleted"])
        expected = (document.content_hash, document.metadata_hash, "STORED", False)
        pg_update = document.evidencna in detail_ids or actual != expected
        if pg_update:
            postgres_updates.append(document)
    embedding_bytes = sum(len(chunk.text.encode("utf-8")) for chunk in embedding_chunks)
    return ReconciliationPlan(
        tuple(postgres_updates),
        tuple(embedding_chunks),
        tuple(payload_updates),
        frozenset(stale),
        embedding_bytes,
    )


def apply_reconciliation(settings: Settings, plan: ReconciliationPlan) -> int:
    if plan.embedding_input_bytes > settings.embedding_byte_cap:
        raise IngestError("embedding input byte cap exceeded before writes")
    if not settings.allow_external_api or not settings.allow_writes:
        raise IngestError("reconciliation apply requires both authorization flags")
    pg_changed = sum(int(pg_save_document(settings, document)) for document in plan.postgres_updates)
    for index in range(0, len(plan.embedding_chunks), 32):
        batch = plan.embedding_chunks[index : index + 32]
        vectors = embed_texts(settings, [chunk.text for chunk in batch])
        qdrant_upsert(settings, list(zip(batch, vectors, strict=True)))
    for chunk in plan.payload_updates:
        qdrant_overwrite_payload(settings, chunk)
    if plan.stale_point_ids:
        qdrant_delete_points(settings, set(plan.stale_point_ids))
    return pg_changed


def run_interval(settings: Settings, end: datetime | None = None) -> dict[str, object]:
    pg_preflight(settings)
    qdrant_preflight(settings)
    last_end = read_checkpoint(settings)
    end = (end or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    start = max(settings.initial_since, last_end - settings.overlap)
    if end <= last_end or end <= start:
        raise IngestError("run interval is not newer than the checkpoint")
    discovered = discover_documents(settings, start, end)
    if len(discovered) > settings.discovery_limit:
        raise IngestError("discovery limit exceeded before PostgreSQL lookup")

    identities = [_listing_identity_version(item)[0] for item in discovered]
    existing = pg_load_listing_states(settings, identities)
    pg_backed_ids = [
        identity
        for item, identity in zip(discovered, identities, strict=True)
        if _state_allows_reconstruction(item, existing.get(identity))
    ]
    stored = pg_load_stored_bodies(settings, pg_backed_ids)
    reconstructed: dict[str, Document] = {}
    detail_candidates: list[dict[str, object]] = []
    for item, identity in zip(discovered, identities, strict=True):
        state = stored.get(identity)
        document = _reconstruct_stored_document(settings, item, state)
        if document is None:
            detail_candidates.append(item)
        else:
            existing[identity] = state
            reconstructed[identity] = document
    if len(detail_candidates) > settings.document_limit:
        raise IngestError("document limit exceeded before detail fetch")
    fetched: dict[str, Document] = {}
    for item in detail_candidates:
        identity, _unid, _internal, listing_modified = _listing_identity_version(item)
        document = normalize_document(fetch_document(settings, item), item, settings.source_base_url)
        if _timestamp(document.normalized.get("datum_zadnje_spremembe"), "dateModified") < listing_modified:
            raise IngestError("source detail is older than its discovery candidate")
        fetched[identity] = document
    documents = [reconstructed.get(identity) or fetched[identity] for identity in identities]
    current = qdrant_documents_points(settings, identities)
    plan = _plan_reconciliation(settings, documents, existing, current, set(fetched))
    pg_changed = apply_reconciliation(settings, plan)
    if fetched:
        verified = pg_load_listing_states(settings, list(fetched))
        verified_ids = [
            identity
            for item, identity in zip(detail_candidates, fetched, strict=True)
            if _state_allows_reconstruction(item, verified.get(identity))
        ]
        verified_bodies = pg_load_stored_bodies(settings, verified_ids)
        if any(
            _reconstruct_stored_document(settings, item, verified_bodies.get(identity)) is None
            for item, identity in zip(detail_candidates, fetched, strict=True)
        ):
            raise IngestError("source detail PostgreSQL persistence postcondition failed")
    write_checkpoint(settings, end)
    return {
        "interval_start": start.isoformat(),
        "interval_end": end.isoformat(),
        "documents": len(documents),
        "listing_candidates": len(discovered),
        "postgres_reconstructed": len(reconstructed),
        "source_details": len(detail_candidates),
        "discovery_limit": settings.discovery_limit,
        "document_limit": settings.document_limit,
        "postgres_changed": pg_changed,
        "vectors_embedded": len(plan.embedding_chunks),
        "payloads_updated": len(plan.payload_updates),
        "points_deleted": len(plan.stale_point_ids),
        "embedding_input_bytes": plan.embedding_input_bytes,
        "checkpoint_advanced": True,
    }
