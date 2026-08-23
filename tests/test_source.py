from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sodnapraksa_ingest import IngestError, source
from sodnapraksa_ingest.config import ConfigError, Settings, load_settings


def settings(tmp_path: Path, limit: int = 10, discovery_limit: int = 100) -> Settings:
    return Settings(
        database_url="postgresql://user:password@db.example.invalid/appdb",
        expected_database="appdb",
        expected_schema="public",
        source_base_url="https://source.example.invalid/api2",
        source_api_key="source-key",
        qdrant_url="https://qdrant.example.invalid",
        qdrant_api_key="",
        qdrant_collection="collection",
        embedding_base_url="https://embedding.example.invalid/v1",
        embedding_api_key="embedding-key",
        embedding_model="test-model",
        embedding_dimensions=3,
        checkpoint_path=tmp_path / "checkpoint.json",
        lock_path=tmp_path / "ingest.lock",
        initial_since=datetime(2026, 8, 1, tzinfo=UTC),
        overlap=timedelta(days=7),
        discovery_limit=discovery_limit,
        document_limit=limit,
        embedding_byte_cap=100000,
        allow_external_api=True,
        allow_writes=True,
    )


def row(identity: int, created: str, modified: str) -> dict[str, object]:
    return {
        "registryNumber": f"TEST{identity:06d}",
        "unid": f"doc_{identity:016d}",
        "dateCreated": created,
        "dateModified": modified,
    }


def test_zero_based_complete_pagination_uses_date_modified(monkeypatch, tmp_path: Path) -> None:
    pages = [
        {
            "hits": 3,
            "documents": [
                row(1, "2020-01-01T00:00:00Z", "2026-08-17T10:00:00Z"),
                row(2, "2026-08-17T00:00:00Z", "2026-08-17T11:00:00Z"),
            ],
        },
        {
            "hits": 3,
            "documents": [{**row(3, "2020-01-01T00:00:00Z", "2026-08-18T00:00:00Z"), "unid": "art_0000000000000001"}],
        },
    ]
    urls: list[str] = []

    def fake(_method: str, url: str, **_kwargs: object) -> dict[str, object]:
        urls.append(url)
        page = int(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["page"][0])
        return pages[page]

    monkeypatch.setattr(source, "request_json_with_retry", fake)
    found = source.discover_documents(
        settings(tmp_path),
        datetime(2026, 8, 16, tzinfo=UTC),
        datetime(2026, 8, 18, tzinfo=UTC),
    )
    assert [item["registryNumber"] for item in found] == ["TEST000001", "TEST000002"]
    assert [urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["page"] for url in urls] == [
        ["0"],
        ["1"],
    ]
    first_query = urllib.parse.parse_qs(urllib.parse.urlsplit(urls[0]).query)
    assert first_query["dateModifiedFrom"] == ["16.08.2026"] and first_query["dateModifiedTo"] == ["18.08.2026"]


def test_intraday_end_uses_next_day_envelope_and_exact_filter(monkeypatch, tmp_path: Path) -> None:
    start = datetime(2026, 8, 13, 2, 30, 1, tzinfo=UTC)
    end = datetime(2026, 8, 20, 5, 42, 33, tzinfo=UTC)
    same_day = [row(index, "2020-01-01T00:00:00Z", "2026-08-20T04:00:00Z") for index in range(1, 263)]
    portal_rows = [
        *same_day,
        row(263, "2020-01-01T00:00:00Z", "2026-08-20T05:42:33Z"),
        row(264, "2020-01-01T00:00:00Z", "2026-08-20T06:00:00Z"),
    ]
    queries: list[dict[str, list[str]]] = []

    def fake(_method: str, url: str, **_kwargs: object) -> dict[str, object]:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        queries.append(query)
        portal_end = datetime.strptime(query["dateModifiedTo"][0], "%d.%m.%Y").replace(tzinfo=UTC)
        returned = [item for item in portal_rows if source._timestamp(item["dateModified"], "dateModified") < portal_end]
        return {"hits": len(returned), "documents": returned}

    monkeypatch.setattr(source, "request_json_with_retry", fake)
    planner_listing = source.discover_documents(settings(tmp_path, discovery_limit=264), start, datetime(2026, 8, 22, 2, 30, tzinfo=UTC))
    planner_admitted = [item for item in planner_listing if start <= source._timestamp(item["dateModified"], "dateModified") < end]
    recovery_admitted = source.discover_documents(settings(tmp_path, discovery_limit=262), start, end)

    assert [query["dateModifiedTo"] for query in queries] == [["23.08.2026"], ["21.08.2026"]]
    assert recovery_admitted == planner_admitted == same_day


def test_discovery_limit_returns_limit_plus_one(monkeypatch, tmp_path: Path) -> None:
    response = {
        "hits": 2,
        "documents": [
            row(1, "2020-01-01T00:00:00Z", "2026-08-17T10:00:00Z"),
            row(2, "2020-01-01T00:00:00Z", "2026-08-17T11:00:00Z"),
        ],
    }
    monkeypatch.setattr(source, "request_json_with_retry", lambda *_args, **_kwargs: response)
    found = source.discover_documents(
        settings(tmp_path, discovery_limit=1),
        datetime(2026, 8, 16, tzinfo=UTC),
        datetime(2026, 8, 18, tzinfo=UTC),
    )
    assert len(found) == 2


