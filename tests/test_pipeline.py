from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_source import Response, http_error, settings

from sodnapraksa_ingest import IngestError, cli, pipeline, source
from sodnapraksa_ingest.document import Chunk, Document, build_chunks, normalize_document

FIXTURE = Path(__file__).parent / "fixtures" / "decision.json"
END = datetime(2026, 8, 18, tzinfo=UTC)


def fixture(**changes: object) -> dict[str, object]:
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    value.update(changes)
    return value


def fixture_for(identity: int, **changes: object) -> dict[str, object]:
    return fixture(
        registryNumber=f"TEST{identity:06d}",
        unid=f"doc_{identity:016d}",
        documentUrl=f"/doc/test-{identity}",
        **changes,
    )


def discovery(raw: dict[str, object]) -> dict[str, object]:
    return {key: raw[key] for key in ("registryNumber", "unid", "dateModified")}


def qdrant_page(point_id: object | None, offset: object, identity: str = "TEST000001") -> dict[str, object]:
    points = [] if point_id is None else [{"id": str(point_id), "payload": {"evidencna_stevilka": identity}}]
    return {"result": {"points": points, "next_page_offset": offset}}


def stored_row(raw: dict[str, object], cfg) -> tuple[Document, dict[str, object]]:
    document = normalize_document(raw, discovery(raw), cfg.source_base_url)
    modified = datetime.fromisoformat(str(document.normalized["datum_zadnje_spremembe"]).replace("Z", "+00:00"))
    return document, {
        "evidencna_stevilka": document.evidencna,
        "unid": document.unid,
        "internal_id": document.internal_id,
        "database_name": document.database,
        "datum_zadnje_spremembe": modified,
        "content_hash": document.content_hash,
        "metadata_hash": document.metadata_hash,
        "status": "STORED",
        "deleted": False,
        "error": None,
        "raw_api": dict(raw),
        "has_raw_api": True,
        "has_normalized": True,
    }


def install_fakes(monkeypatch, holder: dict[str, object]) -> dict[str, object]:
    state: dict[str, object] = {
        "pg": {},
        "points": {},
        "fetch_calls": [],
        "pg_state_queries": [],
        "pg_body_queries": [],
        "q_reads": [],
        "pg_writes": [],
        "embed_calls": [],
        "q_writes": [],
        "checkpoints": [],
        "events": [],
    }
    monkeypatch.setattr(pipeline, "pg_preflight", lambda _settings: {})
    monkeypatch.setattr(pipeline, "qdrant_preflight", lambda _settings: {})
    monkeypatch.setattr(
        pipeline,
        "discover_documents",
        lambda _settings, _start, _end: list(holder.get("discovered") or [discovery(holder["raw"])]),
    )

    def fetch(_settings, item):
        state["fetch_calls"].append(item["registryNumber"])
        if failure := holder.get("failure"):
            raise failure
        details = holder.get("details") or {holder["raw"]["registryNumber"]: holder["raw"]}
        return dict(details[item["registryNumber"]])

    def load_states(_settings, identities: list[str]):
        state["pg_state_queries"].append(list(identities))
        state["events"].append("pg_states")
        return {identity: state["pg"][identity] for identity in identities if identity in state["pg"]}

    def load_bodies(_settings, identities: list[str]):
        state["pg_body_queries"].append(list(identities))
        state["events"].append("pg_bodies")
        return {identity: state["pg"][identity] for identity in identities if identity in state["pg"]}

    def save(cfg, document: Document) -> bool:
        _, row = stored_row(document.raw, cfg)
        state["pg"][document.evidencna] = row
        state["pg_writes"].append(document.evidencna)
        state["events"].append("pg_save")
        return True

    def points(_settings, identities: list[str]):
        state["q_reads"].append(list(identities))
        return {
            identity: {
                point_id: dict(payload) for point_id, payload in state["points"].items() if payload["evidencna_stevilka"] == identity
            }
            for identity in identities
        }

    def embed(_settings, texts: list[str]) -> list[list[float]]:
        state["embed_calls"].append(list(texts))
        return [[1.0, 2.0, 3.0] for _text in texts]

    def upsert(_settings, pairs) -> None:
        for chunk, _vector in pairs:
            state["points"][chunk.point_id] = dict(chunk.payload)
        state["q_writes"].append(("upsert", len(pairs)))

    def overwrite(_settings, chunk) -> None:
        state["points"][chunk.point_id] = dict(chunk.payload)
        state["q_writes"].append(("payload", 1))

    def delete(_settings, point_ids: set[str]) -> None:
        for point_id in point_ids:
            state["points"].pop(point_id, None)
        if point_ids:
            state["q_writes"].append(("delete", len(point_ids)))

    monkeypatch.setattr(pipeline, "fetch_document", fetch)
    monkeypatch.setattr(pipeline, "pg_load_listing_states", load_states)
    monkeypatch.setattr(pipeline, "pg_load_stored_bodies", load_bodies)
    monkeypatch.setattr(pipeline, "pg_save_document", save)
    monkeypatch.setattr(pipeline, "qdrant_documents_points", points)
    monkeypatch.setattr(pipeline, "embed_texts", embed)
    monkeypatch.setattr(pipeline, "qdrant_upsert", upsert)
    monkeypatch.setattr(pipeline, "qdrant_overwrite_payload", overwrite)
    monkeypatch.setattr(pipeline, "qdrant_delete_points", delete)

    def checkpoint(_settings, end) -> None:
        state["checkpoints"].append(end)
        state["events"].append("checkpoint")

    monkeypatch.setattr(pipeline, "write_checkpoint", checkpoint)
    return state


