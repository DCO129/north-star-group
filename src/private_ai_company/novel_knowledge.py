"""Novel KnowledgeAdapter spike (P0-2).

Local, file-backed, provider-neutral knowledge retrieval for the novel
production line. It assembles grounded pre-draft chapter context only:
it never writes chapter prose, calls a model, touches the network, or reads
a Secret.

Frozen contract:
  contracts/20260724-p0-2-novel-knowledge-adapter-contract.md
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

STORY_ANCHOR = "story-anchor"
CRAFT_TECHNIQUE = "craft-technique"
KNOWN_KINDS = (STORY_ANCHOR, CRAFT_TECHNIQUE)
KIND_PRIORITY = {STORY_ANCHOR: 0, CRAFT_TECHNIQUE: 1}
ALLOWED_RIGHTS = ("owned", "licensed", "public-domain")
INJECTION_TARGET = "chapter-draft-context"

REQUEST_SCHEMA = "novel-chapter-context-request/v1"
QUERY_SCHEMA = "novel-knowledge-query/v1"
SNIPPET_SCHEMA = "novel-knowledge-snippet/v1"
RESULT_SCHEMA = "novel-knowledge-result/v1"
CITATION_SCHEMA = "knowledge-citation/v1"
CONTEXT_SCHEMA = "novel-chapter-knowledge-context/v1"
PACKAGE_SCHEMA = "novel-chapter-context-package/v1"
WORKFLOW_NAME = "novel.chapter-knowledge-context/v0"

# Stable blocking codes (contract section 10)
CODE_CHAPTER_REQUEST_INVALID = "chapter-request-invalid"
CODE_STORY_BIBLE_NOT_READY = "story-bible-not-ready"
CODE_ADAPTER_UNAVAILABLE = "knowledge-adapter-unavailable"
CODE_CATALOG_INVALID = "knowledge-catalog-invalid"
CODE_SOURCE_PATH_INVALID = "knowledge-source-path-invalid"
CODE_SOURCE_UNVERSIONED = "knowledge-source-unversioned"
CODE_SOURCE_RIGHTS_UNSAFE = "knowledge-source-rights-unsafe"
CODE_SOURCE_HASH_MISMATCH = "knowledge-source-hash-mismatch"
CODE_NO_STORY_ANCHOR = "knowledge-no-story-anchor"
CODE_NO_CRAFT_TECHNIQUE = "knowledge-no-craft-technique"
CODE_BUDGET_INSUFFICIENT = "knowledge-budget-insufficient"
CODE_RESULT_INVALID = "knowledge-result-invalid"

PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def estimated_tokens(text: str) -> int:
    """Deterministic conservative token estimate (contract section 8)."""
    return max(1, math.ceil(len(text.encode("utf-8")) / 3))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _clean_str(value: Any, field_name: str, *, required: bool = False, max_length: int = 4000) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise ValueError(f"{field_name} is required")
    if len(result) > max_length:
        raise ValueError(f"{field_name} must be at most {max_length} characters")
    return result


def _clean_str_list(value: Any, field_name: str, *, min_items: int = 0, max_items: int = 12, max_length: int = 200) -> list[str]:
    if value is None:
        value = []
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be an array")
    out: list[str] = []
    for raw in value:
        item = _clean_str(raw, field_name, max_length=max_length)
        if item and item not in out:
            out.append(item)
    if len(out) < min_items:
        raise ValueError(f"{field_name} must contain at least {min_items} items")
    if len(out) > max_items:
        raise ValueError(f"{field_name} must contain at most {max_items} items")
    return out


def _safe_project_id(value: Any) -> str:
    candidate = _clean_str(value, "project_id", max_length=64).lower()
    if not PROJECT_ID_RE.fullmatch(candidate):
        raise ValueError("project_id must match ^[a-z0-9][a-z0-9-]{2,63}$")
    return candidate


def _normalize_terms(terms: list[str]) -> list[str]:
    out: list[str] = []
    for term in terms:
        norm = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", str(term).lower()).strip()
        if norm:
            out.append(norm)
    return out


def _match_score(terms: list[str], snippet: "KnowledgeSnippet") -> int:
    haystack = " ".join([snippet.title, " ".join(snippet.tags), snippet.text]).lower()
    score = 0
    for term in terms:
        if term and term in haystack:
            score += 1
    return score


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class KnowledgeAdapterError(Exception):
    """Raised by the adapter when knowledge cannot be safely provided."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