class Response:
    def __init__(self, value: dict[str, object]) -> None:
        self.body = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.invalid/?key=sensitive", status, "error", {}, io.BytesIO(b"private"))


def test_source_retry_is_bounded_and_permanent_error_is_not_retried(monkeypatch, tmp_path: Path) -> None:
    attempts = iter([http_error(503), http_error(429), Response({"documents": [{"ok": True}]})])
    calls: list[int] = []

    def open_transient(*_args: object, **_kwargs: object):
        calls.append(1)
        value = next(attempts)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(source.urllib.request, "urlopen", open_transient)
    monkeypatch.setattr(source.time, "sleep", lambda _seconds: None)
    assert source.fetch_document(settings(tmp_path), {"unid": "doc_0000000000000001"}) == {"ok": True}
    assert len(calls) == 3
    permanent_calls = 0

    def open_permanent(*_args: object, **_kwargs: object):
        nonlocal permanent_calls
        permanent_calls += 1
        raise http_error(400)

    monkeypatch.setattr(source.urllib.request, "urlopen", open_permanent)
    with pytest.raises(IngestError) as raised:
        source.request_json_with_retry("GET", "https://example.invalid", label="test")
    assert permanent_calls == 1
    assert "sensitive" not in str(raised.value) and "private" not in str(raised.value)


def test_embedding_transient_failure_has_one_attempt(monkeypatch, tmp_path: Path) -> None:
    calls = 0

    def open_transient(*_args: object, **_kwargs: object):
        nonlocal calls
        calls += 1
        raise http_error(503)

    monkeypatch.setattr(source.urllib.request, "urlopen", open_transient)
    with pytest.raises(IngestError, match="embedding provider"):
        source.embed_texts(settings(tmp_path), ["besedilo"])
    assert calls == 1


@pytest.mark.parametrize(
    "response,match",
    [
        ({"model": "", "data": [{"index": 0, "embedding": [1, 2, 3]}]}, "unexpected model"),
        ({"model": "test-model", "data": []}, "invalid data"),
        ({"model": "test-model", "data": [{"index": 0, "embedding": [1, 2]}]}, "dimension mismatch"),
    ],
)
def test_embedding_response_contract(monkeypatch, tmp_path: Path, response: dict[str, object], match: str) -> None:
    monkeypatch.setattr(source, "request_json_with_retry", lambda *_args, **_kwargs: response)
    with pytest.raises(IngestError, match=match):
        source.embed_texts(settings(tmp_path), ["besedilo"])


def test_discovery_limit_is_required_and_validated_centrally(tmp_path: Path) -> None:
    env = {
        "SODNAPRAKSA_DATABASE_URL": "postgresql://user:password@db.example.invalid/appdb",
        "SODNAPRAKSA_EXPECTED_DATABASE": "appdb",
        "SODNAPRAKSA_EXPECTED_SCHEMA": "public",
        "SODNAPRAKSA_SOURCE_BASE_URL": "https://source.example.invalid/api2",
        "SODNAPRAKSA_SOURCE_API_KEY": "source-key",
        "SODNAPRAKSA_QDRANT_URL": "https://qdrant.example.invalid",
        "SODNAPRAKSA_QDRANT_COLLECTION": "collection",
        "SODNAPRAKSA_EMBEDDING_BASE_URL": "https://embedding.example.invalid/v1",
        "SODNAPRAKSA_EMBEDDING_API_KEY": "embedding-key",
        "SODNAPRAKSA_EMBEDDING_MODEL": "test-model",
        "SODNAPRAKSA_EMBEDDING_DIMENSIONS": "3",
        "SODNAPRAKSA_CHECKPOINT_PATH": str(tmp_path / "checkpoint.json"),
        "SODNAPRAKSA_LOCK_PATH": str(tmp_path / "ingest.lock"),
        "SODNAPRAKSA_INITIAL_SINCE": "2026-08-01T00:00:00Z",
        "SODNAPRAKSA_OVERLAP_DAYS": "7",
        "SODNAPRAKSA_DISCOVERY_LIMIT": "1000",
        "SODNAPRAKSA_DOCUMENT_LIMIT": "350",
        "SODNAPRAKSA_MAX_EMBEDDING_INPUT_BYTES_PER_RUN": "2000000",
        "SODNAPRAKSA_ALLOW_EXTERNAL_API": "1",
        "SODNAPRAKSA_ALLOW_WRITES": "1",
    }
    assert load_settings("run", env).discovery_limit == 1000
    for invalid in ("", "0", "not-an-integer"):
        with pytest.raises(ConfigError):
            load_settings("run", {**env, "SODNAPRAKSA_DISCOVERY_LIMIT": invalid})