def seed_stored(state: dict[str, object], cfg, raw: dict[str, object], *, points: bool = True) -> Document:
    document, row = stored_row(raw, cfg)
    state["pg"][document.evidencna] = row
    if points:
        for chunk in build_chunks(document, cfg.embedding_model, cfg.embedding_dimensions):
            state["points"][chunk.point_id] = dict(chunk.payload)
    return document


def test_rerun_changes_and_qdrant_repair(monkeypatch, tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    holder: dict[str, object] = {"raw": fixture()}
    state = install_fakes(monkeypatch, holder)
    first = pipeline.run_interval(cfg, END)
    assert first["vectors_embedded"] == 3 and first["source_details"] == 1 and len(state["points"]) == 3
    counts = tuple(len(state[key]) for key in ("pg_writes", "embed_calls", "q_writes"))
    repeated = pipeline.run_interval(cfg, END)
    assert repeated["postgres_changed"] == 0 and repeated["vectors_embedded"] == 0 and repeated["source_details"] == 0
    assert counts == tuple(len(state[key]) for key in ("pg_writes", "embed_calls", "q_writes"))
    assert len(state["fetch_calls"]) == 1

    holder["raw"] = fixture(ordinalNumber="I Cp 2/2026", dateModified="2026-08-17T07:00:00Z")
    metadata = pipeline.run_interval(cfg, END)
    assert metadata["postgres_changed"] == 1 and metadata["vectors_embedded"] == 0 and metadata["source_details"] == 1
    assert metadata["payloads_updated"] == 3

    holder["raw"] = fixture(ordinalNumber="I Cp 2/2026", coreText="Spremenjeno jedro.", dateModified="2026-08-17T08:00:00Z")
    writes_before = len(state["q_writes"])
    content = pipeline.run_interval(cfg, END)
    assert content["vectors_embedded"] == 1 and content["points_deleted"] == 1
    assert state["q_writes"][writes_before:] == [("upsert", 1), ("payload", 1), ("payload", 1), ("delete", 1)]

    state["pg_writes"].clear()
    state["points"].pop(next(iter(state["points"])))
    fetches_before = len(state["fetch_calls"])
    repaired = pipeline.run_interval(cfg, END)
    assert repaired["vectors_embedded"] == 1 and repaired["source_details"] == 0 and not state["pg_writes"]
    assert len(state["fetch_calls"]) == fetches_before
    assert len(state["checkpoints"]) == 5

    for payload in state["points"].values():
        payload.pop("embedding_model")
        payload.pop("embedding_dimensions")
    established = replace(cfg, embedding_model="text-embedding-3-large", embedding_dimensions=3072)
    legacy = pipeline.run_interval(established, END)
    assert legacy["vectors_embedded"] == 0 and legacy["payloads_updated"] == 3

    for payload in state["points"].values():
        payload.pop("embedding_model")
        payload.pop("embedding_dimensions")
    incompatible = pipeline.run_interval(replace(established, embedding_model="different-model"), END)
    assert incompatible["vectors_embedded"] == 3


def test_pg_backed_overlap_exceeds_detail_limit_without_source_work(monkeypatch, tmp_path: Path) -> None:
    cfg = settings(tmp_path, limit=1, discovery_limit=4)
    raws = [fixture_for(index) for index in range(1, 4)]
    holder = {"raw": raws[0], "discovered": [discovery(raw) for raw in raws]}
    state = install_fakes(monkeypatch, holder)
    for index, raw in enumerate(raws):
        stored = {**raw, "dateModified": "2026-08-17T07:00:00Z"} if index == 2 else raw
        seed_stored(state, cfg, stored)

    result = pipeline.run_interval(cfg, END)

    assert result["listing_candidates"] == 3 and result["postgres_reconstructed"] == 3
    assert result["source_details"] == 0 and result["vectors_embedded"] == 0
    assert not state["fetch_calls"] and not state["embed_calls"]
    assert not state["pg_writes"] and not state["q_writes"] and state["checkpoints"] == [END]
    expected = [raw["registryNumber"] for raw in raws]
    assert state["pg_state_queries"] == [expected] and state["pg_body_queries"] == [expected]
    assert state["q_reads"] == [expected]


def test_pg_backed_payload_repair_uses_no_detail_or_embedding(monkeypatch, tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    raw = fixture()
    state = install_fakes(monkeypatch, {"raw": raw})
    seed_stored(state, cfg, raw)
    for payload in state["points"].values():
        payload["title"] = "stale synthetic payload"
    state["points"]["stale-point"] = {**next(iter(state["points"].values())), "chunk_id": "stale"}

    result = pipeline.run_interval(cfg, END)

    assert result["source_details"] == 0 and result["vectors_embedded"] == 0
    assert result["payloads_updated"] == 3 and result["points_deleted"] == 1 and len(state["q_writes"]) == 4
    assert not state["fetch_calls"] and not state["embed_calls"] and not state["pg_writes"]


def test_missing_newer_incomplete_failed_and_ambiguous_states_require_detail(monkeypatch, tmp_path: Path) -> None:
    cfg = settings(tmp_path, limit=5)
    raws = [fixture_for(index, dateModified="2026-08-17T08:00:00Z") for index in range(1, 6)]
    holder = {
        "raw": raws[0],
        "discovered": [discovery(raw) for raw in raws],
        "details": {raw["registryNumber"]: raw for raw in raws},
    }
    state = install_fakes(monkeypatch, holder)

    seed_stored(state, cfg, fixture_for(2, dateModified="2026-08-17T07:00:00Z"))
    seed_stored(state, cfg, raws[2])
    state["pg"][raws[2]["registryNumber"]]["raw_api"] = "malformed"
    seed_stored(state, cfg, raws[3])
    state["pg"][raws[3]["registryNumber"]]["status"] = "FAILED"
    seed_stored(state, cfg, raws[4])
    state["pg"][raws[4]["registryNumber"]]["unid"] = "doc_9999999999999999"

    result = pipeline.run_interval(cfg, END)

    expected = [raw["registryNumber"] for raw in raws]
    assert result["source_details"] == result["postgres_changed"] == 5 and state["fetch_calls"] == expected
    assert state["pg_writes"] == expected
    assert state["pg_body_queries"] == [[raws[2]["registryNumber"]], expected]


@pytest.mark.parametrize("detail_modified", ["2026-08-17T08:00:00Z", "2026-08-17T09:00:00Z"])
def test_detail_timestamp_equal_or_newer_is_accepted(monkeypatch, tmp_path: Path, detail_modified: str) -> None:
    listing = fixture(dateModified="2026-08-17T08:00:00Z")
    detail = {**listing, "dateModified": detail_modified}
    state = install_fakes(
        monkeypatch,
        {"raw": detail, "discovered": [discovery(listing)], "details": {listing["registryNumber"]: detail}},
    )

    result = pipeline.run_interval(settings(tmp_path), END)

    identity = listing["registryNumber"]
    assert result["source_details"] == result["postgres_changed"] == 1
    assert state["pg_state_queries"] == [[identity], [identity]]
    assert state["pg_body_queries"] == [[], [identity]]
    assert state["events"][-3:] == ["pg_states", "pg_bodies", "checkpoint"]


def test_older_detail_timestamp_fails_before_downstream_work(monkeypatch, tmp_path: Path) -> None:
    listing = fixture(dateModified="2026-08-17T08:00:00Z")
    detail = {**listing, "dateModified": "2026-08-17T07:00:00Z"}
    state = install_fakes(
        monkeypatch,
        {"raw": detail, "discovered": [discovery(listing)], "details": {listing["registryNumber"]: detail}},
    )

    with pytest.raises(IngestError, match="older than"):
        pipeline.run_interval(settings(tmp_path), END)

    assert len(state["fetch_calls"]) == 1 and not state["q_reads"]
    assert not state["embed_calls"] and not state["pg_writes"]
    assert not state["q_writes"] and not state["checkpoints"]


def test_detail_forces_identity_persistence_then_overlap_is_pg_backed(monkeypatch, tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    raw = fixture()
    state = install_fakes(monkeypatch, {"raw": raw})
    document = seed_stored(state, cfg, raw)
    state["pg"][document.evidencna]["unid"] = "doc_9999999999999999"
    state["pg"][document.evidencna]["internal_id"] = "9999999999999999"

    first = pipeline.run_interval(cfg, END)

    assert first["source_details"] == first["postgres_changed"] == 1
    assert first["vectors_embedded"] == first["payloads_updated"] == first["points_deleted"] == 0
    assert len(state["fetch_calls"]) == 1 and state["pg_writes"] == [document.evidencna]
    assert not state["embed_calls"] and not state["q_writes"]
    assert state["pg_state_queries"] == [[document.evidencna], [document.evidencna]]
    assert state["pg_body_queries"] == [[], [document.evidencna]]

    repeated = pipeline.run_interval(cfg, END)

    assert repeated["source_details"] == repeated["postgres_changed"] == repeated["vectors_embedded"] == 0
    assert repeated["payloads_updated"] == repeated["points_deleted"] == 0
    assert len(state["fetch_calls"]) == len(state["pg_writes"]) == 1
    assert not state["embed_calls"] and not state["q_writes"] and state["checkpoints"] == [END, END]


@pytest.mark.parametrize(
    "failure",
    ["missing", "older", "unsuccessful", "incomplete", "identity", "timestamp", "hash"],
)
def test_detail_persistence_postcondition_fails_closed(monkeypatch, tmp_path: Path, failure: str) -> None:
    cfg = settings(tmp_path)
    raw = fixture()
    state = install_fakes(monkeypatch, {"raw": raw})
    desired = normalize_document(raw, discovery(raw), cfg.source_base_url)
    for chunk in build_chunks(desired, cfg.embedding_model, cfg.embedding_dimensions):
        state["points"][chunk.point_id] = dict(chunk.payload)
    load_states = pipeline.pg_load_listing_states
    load_bodies = pipeline.pg_load_stored_bodies

    def corrupt_states(current_cfg, identities: list[str]):
        rows = load_states(current_cfg, identities)
        if len(state["pg_state_queries"]) != 2:
            return rows
        if failure == "missing":
            return {}
        row = dict(rows[raw["registryNumber"]])
        if failure == "older":
            row["datum_zadnje_spremembe"] -= timedelta(hours=1)
        elif failure == "unsuccessful":
            row["status"] = "FAILED"
        elif failure == "incomplete":
            row["has_raw_api"] = False
        elif failure == "identity":
            row["unid"] = "doc_9999999999999999"
        elif failure == "timestamp":
            row["datum_zadnje_spremembe"] = row["datum_zadnje_spremembe"].replace(tzinfo=None)
        return {raw["registryNumber"]: row}

    def corrupt_bodies(current_cfg, identities: list[str]):
        rows = load_bodies(current_cfg, identities)
        if failure == "hash" and len(state["pg_body_queries"]) == 2:
            row = dict(rows[raw["registryNumber"]])
            row["content_hash"] = "not-the-persisted-hash"
            return {raw["registryNumber"]: row}
        return rows

    monkeypatch.setattr(pipeline, "pg_load_listing_states", corrupt_states)
    monkeypatch.setattr(pipeline, "pg_load_stored_bodies", corrupt_bodies)

    with pytest.raises(IngestError, match="persistence postcondition"):
        pipeline.run_interval(cfg, END)

    assert state["pg_writes"] == [raw["registryNumber"]]
    assert not state["checkpoints"]


def test_detail_and_discovery_limits_fail_before_downstream_work_and_override_remains_effective(monkeypatch, tmp_path: Path) -> None:
    cfg = settings(tmp_path, limit=1, discovery_limit=2)
    raws = [fixture_for(index) for index in range(1, 4)]
    holder = {
        "raw": raws[0],
        "discovered": [discovery(raw) for raw in raws],
        "details": {raw["registryNumber"]: raw for raw in raws},
    }
    state = install_fakes(monkeypatch, holder)

    with pytest.raises(IngestError, match="discovery limit"):
        pipeline.run_interval(cfg, END)
    assert not state["pg_state_queries"] and not state["fetch_calls"] and not state["q_reads"]
    assert not state["embed_calls"] and not state["pg_writes"] and not state["q_writes"] and not state["checkpoints"]

    holder["discovered"] = holder["discovered"][:2]
    with pytest.raises(IngestError, match="document limit"):
        pipeline.run_interval(cfg, END)
    assert len(state["pg_state_queries"]) == 1 and not state["fetch_calls"] and not state["q_reads"]
    assert not state["embed_calls"] and not state["pg_writes"] and not state["q_writes"] and not state["checkpoints"]

    result = pipeline.run_interval(replace(cfg, document_limit=2), END)
    assert result["source_details"] == 2 and len(state["fetch_calls"]) == 2


def test_pg_backed_embedding_byte_cap_stops_all_writes(monkeypatch, tmp_path: Path) -> None:
    cfg = replace(settings(tmp_path), embedding_byte_cap=1)
    raw = fixture()
    state = install_fakes(monkeypatch, {"raw": raw})
    seed_stored(state, cfg, raw, points=False)

    with pytest.raises(IngestError, match="byte cap"):
        pipeline.run_interval(cfg, END)

    assert not state["fetch_calls"] and not state["embed_calls"]
    assert not state["pg_writes"] and not state["q_writes"] and not state["checkpoints"]


def test_failure_and_byte_cap_keep_checkpoint_and_stop_writes(monkeypatch, tmp_path: Path) -> None:
    holder: dict[str, object] = {"raw": fixture(), "failure": IngestError("detail unavailable")}
    state = install_fakes(monkeypatch, holder)
    with pytest.raises(IngestError, match="unavailable"):
        pipeline.run_interval(settings(tmp_path), END)
    assert not state["checkpoints"] and not state["pg_writes"]

    holder = {"raw": fixture()}
    state = install_fakes(monkeypatch, holder)
    with pytest.raises(IngestError, match="byte cap"):
        pipeline.run_interval(replace(settings(tmp_path), embedding_byte_cap=1), END)
    assert not state["pg_writes"] and not state["embed_calls"]
    assert not state["q_writes"] and not state["checkpoints"]


def test_atomic_checkpoint_and_second_cli_lock_exit_75(monkeypatch, tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    pipeline.write_checkpoint(cfg, END)
    assert pipeline.read_checkpoint(cfg) == END
    first = pipeline.acquire_lock(cfg.lock_path)
    monkeypatch.setattr(cli, "load_settings", lambda _mode, _env: cfg)
    try:
        assert cli.main(["preflight"], env={}) == 75
    finally:
        first.close()


def test_cli_optional_fixed_end(monkeypatch, tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    omitted = object()
    ends: list[object] = []

    def run(_settings, end=omitted):
        ends.append(end)
        return {}

    monkeypatch.setattr(cli, "load_settings", lambda _mode, _env: cfg)
    monkeypatch.setattr(cli, "run_interval", run)
    assert cli.main(["run"], env={}) == 0
    assert cli.main(["run", "--end", "2026-08-18T00:00:00Z"], env={}) == 0
    assert cli.main(["run", "--end", "2026-08-18T02:00:00+02:00"], env={}) == 0
    assert ends == [omitted, END, END]

    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    for invalid in ("not-a-timestamp", "2026-08-18T00:00:00", future):
        assert cli.main(["run", "--end", invalid], env={}) == 78
    assert ends == [omitted, END, END]


def test_qdrant_scroll_paginates(monkeypatch, tmp_path: Path) -> None:
    offset = "00000000-0000-0000-0000-000000000001"
    pages = iter([qdrant_page("a", offset), qdrant_page("b", None)])
    bodies: list[dict[str, object]] = []

    def request(_settings, _method, _path, body, **_kwargs):
        bodies.append(body)
        return next(pages)

    monkeypatch.setattr(pipeline, "_qdrant_request", request)
    found = pipeline.qdrant_documents_points(settings(tmp_path), ["TEST000001"])
    assert set(found["TEST000001"]) == {"a", "b"} and bodies[1]["offset"] == offset
    assert bodies[0]["filter"]["must"][0]["match"] == {"any": ["TEST000001"]}


def test_qdrant_scroll_one_page_terminates(monkeypatch, tmp_path: Path) -> None:
    bodies: list[dict[str, object]] = []
    monkeypatch.setattr(
        pipeline,
        "_qdrant_request",
        lambda _settings, _method, _path, body, **_kwargs: bodies.append(body) or qdrant_page(None, None),
    )

    assert pipeline.qdrant_documents_points(settings(tmp_path), ["TEST000001"]) == {"TEST000001": {}}
    assert len(bodies) == 1 and "offset" not in bodies[0]


@pytest.mark.parametrize(
    "offsets",
    [
        [1, 1],
        [1, 2, 1],
        [1, "00000000-0000-0000-0000-000000000001"],
    ],
)
def test_qdrant_scroll_rejects_repeated_cyclic_or_type_changing_offsets(monkeypatch, tmp_path: Path, offsets: list[object]) -> None:
    pages = iter(offsets)
    point = 0

    def request(_settings, _method, _path, _body, **_kwargs):
        nonlocal point
        point += 1
        return qdrant_page(point, next(pages))

    monkeypatch.setattr(pipeline, "_qdrant_request", request)
    with pytest.raises(IngestError, match="offset"):
        pipeline.qdrant_documents_points(settings(tmp_path), ["TEST000001"])


@pytest.mark.parametrize("offset", [True, -1, 2**64, 1.5, "", "not-a-uuid", {}, []])
def test_qdrant_scroll_rejects_malformed_offsets(monkeypatch, tmp_path: Path, offset: object) -> None:
    monkeypatch.setattr(
        pipeline,
        "_qdrant_request",
        lambda *_args, **_kwargs: qdrant_page("one", offset),
    )
    with pytest.raises(IngestError, match="invalid offset"):
        pipeline.qdrant_documents_points(settings(tmp_path), ["TEST000001"])


def test_qdrant_scroll_rejects_empty_continuation_page(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        pipeline,
        "_qdrant_request",
        lambda *_args, **_kwargs: qdrant_page(None, 1),
    )
    with pytest.raises(IngestError, match="did not advance"):
        pipeline.qdrant_documents_points(settings(tmp_path), ["TEST000001"])


@pytest.mark.parametrize(("offsets", "error"), [([1, 2], "page limit"), ([1, None], None)])
def test_qdrant_scroll_page_bound(monkeypatch, tmp_path: Path, offsets: list[object], error: str | None) -> None:
    pages = iter(offsets)
    point = 0

    def request(_settings, _method, _path, _body, **_kwargs):
        nonlocal point
        point += 1
        return qdrant_page(point, next(pages))

    monkeypatch.setattr(pipeline, "QDRANT_SCROLL_MAX_PAGES", 2)
    monkeypatch.setattr(pipeline, "_qdrant_request", request)
    if error:
        with pytest.raises(IngestError, match=error):
            pipeline.qdrant_documents_points(settings(tmp_path), ["TEST000001"])
    else:
        assert len(pipeline.qdrant_documents_points(settings(tmp_path), ["TEST000001"])["TEST000001"]) == 2


def test_qdrant_pagination_failure_stops_pipeline_before_side_effects(monkeypatch, tmp_path: Path) -> None:
    real_lookup = pipeline.qdrant_documents_points
    cfg = settings(tmp_path)
    raw = fixture()
    state = install_fakes(monkeypatch, {"raw": raw})
    seed_stored(state, cfg, raw)
    point = 0

    def request(_settings, _method, _path, _body, **_kwargs):
        nonlocal point
        point += 1
        return qdrant_page(point, 1, raw["registryNumber"])

    monkeypatch.setattr(pipeline, "qdrant_documents_points", real_lookup)
    monkeypatch.setattr(pipeline, "_qdrant_request", request)
    with pytest.raises(IngestError, match="offset repeated"):
        pipeline.run_interval(cfg, END)

    assert not state["fetch_calls"] and not state["embed_calls"]
    assert not state["pg_writes"] and not state["q_writes"] and not state["checkpoints"]


def test_qdrant_lookup_batches_document_filters(monkeypatch, tmp_path: Path) -> None:
    bodies: list[dict[str, object]] = []

    def request(_settings, _method, _path, body, **_kwargs):
        bodies.append(body)
        return qdrant_page(None, None)

    identities = [f"TEST{index:06d}" for index in range(257)]
    monkeypatch.setattr(pipeline, "_qdrant_request", request)
    found = pipeline.qdrant_documents_points(settings(tmp_path), identities)

    assert list(found) == identities and len(bodies) == 2
    assert [len(body["filter"]["must"][0]["match"]["any"]) for body in bodies] == [256, 1]


def test_qdrant_transient_requests_retain_three_attempts(monkeypatch, tmp_path: Path) -> None:
    attempts = iter([http_error(503), http_error(429), Response({"status": "ok"})])
    calls = 0

    def open_transient(*_args: object, **_kwargs: object):
        nonlocal calls
        calls += 1
        value = next(attempts)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(source.urllib.request, "urlopen", open_transient)
    monkeypatch.setattr(source.time, "sleep", lambda _seconds: None)
    assert pipeline._qdrant_request(settings(tmp_path), "GET", "") == {"status": "ok"}
    assert calls == 3


@pytest.mark.parametrize(("count", "expected_batches"), [(32, [32]), (33, [32, 1])])
def test_embedding_batches_contain_at_most_32_inputs(monkeypatch, tmp_path: Path, count: int, expected_batches: list[int]) -> None:
    chunks = tuple(Chunk(str(index), "x", {}) for index in range(count))
    plan = pipeline.ReconciliationPlan((), chunks, (), frozenset(), count)
    batches: list[int] = []

    def embed(_settings, texts: list[str]) -> list[list[float]]:
        batches.append(len(texts))
        return [[1.0, 2.0, 3.0] for _text in texts]

    monkeypatch.setattr(pipeline, "embed_texts", embed)
    monkeypatch.setattr(pipeline, "qdrant_upsert", lambda _settings, _pairs: None)
    assert pipeline.apply_reconciliation(settings(tmp_path), plan) == 0
    assert batches == expected_batches