# --------------------------------------------------------------------------
# Schemas (frozen contract section 5)
# --------------------------------------------------------------------------

@dataclass
class ChapterContextRequest:
    schema_version: str = REQUEST_SCHEMA
    project_id: str = ""
    chapter_number: int = 0
    objective: str = ""
    anchor_terms: list[str] = field(default_factory=list)
    technique_tags: list[str] = field(default_factory=list)
    token_budget: int = 0
    job_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "chapter_number": self.chapter_number,
            "objective": self.objective,
            "anchor_terms": list(self.anchor_terms),
            "technique_tags": list(self.technique_tags),
            "token_budget": self.token_budget,
        }
        if self.job_id is not None:
            data["job_id"] = self.job_id
        return data


@dataclass
class KnowledgeQuery:
    schema_version: str = QUERY_SCHEMA
    project_id: str = ""
    chapter_number: int = 0
    objective: str = ""
    anchor_terms: list[str] = field(default_factory=list)
    technique_tags: list[str] = field(default_factory=list)
    required_kinds: list[str] = field(default_factory=lambda: [STORY_ANCHOR, CRAFT_TECHNIQUE])
    max_results_per_kind: int = 8
    token_budget: int = 0
    query_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "chapter_number": self.chapter_number,
            "objective": self.objective,
            "anchor_terms": list(self.anchor_terms),
            "technique_tags": list(self.technique_tags),
            "required_kinds": list(self.required_kinds),
            "max_results_per_kind": self.max_results_per_kind,
            "token_budget": self.token_budget,
            "query_sha256": self.query_sha256,
        }


@dataclass
class KnowledgeSnippet:
    schema_version: str = SNIPPET_SCHEMA
    snippet_id: str = ""
    kind: str = STORY_ANCHOR
    title: str = ""
    text: str = ""
    tags: list[str] = field(default_factory=list)
    source_ref: str = ""
    source_version: str = ""
    source_sha256: str = ""
    rights_status: str = ""
    score_milli: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SNIPPET_SCHEMA,
            "snippet_id": self.snippet_id,
            "kind": self.kind,
            "title": self.title,
            "text": self.text,
            "tags": list(self.tags),
            "source_ref": self.source_ref,
            "source_version": self.source_version,
            "source_sha256": self.source_sha256,
            "rights_status": self.rights_status,
            "score_milli": self.score_milli,
        }


@dataclass
class KnowledgeResult:
    schema_version: str = RESULT_SCHEMA
    status: str = "blocked"
    adapter: dict[str, str] = field(default_factory=dict)
    query_sha256: str = ""
    snippets: list[KnowledgeSnippet] = field(default_factory=list)
    blocking_codes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "adapter": dict(self.adapter),
            "query_sha256": self.query_sha256,
            "snippets": [s.to_dict() for s in self.snippets],
            "blocking_codes": list(self.blocking_codes),
        }


@dataclass
class KnowledgeCitation:
    schema_version: str = CITATION_SCHEMA
    snippet_id: str = ""
    kind: str = ""
    source_ref: str = ""
    source_version: str = ""
    source_sha256: str = ""
    injected_text_sha256: str = ""
    estimated_tokens: int = 0
    use_position: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CITATION_SCHEMA,
            "snippet_id": self.snippet_id,
            "kind": self.kind,
            "source_ref": self.source_ref,
            "source_version": self.source_version,
            "source_sha256": self.source_sha256,
            "injected_text_sha256": self.injected_text_sha256,
            "estimated_tokens": self.estimated_tokens,
            "use_position": self.use_position,
        }


