from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest

from sodnapraksa_ingest import IngestError
from sodnapraksa_ingest.document import build_chunks, normalize_document, split_chunks

FIXTURE = Path(__file__).parent / "fixtures" / "decision.json"


def raw(**changes: object) -> dict[str, object]:
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    value.update(changes)
    return value


def discovery(value: dict[str, object]) -> dict[str, object]:
    return {"registryNumber": value["registryNumber"], "unid": value["unid"]}


@pytest.mark.parametrize("database", ["doc", "seu"])
def test_doc_and_seu_hashes_identities_and_payload_types(database: str) -> None:
    value = raw(sourceDb=database)
    document = normalize_document(value, discovery(value), "https://source.example.invalid/api2")
    same = normalize_document(value, discovery(value), "https://source.example.invalid/api2")
    assert document == same
    metadata = raw(sourceDb=database, ordinalNumber="I Cp 2/2026")
    metadata_document = normalize_document(metadata, discovery(metadata), "https://source.example.invalid/api2")
    assert metadata_document.content_hash == document.content_hash and metadata_document.metadata_hash != document.metadata_hash
    changed = raw(sourceDb=database, coreText="Spremenjeno jedro.")
    assert normalize_document(changed, discovery(changed), "https://source.example.invalid/api2").content_hash != document.content_hash
    chunks = build_chunks(document, "test-model", 3)
    assert [chunk.payload["section"] for chunk in chunks] == [
        "jedro",
        "izrek",
        "obrazlozitev",
    ]
    first = chunks[0]
    chunk_hash = hashlib.sha256(first.text.encode()).hexdigest()
    chunk_id = hashlib.sha256(f"{document.evidencna}|jedro|0|{chunk_hash}".encode()).hexdigest()
    assert first.payload["chunk_id"] == chunk_id
    assert first.point_id == str(uuid.uuid5(uuid.NAMESPACE_URL, "lexai:sodnapraksa:" + chunk_id))
    assert first.payload["is_key_decision"] is False and first.payload["is_on_key_decision_list"] is False
    assert isinstance(first.payload["chunk_index"], int)


def test_art_and_inj_normalization() -> None:
    complete = "<article><p>" + ("Celotno vsebinsko besedilo s pravno razlago. " * 12) + "</p></article>"
    article = raw(
        sourceDb="art",
        coreText=None,
        ruling=None,
        explanation=None,
        content="Vsebina članka.",
        notes="Povzetek članka.",
        source="Avtor",
        publication="Pravna praksa 1/2026",
        complete_article=complete,
    )
    art = normalize_document(article, discovery(article), "https://source.example.invalid/api2")
    assert art.normalized["jedro"] == "Povzetek članka."
    assert "Vsebina članka." in art.normalized["obrazlozitev"] and "Celotno vsebinsko" in art.normalized["obrazlozitev"]
    assert art.normalized["author"] == "Avtor" and "literature" not in art.normalized
    metadata = normalize_document({**article, "source": "Drug avtor"}, discovery(article), "https://source.example.invalid/api2")
    assert metadata.content_hash == art.content_hash and metadata.metadata_hash != art.metadata_hash
    article["complete_article"] = complete.replace("<p>", "<p>  ")
    assert normalize_document(article, discovery(article), "https://source.example.invalid/api2").content_hash == art.content_hash
    malformed = normalize_document(
        {**article, "complete_article": "<nav>Domov Iskanje Kazalo</nav>"}, discovery(article), "https://source.example.invalid/api2"
    )
    assert malformed.normalized["obrazlozitev"] == "Vsebina članka."
    assert "Celotno vsebinsko" in " ".join(chunk.text for chunk in build_chunks(art, "test-model", 3))

    seu_value = raw(sourceDb="seu", coreText=None, explanation=None, explanationHtml=None, content="Celotno besedilo SEU.")
    seu = normalize_document(seu_value, discovery(seu_value), "https://source.example.invalid/api2")
    assert seu.normalized["obrazlozitev"] == "Celotno besedilo SEU."

    injury = raw(
        sourceDb="inj",
        coreText=None,
        ruling=None,
        explanation=None,
        compensation="10.000 EUR",
        injuryEvent="Prometna nesreča",
        injury="Zlom",
        physicalPain="Hude bolečine",
    )
    inj = normalize_document(injury, discovery(injury), "https://source.example.invalid/api2")
    assert "10.000 EUR" in inj.normalized["jedro"]
    assert "Prometna nesreča" in inj.normalized["obrazlozitev"]

    opinion_raw = raw(explanationHtml='LOČENO MNENJE [ATTACH id="42"]LOČENO MNENJE[/ATTACH]')
    opinion = normalize_document(opinion_raw, discovery(opinion_raw), "https://source.example.invalid/api2")
    assert opinion.normalized["attachment_ids"] == ["42"] and opinion.normalized["locena_mnenja_count"] == 1


@pytest.mark.parametrize(("true_token", "false_token"), [("D", "N"), ("Y", None), (True, False)])
def test_decision_flag_contract(true_token: object, false_token: object) -> None:
    value = raw(isKeyDecisions=true_token, isOnKeyDecisionList=false_token)
    document = normalize_document(value, discovery(value), "https://source.example.invalid/api2")
    assert document.normalized["is_key_decision"] is True and document.normalized["is_on_key_decision_list"] is False


def test_decision_flag_contract_fails_closed() -> None:
    value = raw(isKeyDecisions="unknown")
    with pytest.raises(IngestError, match="source boolean has an unsupported value"):
        normalize_document(value, discovery(value), "https://source.example.invalid/api2")


def test_adaptive_chunking_is_bounded_and_deterministic() -> None:
    text = ("Prvi dovolj dolg stavek. " * 180).strip()
    chunks = split_chunks(text)
    assert chunks == split_chunks(text)
    assert len(chunks) > 1
    assert all(chunk and len(chunk) <= 3500 for chunk in chunks)
