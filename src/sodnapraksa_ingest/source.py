"""Direct source and embedding HTTP functions."""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from . import IngestError
from .config import Settings

TRANSIENT = {429, 500, 502, 503, 504}


def request_json_with_retry(
    method: str,
    url: str,
    *,
    payload: Mapping[str, object] | None = None,
    headers: Mapping[str, str] | None = None,
    label: str,
    attempts: int = 3,
) -> dict[str, object]:
    """Make one verified-TLS JSON request with bounded attempts."""

    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    request_headers = {"Accept": "application/json", **dict(headers or {})}
    if data is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    last = "connection failure"
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                parsed = json.loads(response.read().decode("utf-8"))
            if not isinstance(parsed, dict):
                raise IngestError(f"{label} returned non-object JSON")
            return parsed
        except urllib.error.HTTPError as exc:
            if exc.code not in TRANSIENT:
                raise IngestError(f"{label} returned permanent HTTP {exc.code}") from None
            last = f"HTTP {exc.code}"
        except (TimeoutError, urllib.error.URLError, ConnectionError) as exc:
            last = type(exc).__name__
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IngestError(f"{label} returned malformed JSON") from exc
        if attempt < attempts - 1:
            time.sleep(min(0.5 * (2**attempt), 2.0))
    raise IngestError(f"{label} exhausted retries ({last})")


def _timestamp(value: object, label: str) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IngestError(f"invalid source timestamp: {label}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _internal_id(unid: str) -> str:
    suffix = unid.rsplit("_", 1)[-1]
    return suffix if suffix.isdigit() else ""


def discover_documents(
    settings: Settings,
    start: datetime,
    end: datetime,
) -> list[dict[str, object]]:
    """Read complete zero-based pages and retain dateModified in [start, end)."""

    portal_end = end if end.time() == datetime.min.time() else end + timedelta(days=1)
    page = 0
    raw_seen = 0
    expected_hits: int | None = None
    accepted: list[dict[str, object]] = []
    seen_evidencne: set[str] = set()
    seen_unids: set[str] = set()
    while True:
        query = urllib.parse.urlencode(
            {
                "apiKey": settings.source_api_key,
                "q": "",
                "page": page,
                "itemsPerPage": 100,
                "dateModifiedFrom": start.strftime("%d.%m.%Y"),
                "dateModifiedTo": portal_end.strftime("%d.%m.%Y"),
            }
        )
        response = request_json_with_retry(
            "GET",
            f"{settings.source_base_url}/mainSearchFull/?{query}",
            label="source discovery",
        )
        try:
            hits = int(response["hits"])
        except (KeyError, TypeError, ValueError) as exc:
            raise IngestError("source discovery returned invalid hits") from exc
        if hits < 0 or (expected_hits is not None and hits != expected_hits):
            raise IngestError("source hit count changed during pagination")
        expected_hits = hits
        rows = response.get("documents")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise IngestError("source discovery returned invalid documents")
        raw_seen += len(rows)
        if raw_seen > hits:
            raise IngestError("source pagination exceeded advertised hits")
        for row in rows:
            evidencna = str(row.get("registryNumber") or "").strip()
            unid = str(row.get("unid") or "").strip()
            internal = _internal_id(unid)
            if not evidencna or not unid or not internal:
                raise IngestError("source discovery identity is incomplete")
            if evidencna in seen_evidencne or unid in seen_unids:
                raise IngestError("source pagination returned a duplicate identity")
            seen_evidencne.add(evidencna)
            seen_unids.add(unid)
            modified = _timestamp(row.get("dateModified"), "dateModified")
            if start <= modified < end:
                accepted.append(dict(row))
                if len(accepted) > settings.discovery_limit:
                    return accepted
        if raw_seen == hits:
            return accepted
        if not rows:
            raise IngestError("source pagination ended before advertised hits")
        page += 1


def fetch_document(settings: Settings, discovered: Mapping[str, object]) -> dict[str, object]:
    """Fetch exactly one detail document; absence is a run failure, not withdrawal."""

    unid = str(discovered.get("unid") or "").strip()
    query = urllib.parse.urlencode({"apiKey": settings.source_api_key})
    response = request_json_with_retry(
        "GET",
        f"{settings.source_base_url}/show/{urllib.parse.quote(unid, safe='')}/?{query}",
        label="source detail",
    )
    rows = response.get("documents")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise IngestError("source detail did not return exactly one document")
    return dict(rows[0])


def embed_texts(settings: Settings, texts: list[str]) -> list[list[float]]:
    """Call the configured OpenAI-compatible embedding endpoint."""

    if not texts:
        return []
    response = request_json_with_retry(
        "POST",
        f"{settings.embedding_base_url}/embeddings",
        payload={
            "model": settings.embedding_model,
            "dimensions": settings.embedding_dimensions,
            "input": texts,
        },
        headers={"Authorization": f"Bearer {settings.embedding_api_key}"},
        label="embedding provider",
        attempts=1,
    )
    actual_model = str(response.get("model") or "").strip()
    if actual_model != settings.embedding_model:
        raise IngestError("embedding provider returned an unexpected model")
    rows = response.get("data")
    if not isinstance(rows, list) or len(rows) != len(texts):
        raise IngestError("embedding provider returned invalid data")
    vectors: dict[int, list[float]] = {}
    for row in rows:
        if not isinstance(row, dict) or type(row.get("index")) is not int or row["index"] in vectors:
            raise IngestError("embedding provider returned invalid data")
        vector = row.get("embedding")
        if not isinstance(vector, list) or any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) for value in vector
        ):
            raise IngestError("embedding provider returned invalid data")
        vectors[row["index"]] = [float(value) for value in vector]
    if set(vectors) != set(range(len(texts))):
        raise IngestError("embedding provider omitted a requested vector")
    ordered = [vectors[index] for index in range(len(texts))]
    if any(len(vector) != settings.embedding_dimensions for vector in ordered):
        raise IngestError("embedding vector dimension mismatch")
    return ordered