@dataclass
class ChapterKnowledgeContext:
    schema_version: str = CONTEXT_SCHEMA
    status: str = "blocked"
    project_id: str = ""
    job_id: str = ""
    chapter_number: int = 0
    objective: str = ""
    query_sha256: str = ""
    adapter: dict[str, str] = field(default_factory=dict)
    token_budget: int = 0
    estimated_tokens: int = 0
    trimmed: bool = False
    selected_snippets: list[KnowledgeSnippet] = field(default_factory=list)
    omitted_snippet_ids: list[str] = field(default_factory=list)
    citations: list[KnowledgeCitation] = field(default_factory=list)
    injection_target: str = INJECTION_TARGET
    blocking_codes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "project_id": self.project_id,
            "job_id": self.job_id,
            "chapter_number": self.chapter_number,
            "objective": self.objective,
            "query_sha256": self.query_sha256,
            "adapter": dict(self.adapter),
            "token_budget": self.token_budget,
            "estimated_tokens": self.estimated_tokens,
            "trimmed": self.trimmed,
            "selected_snippets": [s.to_dict() for s in self.selected_snippets],
            "omitted_snippet_ids": list(self.omitted_snippet_ids),
            "citations": [c.to_dict() for c in self.citations],
            "injection_target": self.injection_target,
            "blocking_codes": list(self.blocking_codes),
        }


@dataclass
class ChapterContextPackage:
    schema_version: str = PACKAGE_SCHEMA
    status: str = "blocked"
    job_id: str = ""
    project_id: str = ""
    chapter_number: int = 0
    writes_novel_prose: bool = False
    external_knowledge_accessed: bool = False
    artifact_paths: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "job_id": self.job_id,
            "project_id": self.project_id,
            "chapter_number": self.chapter_number,
            "writes_novel_prose": self.writes_novel_prose,
            "external_knowledge_accessed": self.external_knowledge_accessed,
            "artifact_paths": dict(self.artifact_paths),
        }


# --------------------------------------------------------------------------
# Request validation
# --------------------------------------------------------------------------

def validate_chapter_context_request(data: Any) -> ChapterContextRequest:
    """Validate and normalize a novel-chapter-context-request/v1 payload.

    Unknown fields are rejected (contract section 5.1).
    """
    if not isinstance(data, dict):
        raise ValueError("request body must be a JSON object")
    allowed = {
        "schema_version", "chapter_number", "objective",
        "anchor_terms", "technique_tags", "token_budget", "job_id",
    }
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(f"unknown fields: {', '.join(unknown)}")
    if data.get("schema_version") != REQUEST_SCHEMA:
        raise ValueError(f"schema_version must be {REQUEST_SCHEMA}")
    chapter_number = data.get("chapter_number")
    if not isinstance(chapter_number, int) or isinstance(chapter_number, bool) or chapter_number <= 0:
        raise ValueError("chapter_number must be a positive integer")
    objective = _clean_str(data.get("objective"), "objective", required=True, max_length=4000)
    anchor_terms = _clean_str_list(data.get("anchor_terms"), "anchor_terms", min_items=1, max_items=12, max_length=200)
    technique_tags = _clean_str_list(data.get("technique_tags"), "technique_tags", min_items=1, max_items=12, max_length=200)
    token_budget = data.get("token_budget")
    if not isinstance(token_budget, int) or isinstance(token_budget, bool) or not (128 <= token_budget <= 4096):
        raise ValueError("token_budget must be an integer between 128 and 4096")
    job_id = data.get("job_id")
    if job_id is not None:
        job_id = _clean_str(job_id, "job_id", max_length=128)
    return ChapterContextRequest(
        schema_version=REQUEST_SCHEMA,
        project_id="",
        chapter_number=chapter_number,
        objective=objective,
        anchor_terms=anchor_terms,
        technique_tags=technique_tags,
        token_budget=token_budget,
        job_id=job_id,
    )


