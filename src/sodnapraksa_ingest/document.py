"""Source normalization, hashes, chunks, and Qdrant payloads."""

from __future__ import annotations

import hashlib
import html
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

from . import IngestError


@dataclass(frozen=True)
class Document:
    evidencna: str
    unid: str
    internal_id: str
    database: str
    content_hash: str
    metadata_hash: str
    normalized: dict[str, object]
    raw: dict[str, object]


@dataclass(frozen=True)
class Chunk:
    point_id: str
    text: str
    payload: dict[str, object]


class _HTMLText(HTMLParser):
    """Minimal standard-library HTML text protocol implementation."""

    blocks = {"br", "div", "h1", "h2", "h3", "li", "p", "table", "td", "th", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in self.blocks:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in self.blocks:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def clean_text(value: object) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    if re.search(r"</?[A-Za-z][^>]*>", text):
        parser = _HTMLText()
        try:
            parser.feed(text)
            parser.close()
        except Exception as exc:
            raise IngestError("source HTML could not be normalized") from exc
        text = "".join(parser.parts)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _strings(value: object) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return list(dict.fromkeys(text for item in values if (text := clean_text(item))))


def _boolean(value: object) -> bool:
    if isinstance(value, bool):
        return value
    text = clean_text(value).casefold()
    if text in {"", "0", "false", "n"}:
        return False
    if text in {"d", "y"}:
        return True
    raise IngestError("source boolean has an unsupported value")


def _timestamp(value: object) -> str:
    text = clean_text(value)
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IngestError("source timestamp is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _hash(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _sections(raw: Mapping[str, object], database: str) -> tuple[str, str, str]:
    if database == "art":
        notes = clean_text(raw.get("notes"))
        content = clean_text(raw.get("content"))
        try:
            complete = clean_text(raw.get("complete_article"))
        except IngestError:
            complete = ""
        words = re.findall(r"[^\W\d_]{2,}", complete)
        if len(complete) < 200 or len(words) < 30 or not re.search(r"[.!?](?:\s|$)", complete):
            complete = ""
        parts = [content] if content and content not in complete else []
        if complete:
            parts.append(complete)
        return notes, "", "\n\n".join(parts)
    if database == "seu":
        return (
            clean_text(raw.get("coreText")),
            clean_text(raw.get("ruling") or raw.get("decree")),
            clean_text(raw.get("explanation") or raw.get("content")),
        )
    if database == "inj":
        fields = (
            ("Odškodnina", "compensation"),
            ("Odškodnina v povprečnih plačah", "compensationAvgSalary"),
            ("Škodni dogodek", "injuryEvent"),
            ("Oškodovanec", "victim"),
            ("Poškodba", "injury"),
            ("Zdravljenje", "treatment"),
            ("Hospitalizacija", "hospitalization"),
            ("Rehabilitacija", "rehabilitation"),
            ("Nevšečnosti med zdravljenjem", "inconvenience"),
            ("Telesne bolečine", "physicalPain"),
            ("Strah", "fear"),
            ("Zmanjšanje življenjske aktivnosti", "reducedLifeActivity"),
            ("Skaženost", "disfigurement"),
            ("Poseg v osebnostne pravice", "otherViolationPersonalityRights"),
            ("Fischerjeva skupina", "fisherGroups"),
            ("Pravna podlaga / zveza", "connection"),
            ("Opombe", "notes"),
        )
        values = {key: clean_text(raw.get(key)) for _, key in fields}
        core_keys = (
            "compensation injuryEvent injury physicalPain fear reducedLifeActivity otherViolationPersonalityRights".split()  # noqa: SIM905
        )
        core = [f"{label}: {values[key]}" for label, key in fields if key in core_keys and values[key]]
        explanation = "\n\n".join(f"{label}:\n{values[key]}" for label, key in fields if values[key])
        return "\n\n".join(core[:4]) or values["notes"], "", explanation
    explanation = clean_text(raw.get("explanation"))
    if not explanation:
        marked = re.sub(r"\[ATTACH\s+[^\]]*?\](.*?)\[/ATTACH\]", r"\1", str(raw.get("explanationHtml") or ""), flags=re.I | re.S)
        explanation = clean_text(marked)
    return clean_text(raw.get("coreText")), clean_text(raw.get("ruling")), explanation


def normalize_document(raw: Mapping[str, object], discovered: Mapping[str, object], source_base_url: str) -> Document:
    evidencna = clean_text(raw.get("registryNumber"))
    unid = clean_text(raw.get("unid"))
    internal = unid.rsplit("_", 1)[-1]
    if evidencna != clean_text(discovered.get("registryNumber")) or unid != clean_text(discovered.get("unid")) or not internal.isdigit():
        raise IngestError("source detail identity conflicts with discovery")
    database = clean_text(raw.get("sourceDb"))
    if database not in {"doc", "seu", "art", "inj"}:
        raise IngestError("unsupported source document class")
    jedro, izrek, obrazlozitev = _sections(raw, database)
    if not any((jedro, izrek, obrazlozitev)):
        raise IngestError("source document has no substantive text")
    parsed = urlsplit(source_base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    def full_url(value: object) -> str:
        text = clean_text(value)
        return text if text.startswith(("http://", "https://")) else origin + "/" + text.lstrip("/") if text else ""

    html_value = str(raw.get("explanationHtml") or "")
    attachment_ids = re.findall(r"\[ATTACH\s+[^\]]*?id=[\"']([^\"']+)", html_value, re.I)
    opinion_count = min(
        len(set(attachment_ids)), len(re.findall(r"(?:ODKLONILNO\s+|PRITRDILNO\s+)?LOČENO\s+MNENJE\b", clean_text(html_value), re.I))
    )
    title = clean_text(raw.get("ordinalNumber"))
    if database == "art" and not title:
        title = clean_text(raw.get("content")).split("\n", 1)[0]
    if database == "inj":
        compensation = clean_text(raw.get("compensation"))
        title = f"Odškodninski primer - {compensation}" if compensation else "Odškodninski primer"
    structured = raw.get("connection3")
    metadata: dict[str, object] = {
        "source": "sodnapraksa.si",
        "evidencna_stevilka": evidencna,
        "unid": unid,
        "internal_id": internal,
        "database": database,
        "title": title,
        "author": clean_text(raw.get("source")) if database == "art" else "",
        "publication": clean_text(raw.get("publication")) if database == "art" else "",
        "language": clean_text(raw.get("language")) if database == "art" else "",
        "country": clean_text(raw.get("country")) if database == "art" else "",
        "url": full_url(raw.get("documentUrl")),
        "feed_url": clean_text(raw.get("feedUrl")),
        "sodisce": clean_text(raw.get("court")),
        "court_id": clean_text(raw.get("courtId")),
        "oddelek": clean_text(raw.get("department")),
        "ecli": clean_text(raw.get("ecli")),
        "datum_odlocbe": _timestamp(raw.get("sessionDate")),
        "datum_nastanka": _timestamp(raw.get("dateCreated")),
        "datum_zadnje_spremembe": _timestamp(raw.get("dateModified")),
        "podrocje": _strings(raw.get("areas")),
        "institut": _strings(raw.get("institutes")),
        "is_key_decision": _boolean(raw.get("isKeyDecisions")),
        "is_on_key_decision_list": _boolean(raw.get("isOnKeyDecisionList")),
        "zveza": clean_text(raw.get("connection")),
        "zveza_refs": _strings(raw.get("connection2")),
        "zveza_structured": structured if isinstance(structured, list) else [],
        "attachment_ids": attachment_ids,
        "locena_mnenja_count": opinion_count,
        "similar_docs": raw.get("similarDocs") if isinstance(raw.get("similarDocs"), (dict, list)) else [],
        "relations": raw.get("relations") if isinstance(raw.get("relations"), (dict, list)) else [],
    }
    content = {"jedro": jedro, "izrek": izrek, "obrazlozitev": obrazlozitev}
    content_hash = _hash(content)
    metadata_hash = _hash(metadata)
    normalized = {**metadata, **content, "content_hash": content_hash, "metadata_hash": metadata_hash}
    return Document(evidencna, unid, internal, database, content_hash, metadata_hash, normalized, dict(raw))


def _chunk_text(value: object) -> str:
    lines = [line.strip() for line in str(value or "").replace("\r", "").splitlines()]
    return "\n".join(line for line in lines if line).strip()


def split_chunks(text: str, maximum: int = 3500, overlap: int = 400) -> list[str]:
    """Preserve the donor's deterministic adaptive 3,500/400 boundaries."""

    text = _chunk_text(text)
    if not text:
        return []
    minimum = int(maximum * 0.55)

    def sentence_ends(value: str) -> list[int]:
        ends = [match.end() for match in re.finditer(r'(?<=[.!?])\s+(?=[A-ZČŠŽĐĆ0-9»„"])', value)]
        ends.extend(match.end() for match in re.finditer(r"(?<=\.)\n+", value))
        return ends

    def split_long(value: str) -> list[str]:
        parts: list[str] = []
        cursor = 0
        while cursor < len(value):
            remaining = _chunk_text(value[cursor:])
            if len(remaining) <= maximum:
                if remaining:
                    parts.append(remaining)
                break
            cut = remaining.rfind("\n\n", 0, maximum + 1)
            if cut < minimum:
                candidates = [position for position in sentence_ends(remaining[: maximum + 1]) if position >= minimum]
                cut = candidates[-1] if candidates else -1
            if cut < minimum:
                punctuation = [
                    position + 1
                    for position, char in enumerate(remaining[: maximum + 1])
                    if char in ";:" and position + 1 >= minimum and (position + 1 == len(remaining) or remaining[position + 1].isspace())
                ]
                cut = punctuation[-1] if punctuation else remaining.rfind(" ", 0, maximum + 1)
            if cut < minimum:
                cut = maximum
            if piece := _chunk_text(remaining[:cut]):
                parts.append(piece)
            window = max(0, cut - overlap)
            start = remaining.rfind("\n\n", window, cut)
            if start > window:
                start += 2
            else:
                candidates = [position for position in sentence_ends(remaining[:cut]) if position >= window]
                start = candidates[-1] if candidates else cut
            cursor += start if 0 < start < len(remaining) else len(value)
        return parts

    chunks: list[str] = []
    current: list[str] = []

    def flush() -> None:
        nonlocal current
        chunks.extend(split_long(_chunk_text("\n\n".join(current))))
        current = []

    for paragraph in (_chunk_text(part) for part in text.split("\n")):
        if not paragraph:
            continue
        if len(paragraph) > maximum:
            flush()
            chunks.extend(split_long(paragraph))
        elif not current or len(_chunk_text("\n\n".join([*current, paragraph]))) <= maximum:
            current.append(paragraph)
        else:
            flush()
            previous = chunks[-1].split("\n\n")[-1] if chunks else ""
            current = ([previous] if previous and len(previous) <= overlap else []) + [paragraph]
    flush()
    return list(dict.fromkeys(chunk for chunk in chunks if chunk))


def build_chunks(document: Document, model: str, dimensions: int) -> list[Chunk]:
    normalized = document.normalized
    payload_keys = (  # noqa: SIM905
        "source database evidencna_stevilka unid internal_id title sodisce court_id oddelek ecli datum_odlocbe "
        "datum_nastanka datum_zadnje_spremembe podrocje institut zveza zveza_refs is_key_decision is_on_key_decision_list url"
    ).split()
    base: dict[str, object] = {key: normalized.get(key, "") for key in payload_keys}
    base.update(
        {
            "content_hash": document.content_hash,
            "metadata_hash": document.metadata_hash,
            "has_locena_mnenja": bool(normalized.get("locena_mnenja_count")),
            "locena_mnenja_count": int(normalized.get("locena_mnenja_count") or 0),
            "status": "STORED",
            "deleted": False,
            "is_active": True,
            "embedding_model": model,
            "embedding_dimensions": dimensions,
        }
    )
    chunks: list[Chunk] = []
    for section in ("jedro", "izrek", "obrazlozitev"):
        for index, text in enumerate(split_chunks(str(normalized.get(section) or ""))):
            chunk_hash = hashlib.sha256(_chunk_text(text).encode()).hexdigest()
            raw_id = f"{document.evidencna}|{section}|{index}|{chunk_hash}"
            chunk_id = hashlib.sha256(raw_id.encode()).hexdigest()
            point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "lexai:sodnapraksa:" + chunk_id))
            payload = {
                **base,
                "section": section,
                "chunk_index": index,
                "chunk_id": chunk_id,
                "chunk_hash": chunk_hash,
                "text": text,
            }
            chunks.append(Chunk(point_id, text, payload))
    if not chunks:
        raise IngestError("document produced no chunks")
    return chunks