# --------------------------------------------------------------------------
# Query / context / package builders
# --------------------------------------------------------------------------

def build_knowledge_query(request: ChapterContextRequest, job_id: str) -> KnowledgeQuery:
    query = KnowledgeQuery(
        project_id=request.project_id,
        chapter_number=request.chapter_number,
        objective=request.objective,
        anchor_terms=list(request.anchor_terms),
        technique_tags=list(request.technique_tags),
        required_kinds=[STORY_ANCHOR, CRAFT_TECHNIQUE],
        max_results_per_kind=8,
        token_budget=request.token_budget,
    )
    query.query_sha256 = sha256_text(canonical_json(query.to_dict()))
    return query


def build_citation(snippet: KnowledgeSnippet, index: int) -> KnowledgeCitation:
    return KnowledgeCitation(
        snippet_id=snippet.snippet_id,
        kind=snippet.kind,
        source_ref=snippet.source_ref,
        source_version=snippet.source_version,
        source_sha256=snippet.source_sha256,
        injected_text_sha256=sha256_text(snippet.text),
        estimated_tokens=estimated_tokens(snippet.text),
        use_position=f"{INJECTION_TARGET}#{index + 1}",
    )


def build_chapter_context(
    *,
    request: ChapterContextRequest,
    query: KnowledgeQuery,
    result: KnowledgeResult,
    adapter_id: str,
    adapter_version: str,
    job_id: str,
) -> ChapterKnowledgeContext:
    """Deterministic token-budget trimming (contract section 8)."""
    snippets = list(result.snippets)
    story = [s for s in snippets if s.kind == STORY_ANCHOR]
    craft = [s for s in snippets if s.kind == CRAFT_TECHNIQUE]

    if not story:
        return ChapterKnowledgeContext(
            status="blocked", project_id=request.project_id, job_id=job_id,
            chapter_number=request.chapter_number, objective=request.objective,
            query_sha256=query.query_sha256,
            adapter={"id": adapter_id, "version": adapter_version},
            token_budget=request.token_budget, blocking_codes=[CODE_NO_STORY_ANCHOR],
        )
    if not craft:
        return ChapterKnowledgeContext(
            status="blocked", project_id=request.project_id, job_id=job_id,
            chapter_number=request.chapter_number, objective=request.objective,
            query_sha256=query.query_sha256,
            adapter={"id": adapter_id, "version": adapter_version},
            token_budget=request.token_budget, blocking_codes=[CODE_NO_CRAFT_TECHNIQUE],
        )

    budget = request.token_budget
    mandatory = [story[0], craft[0]]
    selected = list(mandatory)
    total = sum(estimated_tokens(s.text) for s in selected)
    if total > budget:
        return ChapterKnowledgeContext(
            status="blocked", project_id=request.project_id, job_id=job_id,
            chapter_number=request.chapter_number, objective=request.objective,
            query_sha256=query.query_sha256,
            adapter={"id": adapter_id, "version": adapter_version},
            token_budget=budget, estimated_tokens=total,
            selected_snippets=selected, omitted_snippet_ids=[],
            blocking_codes=[CODE_BUDGET_INSUFFICIENT],
        )

    rest = [s for s in snippets if s not in mandatory]
    for snippet in rest:
        cost = estimated_tokens(snippet.text)
        if total + cost <= budget:
            selected.append(snippet)
            total += cost

    omitted = [s.snippet_id for s in snippets if s not in selected]
    citations = [build_citation(s, idx) for idx, s in enumerate(selected)]
    return ChapterKnowledgeContext(
        status="ready", project_id=request.project_id, job_id=job_id,
        chapter_number=request.chapter_number, objective=request.objective,
        query_sha256=query.query_sha256,
        adapter={"id": adapter_id, "version": adapter_version},
        token_budget=budget, estimated_tokens=total,
        trimmed=bool(omitted), selected_snippets=selected,
        omitted_snippet_ids=omitted, citations=citations,
        blocking_codes=[],
    )


def build_chapter_context_package(
    *,
    request: ChapterContextRequest,
    context: ChapterKnowledgeContext,
    job_id: str,
    artifact_paths: dict[str, str],
) -> ChapterContextPackage:
    return ChapterContextPackage(
        status=context.status,
        job_id=job_id,
        project_id=request.project_id,
        chapter_number=request.chapter_number,
        writes_novel_prose=False,
        external_knowledge_accessed=False,
        artifact_paths=artifact_paths,
    )


# --------------------------------------------------------------------------
# Adapter
# --------------------------------------------------------------------------

class KnowledgeAdapter(Protocol):
    def query(self, query: KnowledgeQuery) -> KnowledgeResult: ...


class LocalFileKnowledgeAdapter:
    """File-backed KnowledgeAdapter spike.

    Reads project Story Bible anchors and a reviewed shared craft catalog.
    Rejects absolute paths, traversal, duplicate IDs, missing files, unsupported
    schemas, missing versions, unsafe rights, and content hash mismatches.
    """

    ADAPTER_ID = "local-file-knowledge-adapter"
    ADAPTER_VERSION = "v1"

    def __init__(self, group_root: Path) -> None:
        self.group_root = Path(group_root).resolve()
        self.shared_novel_root = (self.group_root / "shared" / "knowledge" / "novel").resolve()
        self._catalog = self._load_catalog()

    # -- catalog loading & validation ------------------------------------

    def _load_catalog(self) -> list[dict[str, Any]]:
        path = self.shared_novel_root / "catalog.json"
        if not path.is_file():
            raise KnowledgeAdapterError(CODE_CATALOG_INVALID, "shared knowledge catalog not found")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise KnowledgeAdapterError(CODE_CATALOG_INVALID, f"malformed catalog: {exc}")
        if not isinstance(data, dict) or not isinstance(data.get("assets"), list):
            raise KnowledgeAdapterError(CODE_CATALOG_INVALID, "catalog missing assets list")
        assets: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for entry in data["assets"]:
            if not isinstance(entry, dict):
                raise KnowledgeAdapterError(CODE_CATALOG_INVALID, "catalog entry must be an object")
            asset_id = entry.get("asset_id")
            if not isinstance(asset_id, str) or not asset_id.strip():
                raise KnowledgeAdapterError(CODE_CATALOG_INVALID, "catalog entry missing asset_id")
            if asset_id in seen_ids:
                raise KnowledgeAdapterError(CODE_CATALOG_INVALID, f"duplicate asset id: {asset_id}")
            seen_ids.add(asset_id)
            asset_path = entry.get("asset_path")
            if not isinstance(asset_path, str) or not asset_path.strip():
                raise KnowledgeAdapterError(CODE_SOURCE_PATH_INVALID, f"asset {asset_id} missing asset_path")
            if "\\" in asset_path or ".." in asset_path or asset_path.startswith(("/", "\\")):
                raise KnowledgeAdapterError(CODE_SOURCE_PATH_INVALID, f"asset {asset_id} absolute or traversal path")
            resolved = (self.group_root / asset_path).resolve()
            try:
                resolved.relative_to(self.shared_novel_root)
            except ValueError:
                raise KnowledgeAdapterError(CODE_SOURCE_PATH_INVALID, f"asset {asset_id} escapes shared root")
            version = entry.get("version")
            if not isinstance(version, str) or not version.strip():
                raise KnowledgeAdapterError(CODE_SOURCE_UNVERSIONED, f"asset {asset_id} missing version")
            rights = entry.get("rights_status")
            if rights not in ALLOWED_RIGHTS:
                raise KnowledgeAdapterError(CODE_SOURCE_RIGHTS_UNSAFE, f"asset {asset_id} unsafe rights: {rights}")
            if not resolved.is_file():
                raise KnowledgeAdapterError(CODE_SOURCE_PATH_INVALID, f"asset {asset_id} file not found")
            try:
                asset_data = json.loads(resolved.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                raise KnowledgeAdapterError(CODE_CATALOG_INVALID, f"asset {asset_id} malformed: {exc}")
            if not isinstance(asset_data, dict):
                raise KnowledgeAdapterError(CODE_CATALOG_INVALID, f"asset {asset_id} not an object")
            asset_version = asset_data.get("version")
            if not isinstance(asset_version, str) or not asset_version.strip():
                raise KnowledgeAdapterError(CODE_SOURCE_UNVERSIONED, f"asset {asset_id} missing version")
            text = asset_data.get("text")
            if not isinstance(text, str) or not text.strip():
                raise KnowledgeAdapterError(CODE_CATALOG_INVALID, f"asset {asset_id} missing text")
            content_sha256 = asset_data.get("content_sha256")
            if not isinstance(content_sha256, str) or sha256_text(text) != content_sha256:
                raise KnowledgeAdapterError(CODE_SOURCE_HASH_MISMATCH, f"asset {asset_id} content hash mismatch")
            assets.append({
                "asset_id": asset_id,
                "version": asset_version,
                "source_ref": entry.get("source_ref") or f"group://shared/knowledge/novel/{asset_id}.json",
                "rights_status": rights,
                "title": asset_data.get("title", asset_id),
                "tags": list(asset_data.get("tags", [])),
                "text": text,
                "content_sha256": content_sha256,
            })
        return assets

    def _load_story_bible(self, project_id: str) -> tuple[dict[str, Any], bytes]:
        path = (
            self.group_root / "runtime" / "novel-studio" / "projects"
            / project_id / "story-bible" / "current.json"
        )
        if not path.is_file():
            raise KnowledgeAdapterError(CODE_STORY_BIBLE_NOT_READY, "story bible current state not found")
        raw = path.read_bytes()
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise KnowledgeAdapterError(CODE_STORY_BIBLE_NOT_READY, f"malformed story bible: {exc}")
        if not isinstance(data, dict) or data.get("status") != "assembled":
            raise KnowledgeAdapterError(CODE_STORY_BIBLE_NOT_READY, "story bible not assembled")
        return data, raw

    # -- candidate building ----------------------------------------------

    def _make_story_snippet(
        self, project_id: str, sb_run_id: str, sb_sha: str,
        snippet_id: str, title: str, text: str, tags: list[str],
    ) -> KnowledgeSnippet:
        return KnowledgeSnippet(
            snippet_id=snippet_id, kind=STORY_ANCHOR, title=title, text=text,
            tags=list(tags), source_ref=f"project://{project_id}/story-bible/current",
            source_version=sb_run_id, source_sha256=sb_sha, rights_status="owned",
            score_milli=0,
        )

    def _build_story_anchor_candidates(
        self, project_id: str, story_bible: dict[str, Any], sb_run_id: str, sb_sha: str
    ) -> list[KnowledgeSnippet]:
        candidates: list[KnowledgeSnippet] = []
        nc = story_bible.get("narrative_contract", {}) or {}
        single_fields = [
            ("core_conflict", "Core conflict"),
            ("protagonist_goal", "Protagonist goal"),
            ("antagonistic_force", "Antagonistic force"),
            ("ending_direction", "Ending direction"),
        ]
        for key, label in single_fields:
            text = nc.get(key)
            if isinstance(text, str) and text.strip():
                candidates.append(self._make_story_snippet(
                    project_id, sb_run_id, sb_sha, f"story-anchor:{key}", label, text,
                    ["story-anchor", key],
                ))
        for i, rule in enumerate(nc.get("world_rules", []) or []):
            if isinstance(rule, str) and rule.strip():
                candidates.append(self._make_story_snippet(
                    project_id, sb_run_id, sb_sha, f"story-anchor:world-rule-{i}",
                    f"World rule {i + 1}", rule, ["story-anchor", "world-rule"],
                ))
        cb = story_bible.get("continuity_baseline", {}) or {}
        for i, constraint in enumerate(cb.get("character_constraints", []) or []):
            if isinstance(constraint, str) and constraint.strip():
                candidates.append(self._make_story_snippet(
                    project_id, sb_run_id, sb_sha, f"story-anchor:character-constraint-{i}",
                    f"Character constraint {i + 1}", constraint, ["story-anchor", "character-constraint"],
                ))
        for summary in story_bible.get("source_summaries", []) or []:
            if isinstance(summary, dict) and isinstance(summary.get("summary"), str) and summary["summary"].strip():
                mid = summary.get("material_id", "unknown")
                category = summary.get("category", "source")
                candidates.append(self._make_story_snippet(
                    project_id, sb_run_id, sb_sha, f"story-anchor:source-{mid}",
                    f"{category} source summary", summary["summary"],
                    ["story-anchor", "source-summary", str(category)],
                ))
        return candidates

    def _build_craft_candidates(self) -> list[KnowledgeSnippet]:
        out: list[KnowledgeSnippet] = []
        for asset in self._catalog:
            out.append(KnowledgeSnippet(
                snippet_id=f"craft-technique:{asset['asset_id']}",
                kind=CRAFT_TECHNIQUE, title=asset["title"], text=asset["text"],
                tags=["craft-technique", *asset["tags"]],
                source_ref=asset["source_ref"], source_version=asset["version"],
                source_sha256=asset["content_sha256"], rights_status=asset["rights_status"],
                score_milli=0,
            ))
        return out

    def _rank(
        self, candidates: list[KnowledgeSnippet], terms: list[str],
        required_kinds: list[str], max_per_kind: int,
    ) -> list[KnowledgeSnippet]:
        scored = [( _match_score(terms, s), s) for s in candidates]
        scored.sort(key=lambda item: (-item[0], KIND_PRIORITY.get(item[1].kind, 9), item[1].snippet_id))
        per_kind = {kind: 0 for kind in required_kinds}
        out: list[KnowledgeSnippet] = []
        for score, snippet in scored:
            if snippet.kind in per_kind and per_kind[snippet.kind] < max_per_kind:
                snippet.score_milli = int(score * 1000)
                out.append(snippet)
                per_kind[snippet.kind] += 1
        return out

    # -- query -----------------------------------------------------------

    def query(self, query: KnowledgeQuery) -> KnowledgeResult:
        if not isinstance(query, KnowledgeQuery):
            raise KnowledgeAdapterError(CODE_RESULT_INVALID, "query must be a KnowledgeQuery")
        project_id = _safe_project_id(query.project_id)
        story_bible, sb_bytes = self._load_story_bible(project_id)
        sb_run_id = str(story_bible.get("run_id", ""))
        sb_sha = sha256_text(sb_bytes.decode("utf-8"))

        candidates = self._build_story_anchor_candidates(project_id, story_bible, sb_run_id, sb_sha)
        candidates += self._build_craft_candidates()

        terms = _normalize_terms(query.anchor_terms + query.technique_tags)
        required = query.required_kinds or [STORY_ANCHOR, CRAFT_TECHNIQUE]
        ranked = self._rank(candidates, terms, required, query.max_results_per_kind)

        missing = [kind for kind in required if not any(s.kind == kind for s in ranked)]
        if missing:
            code = CODE_NO_STORY_ANCHOR if STORY_ANCHOR in missing else CODE_NO_CRAFT_TECHNIQUE
            return KnowledgeResult(
                status="blocked",
                adapter={"id": self.ADAPTER_ID, "version": self.ADAPTER_VERSION},
                query_sha256=query.query_sha256, snippets=[], blocking_codes=[code],
            )
        return KnowledgeResult(
            status="ready",
            adapter={"id": self.ADAPTER_ID, "version": self.ADAPTER_VERSION},
            query_sha256=query.query_sha256, snippets=ranked, blocking_codes=[],
        )
