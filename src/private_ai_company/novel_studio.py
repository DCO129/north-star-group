import asyncio
import json
import re
import threading
import types
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import JSONResponse

from .novel_knowledge import (
    CODE_STORY_BIBLE_NOT_READY,
    ChapterKnowledgeContext,
    KnowledgeAdapterError,
    KnowledgeCitation,
    KnowledgeQuery,
    KnowledgeResult,
    KnowledgeSnippet,
    LocalFileKnowledgeAdapter,
    WORKFLOW_NAME,
    build_chapter_context,
    build_chapter_context_package,
    build_knowledge_query,
    validate_chapter_context_request,
)
from .providers import ProviderError

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
ALLOWED_PLATFORMS = {"fanqie", "qidian", "internal", "undecided"}
ALLOWED_MATERIAL_CATEGORIES = {"world", "character", "plot", "style", "market", "reference"}
ALLOWED_SOURCE_KINDS = {"registered-slot", "distilled-summary", "external-reference", "manual-note"}
ALLOWED_RIGHTS_STATUS = {"unknown", "owned", "licensed", "public-domain", "reference-only"}
ALLOWED_REVIEW_DECISIONS = {"approved", "rejected"}
STORY_BIBLE_STRING_FIELDS = {"core_conflict", "protagonist_goal", "antagonistic_force", "ending_direction", "platform_positioning"}
STORY_BIBLE_LIST_FIELDS = {"world_rules", "character_constraints", "style_constraints", "forbidden_elements"}


class NovelGateError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(SHANGHAI_TZ).isoformat()


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def safe_project_id(value: str) -> str:
    candidate = value.strip().lower()
    if not PROJECT_ID_RE.fullmatch(candidate):
        raise ValueError("project_id must match ^[a-z0-9][a-z0-9-]{2,63}$")
    return candidate


def require_object(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    return payload


def clean_string(value: Any, field: str, *, required: bool = False, max_length: int = 4000) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise ValueError(f"{field} is required")
    if len(result) > max_length:
        raise ValueError(f"{field} must be at most {max_length} characters")
    return result


def clean_string_list(value: Any, field: str, *, max_items: int = 24, max_length: int = 500) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    result: list[str] = []
    for raw in value:
        item = clean_string(raw, field, max_length=max_length)
        if item and item not in result:
            result.append(item)
    if len(result) > max_items:
        raise ValueError(f"{field} must contain at most {max_items} items")
    return result


class NovelStudio:
    def __init__(self, group_root: Path) -> None:
        self.group_root = group_root.resolve()
        self.root = self.group_root / "runtime" / "novel-studio"
        self.projects_root = self.root / "projects"
        self.projects_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _project_root(self, project_id: str) -> Path:
        return self.projects_root / safe_project_id(project_id)

    @staticmethod
    def _normalize_manifest(item: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(item)
        normalized.setdefault("manifest_version", "v1")
        normalized.setdefault("source_kind", "registered-slot")
        normalized.setdefault("source_ref", "")
        normalized.setdefault("version", "unversioned")
        normalized.setdefault("rights_status", "unknown")
        normalized.setdefault("summary", "")
        normalized.setdefault("content_sha256", "")
        normalized.setdefault("tags", [])
        normalized.setdefault("content_loaded", False)
        normalized.setdefault("status", "pending-review")
        normalized.setdefault("approved_for_story_bible", False)
        normalized.setdefault("review", None)
        return normalized

    def _catalog(self, root: Path) -> dict[str, Any]:
        raw = read_json(root / "materials" / "catalog.json", {"items": []})
        items = raw.get("items", []) if isinstance(raw, dict) else []
        return {"schema_version": "novel-material-manifest-catalog/v1", "items": [self._normalize_manifest(item) for item in items if isinstance(item, dict)]}

    def _project_view(self, project: dict[str, Any]) -> dict[str, Any]:
        value = dict(project)
        root = self._project_root(value["project_id"])
        catalog = self._catalog(root)
        approved = [item for item in catalog["items"] if item.get("approved_for_story_bible")]
        value.setdefault("story_bible_run_count", 0)
        value["material_count"] = len(catalog["items"])
        value["approved_material_count"] = len(approved)
        value["materials"] = catalog["items"]
        value["story_bible_seed"] = read_json(root / "story-bible" / "seed.json")
        value["story_bible"] = read_json(root / "story-bible" / "current.json")
        value["latest_run"] = self._latest_run(value["project_id"])
        value["latest_bootstrap_run"] = self._latest_run(value["project_id"], workflow="novel.project-bootstrap/v0")
        value["latest_story_bible_run"] = self._latest_run(value["project_id"], workflow="novel.story-bible-assembly/v0")
        return value

    def list_projects(self) -> list[dict[str, Any]]:
        projects = []
        for path in sorted(self.projects_root.glob("*/project.json")):
            value = read_json(path, {})
            if value:
                projects.append(self._project_view(value))
        return sorted(projects, key=lambda item: item.get("updated_at", ""), reverse=True)

    def overview(self) -> dict[str, Any]:
        return {
            "schema_version": "novel-studio/v1",
            "generated_at": now_iso(),
            "product": {
                "name": "Novel Subsidiary Production Studio",
                "runtime": "microsoft-agent-framework-core/1.12.0",
                "workflows": ["novel.project-bootstrap/v0", "novel.story-bible-assembly/v0"],
                "writes_full_novel": False,
                "workbuddy_access": False,
            },
            "projects": self.list_projects(),
            "pipeline_template": self.pipeline_template(),
            "material_contract": {
                "schema_version": "novel-material-manifest/v1",
                "categories": sorted(ALLOWED_MATERIAL_CATEGORIES),
                "source_kinds": sorted(ALLOWED_SOURCE_KINDS),
                "rights_statuses": sorted(ALLOWED_RIGHTS_STATUS),
                "status": "auditable-manifest-ready",
                "content_loaded": False,
                "approval_required": True,
            },
            "story_bible_contract": {
                "schema_version": "novel-story-bible/v0",
                "uses_approved_summaries_only": True,
                "external_knowledge_accessed": False,
                "writes_novel_prose": False,
            },
        }

    def create_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = require_object(payload)
        required = {"project_id", "title", "genre", "platform", "premise"}
        missing = sorted(key for key in required if not str(payload.get(key, "")).strip())
        if missing:
            raise ValueError(f"missing fields: {', '.join(missing)}")
        unknown = sorted(set(payload) - required - {"target_words", "pen_name"})
        if unknown:
            raise ValueError(f"unknown fields: {', '.join(unknown)}")
        project_id = safe_project_id(str(payload["project_id"]))
        platform = str(payload["platform"]).strip().lower()
        if platform not in ALLOWED_PLATFORMS:
            raise ValueError(f"unsupported platform: {platform}")
        target_words = int(payload.get("target_words", 1_200_000))
        if not 50_000 <= target_words <= 10_000_000:
            raise ValueError("target_words must be between 50000 and 10000000")
        root = self._project_root(project_id)
        with self._lock:
            if (root / "project.json").exists():
                raise FileExistsError(project_id)
            for name in ("materials", "story-bible", "story-bible/history", "outlines", "chapter-jobs", "drafts", "reviews", "publish-queue", "runs"):
                (root / name).mkdir(parents=True, exist_ok=True)
            timestamp = now_iso()
            project = {
                "schema_version": "novel-project/v1", "project_id": project_id,
                "title": clean_string(payload["title"], "title", required=True, max_length=200),
                "pen_name": clean_string(payload.get("pen_name"), "pen_name", max_length=120),
                "genre": clean_string(payload["genre"], "genre", required=True, max_length=120),
                "platform": platform,
                "premise": clean_string(payload["premise"], "premise", required=True, max_length=4000),
                "target_words": target_words, "status": "foundation", "created_at": timestamp, "updated_at": timestamp,
                "material_count": 0, "approved_material_count": 0, "run_count": 0, "story_bible_run_count": 0,
            }
            write_json(root / "project.json", project)
            write_json(root / "materials" / "catalog.json", {"schema_version": "novel-material-manifest-catalog/v1", "items": []})
            return project

    def register_material(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload = require_object(payload)
        allowed = {"category", "title", "source_kind", "source_ref", "version", "rights_status", "summary", "content_sha256", "tags"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError(f"unknown fields: {', '.join(unknown)}")
        category = clean_string(payload.get("category"), "category", required=True, max_length=40).lower()
        title = clean_string(payload.get("title"), "title", required=True, max_length=240)
        if category not in ALLOWED_MATERIAL_CATEGORIES:
            raise ValueError("a supported material category is required")
        source_kind = clean_string(payload.get("source_kind", "registered-slot"), "source_kind", max_length=40).lower()
        if source_kind not in ALLOWED_SOURCE_KINDS:
            raise ValueError(f"unsupported source_kind: {source_kind}")
        rights_status = clean_string(payload.get("rights_status", "unknown"), "rights_status", max_length=40).lower()
        if rights_status not in ALLOWED_RIGHTS_STATUS:
            raise ValueError(f"unsupported rights_status: {rights_status}")
        content_sha256 = clean_string(payload.get("content_sha256"), "content_sha256", max_length=64).lower()
        if content_sha256 and not SHA256_RE.fullmatch(content_sha256):
            raise ValueError("content_sha256 must be a lowercase SHA-256 hex digest")
        root = self._project_root(project_id)
        with self._lock:
            project = read_json(root / "project.json")
            if not project:
                raise FileNotFoundError(project_id)
            catalog = self._catalog(root)
            item = {
                "manifest_version": "v1", "material_id": uuid.uuid4().hex[:12], "title": title, "category": category,
                "source_kind": source_kind, "source_ref": clean_string(payload.get("source_ref"), "source_ref", max_length=1000),
                "version": clean_string(payload.get("version", "unversioned"), "version", max_length=128) or "unversioned",
                "rights_status": rights_status, "summary": clean_string(payload.get("summary"), "summary", max_length=4000),
                "content_sha256": content_sha256, "tags": clean_string_list(payload.get("tags"), "tags", max_items=12, max_length=80),
                "content_loaded": False, "status": "pending-review", "approved_for_story_bible": False,
                "review": None, "registered_at": now_iso(),
            }
            catalog["items"].append(item)
            write_json(root / "materials" / "catalog.json", catalog)
            project["schema_version"] = "novel-project/v1"
            project["material_count"] = len(catalog["items"])
            project["approved_material_count"] = sum(1 for value in catalog["items"] if value["approved_for_story_bible"])
            project["updated_at"] = now_iso()
            write_json(root / "project.json", project)
            return item

    def review_material(self, project_id: str, material_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload = require_object(payload)
        unknown = sorted(set(payload) - {"decision", "reviewer", "note"})
        if unknown:
            raise ValueError(f"unknown fields: {', '.join(unknown)}")
        decision = clean_string(payload.get("decision"), "decision", required=True, max_length=32).lower()
        if decision not in ALLOWED_REVIEW_DECISIONS:
            raise ValueError(f"unsupported review decision: {decision}")
        reviewer = clean_string(payload.get("reviewer"), "reviewer", required=True, max_length=120)
        note = clean_string(payload.get("note"), "note", max_length=1000)
        root = self._project_root(project_id)
        with self._lock:
            project = read_json(root / "project.json")
            if not project:
                raise FileNotFoundError(project_id)
            catalog = self._catalog(root)
            item = next((value for value in catalog["items"] if value.get("material_id") == material_id), None)
            if item is None:
                raise KeyError(material_id)
            if decision == "approved":
                if not item.get("summary"):
                    raise NovelGateError("material approval requires a non-empty summary")
                if item.get("rights_status") == "unknown":
                    raise NovelGateError("material approval requires an explicit rights_status")
            item["status"] = decision
            item["approved_for_story_bible"] = decision == "approved"
            item["review"] = {"decision": decision, "reviewer": reviewer, "note": note, "reviewed_at": now_iso()}
            write_json(root / "materials" / "catalog.json", catalog)
            project["approved_material_count"] = sum(1 for value in catalog["items"] if value["approved_for_story_bible"])
            project["updated_at"] = now_iso()
            write_json(root / "project.json", project)
            return item

    def save_story_bible_seed(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload = require_object(payload)
        unknown = sorted(set(payload) - (STORY_BIBLE_STRING_FIELDS | STORY_BIBLE_LIST_FIELDS))
        if unknown:
            raise ValueError(f"unknown fields: {', '.join(unknown)}")
        required = STORY_BIBLE_STRING_FIELDS - {"platform_positioning"}
        missing = sorted(field for field in required if not str(payload.get(field, "")).strip())
        if missing:
            raise ValueError(f"missing fields: {', '.join(missing)}")
        root = self._project_root(project_id)
        with self._lock:
            project = read_json(root / "project.json")
            if not project:
                raise FileNotFoundError(project_id)
            inputs = {
                field: clean_string(payload.get(field), field, required=field in required, max_length=4000)
                for field in sorted(STORY_BIBLE_STRING_FIELDS)
            }
            inputs.update({
                field: clean_string_list(payload.get(field), field, max_items=30, max_length=500)
                for field in sorted(STORY_BIBLE_LIST_FIELDS)
            })
            seed = {
                "schema_version": "novel-story-bible-seed/v0", "project_id": project_id,
                "seed_id": uuid.uuid4().hex[:12], "inputs": inputs,
                "writes_novel_prose": False, "external_knowledge_accessed": False, "saved_at": now_iso(),
            }
            write_json(root / "story-bible" / "seed.json", seed)
            project["status"] = "story-bible-seeded"
            project["updated_at"] = now_iso()
            write_json(root / "project.json", project)
            return seed

    async def run_bootstrap(self, project_id: str) -> dict[str, Any]:
        framework = self._agent_framework()
        root = self._project_root(project_id)
        project = read_json(root / "project.json")
        if not project:
            raise FileNotFoundError(project_id)
        run_id = f"bootstrap-{datetime.now(SHANGHAI_TZ).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        run_root = root / "runs" / run_id
        artifacts = run_root / "artifacts"
        storage = framework["FileCheckpointStorage"](run_root / "checkpoints")
        artifacts.mkdir(parents=True, exist_ok=True)
        calls: dict[str, int] = {}
        started_at = now_iso()

        def record(stage: str) -> int:
            calls[stage] = calls.get(stage, 0) + 1
            write_json(run_root / "calls.json", calls)
            return calls[stage]

        executor = framework["executor"]
        WorkflowContext = framework["WorkflowContext"]

        @executor(id="project-brief")
        async def project_brief(message: dict[str, Any], ctx: WorkflowContext[dict[str, Any]]) -> None:
            record("project-brief")
            value = {
                "schema_version": "novel-project-brief/v0", "project_id": project_id,
                "title": project["title"], "genre": project["genre"], "platform": project["platform"],
                "premise": project["premise"], "target_words": project["target_words"],
                "rule": "This stage creates production structure, not novel prose.",
            }
            write_json(artifacts / "project-brief.json", value)
            await ctx.send_message({**message, "project_brief": value})

        @executor(id="knowledge-slots")
        async def knowledge_slots(message: dict[str, Any], ctx: WorkflowContext[dict[str, Any]]) -> None:
            record("knowledge-slots")
            catalog = self._catalog(root)
            value = {
                "schema_version": "novel-knowledge-slots/v1", "project_id": project_id,
                "registered_materials": catalog["items"],
                "slots": [
                    {"id": "world", "label": "World and setting", "status": "manifest-ready"},
                    {"id": "character", "label": "Character state", "status": "manifest-ready"},
                    {"id": "plot", "label": "Plot and outline", "status": "manifest-ready"},
                    {"id": "style", "label": "Style and anti-AI constraints", "status": "manifest-ready"},
                    {"id": "market", "label": "Platform and market evidence", "status": "manifest-ready"},
                ],
                "external_knowledge_accessed": False,
            }
            write_json(artifacts / "knowledge-slots.json", value)
            await ctx.send_message({**message, "knowledge_slots": value})

        @executor(id="production-board")
        async def production_board(message: dict[str, Any], ctx: WorkflowContext[dict[str, Any]]) -> None:
            record("production-board")
            value = {
                "schema_version": "novel-production-board/v0", "project_id": project_id,
                "phases": [
                    {"id": "foundation", "label": "Foundation", "tasks": ["material-manifest", "human-review", "story-bible-seed"]},
                    {"id": "architecture", "label": "Book architecture", "tasks": ["story-bible-assembly", "character-ledger", "volume-outline", "continuity-baseline"]},
                    {"id": "chapter-line", "label": "Chapter production", "tasks": ["chapter-job", "draft", "continuity-review", "quality-review", "revision"]},
                    {"id": "release-line", "label": "Release operations", "tasks": ["buffer-check", "metadata-package", "human-approval", "platform-submit", "revenue-ledger"]},
                ],
                "first_gate": "Approved material summaries and a story-bible seed must pass before prose generation.",
            }
            write_json(artifacts / "production-board.json", value)
            await ctx.send_message({**message, "production_board": value})

        @executor(id="bootstrap-package")
        async def package(message: dict[str, Any], ctx: WorkflowContext[dict[str, Any], dict[str, Any]]) -> None:
            record("bootstrap-package")
            value = {
                "schema_version": "novel-bootstrap-package/v1", "project_id": project_id, "run_id": run_id,
                "status": "completed", "writes_full_novel": False,
                "artifacts": [str(artifacts / "project-brief.json"), str(artifacts / "knowledge-slots.json"), str(artifacts / "production-board.json")],
                "next_gate": "review-material-manifests-and-save-story-bible-seed",
            }
            write_json(artifacts / "package.json", value)
            await ctx.yield_output(value)

        skill = await self._load_skill(framework, "novel-project-bootstrap")
        builder = framework["WorkflowBuilder"](
            name="novel-project-bootstrap-v1", start_executor=project_brief,
            checkpoint_storage=storage, output_from=[package],
        )
        builder.add_edge(project_brief, knowledge_slots).add_edge(knowledge_slots, production_board).add_edge(production_board, package)
        result = await builder.build().run({"project_id": project_id, "skill_version": "v1"})
        outputs = result.get_outputs()
        package_value = outputs[-1] if outputs else read_json(artifacts / "package.json", {})
        run_record = self._run_record(
            run_id=run_id, project_id=project_id, workflow="novel.project-bootstrap/v0", started_at=started_at,
            skill=skill, nodes=["project-brief", "knowledge-slots", "production-board", "bootstrap-package"],
            calls=calls, package=package_value,
        )
        write_json(run_root / "run.json", run_record)
        with self._lock:
            project = read_json(root / "project.json", project)
            project["run_count"] = int(project.get("run_count", 0)) + 1
            project["status"] = "bootstrap-ready"
            project["updated_at"] = now_iso()
            write_json(root / "project.json", project)
        return run_record

    async def run_story_bible(self, project_id: str) -> dict[str, Any]:
        framework = self._agent_framework()
        root = self._project_root(project_id)
        project = read_json(root / "project.json")
        if not project:
            raise FileNotFoundError(project_id)
        seed = read_json(root / "story-bible" / "seed.json")
        if not seed:
            raise NovelGateError("story-bible seed must be saved before assembly")
        catalog = self._catalog(root)
        approved = [item for item in catalog["items"] if item.get("approved_for_story_bible")]
        if not approved:
            raise NovelGateError("at least one reviewed and approved material manifest is required")
        if any(not item.get("summary") or item.get("rights_status") == "unknown" for item in approved):
            raise NovelGateError("approved manifests must include summary and rights_status")

        run_id = f"story-bible-{datetime.now(SHANGHAI_TZ).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        run_root = root / "runs" / run_id
        artifacts = run_root / "artifacts"
        storage = framework["FileCheckpointStorage"](run_root / "checkpoints")
        artifacts.mkdir(parents=True, exist_ok=True)
        calls: dict[str, int] = {}
        started_at = now_iso()
        inputs = seed["inputs"]

        def record(stage: str) -> int:
            calls[stage] = calls.get(stage, 0) + 1
            write_json(run_root / "calls.json", calls)
            return calls[stage]

        executor = framework["executor"]
        WorkflowContext = framework["WorkflowContext"]

        @executor(id="material-gate")
        async def material_gate(message: dict[str, Any], ctx: WorkflowContext[dict[str, Any]]) -> None:
            record("material-gate")
            value = {
                "schema_version": "novel-material-gate/v0", "project_id": project_id, "status": "passed",
                "approved_manifest_count": len(approved),
                "manifests": [
                    {
                        "material_id": item["material_id"], "title": item["title"], "category": item["category"],
                        "version": item["version"], "rights_status": item["rights_status"],
                        "content_sha256": item["content_sha256"], "review": item["review"],
                    }
                    for item in approved
                ],
                "uses_summary_only": True, "external_knowledge_accessed": False, "workbuddy_access": False,
            }
            write_json(artifacts / "material-gate.json", value)
            await ctx.send_message({**message, "material_gate": value})

        @executor(id="narrative-contract")
        async def narrative_contract(message: dict[str, Any], ctx: WorkflowContext[dict[str, Any]]) -> None:
            record("narrative-contract")
            value = {
                "schema_version": "novel-narrative-contract/v0", "project_id": project_id,
                "book_identity": {
                    "title": project["title"], "genre": project["genre"], "platform": project["platform"],
                    "premise": project["premise"], "target_words": project["target_words"],
                },
                "core_conflict": inputs["core_conflict"], "protagonist_goal": inputs["protagonist_goal"],
                "antagonistic_force": inputs["antagonistic_force"], "ending_direction": inputs["ending_direction"],
                "platform_positioning": inputs["platform_positioning"], "world_rules": inputs["world_rules"],
                "style_constraints": inputs["style_constraints"], "forbidden_elements": inputs["forbidden_elements"],
                "evidence_manifest_ids": [item["material_id"] for item in approved],
            }
            write_json(artifacts / "narrative-contract.json", value)
            await ctx.send_message({**message, "narrative_contract": value})

        @executor(id="continuity-baseline")
        async def continuity_baseline(message: dict[str, Any], ctx: WorkflowContext[dict[str, Any]]) -> None:
            record("continuity-baseline")
            value = {
                "schema_version": "novel-continuity-baseline/v0", "project_id": project_id,
                "character_constraints": inputs["character_constraints"],
                "initial_ledger": {
                    "chapter": 0, "locations": [], "relationships": [], "injuries": [],
                    "abilities": [], "resources": [], "open_promises": [],
                },
                "required_before_chapter_jobs": ["named character records", "volume outline", "chapter acceptance contract"],
            }
            write_json(artifacts / "continuity-baseline.json", value)
            await ctx.send_message({**message, "continuity_baseline": value})

        @executor(id="story-bible-package")
        async def story_bible_package(message: dict[str, Any], ctx: WorkflowContext[dict[str, Any], dict[str, Any]]) -> None:
            record("story-bible-package")
            story_bible = {
                "schema_version": "novel-story-bible/v0", "project_id": project_id, "run_id": run_id,
                "status": "assembled", "assembled_at": now_iso(),
                "source_manifest_ids": [item["material_id"] for item in approved],
                "source_summaries": [
                    {"material_id": item["material_id"], "category": item["category"], "summary": item["summary"]}
                    for item in approved
                ],
                "narrative_contract": read_json(artifacts / "narrative-contract.json", {}),
                "continuity_baseline": read_json(artifacts / "continuity-baseline.json", {}),
                "writes_novel_prose": False, "external_knowledge_accessed": False, "workbuddy_access": False,
                "next_gate": "character-ledger-and-volume-outline",
            }
            write_json(artifacts / "story-bible.json", story_bible)
            write_json(root / "story-bible" / "current.json", story_bible)
            write_json(root / "story-bible" / "history" / f"{run_id}.json", story_bible)
            value = {
                "schema_version": "novel-story-bible-package/v0", "project_id": project_id, "run_id": run_id,
                "status": "completed", "writes_full_novel": False,
                "artifacts": [
                    str(artifacts / "material-gate.json"), str(artifacts / "narrative-contract.json"),
                    str(artifacts / "continuity-baseline.json"), str(artifacts / "story-bible.json"),
                    str(root / "story-bible" / "current.json"),
                ],
                "next_gate": "character-ledger-and-volume-outline",
            }
            write_json(artifacts / "package.json", value)
            await ctx.yield_output(value)

        skill = await self._load_skill(framework, "novel-story-bible-assembly")
        builder = framework["WorkflowBuilder"](
            name="novel-story-bible-assembly-v0", start_executor=material_gate,
            checkpoint_storage=storage, output_from=[story_bible_package],
        )
        builder.add_edge(material_gate, narrative_contract).add_edge(narrative_contract, continuity_baseline).add_edge(continuity_baseline, story_bible_package)
        result = await builder.build().run({"project_id": project_id, "skill_version": "v0"})
        outputs = result.get_outputs()
        package_value = outputs[-1] if outputs else read_json(artifacts / "package.json", {})
        run_record = self._run_record(
            run_id=run_id, project_id=project_id, workflow="novel.story-bible-assembly/v0", started_at=started_at,
            skill=skill, nodes=["material-gate", "narrative-contract", "continuity-baseline", "story-bible-package"],
            calls=calls, package=package_value,
        )
        write_json(run_root / "run.json", run_record)
        with self._lock:
            project = read_json(root / "project.json", project)
            project["run_count"] = int(project.get("run_count", 0)) + 1
            project["story_bible_run_count"] = int(project.get("story_bible_run_count", 0)) + 1
            project["status"] = "story-bible-ready"
            project["updated_at"] = now_iso()
            write_json(root / "project.json", project)
        return run_record

    async def run_chapter_context(self, project_id: str, request_data: dict[str, Any]) -> dict[str, Any]:
        framework = self._agent_framework()
        root = self._project_root(project_id)
        project = read_json(root / "project.json")
        if not project:
            raise FileNotFoundError(project_id)
        request = validate_chapter_context_request(request_data)
        request.project_id = project_id

        # Gate pre-flight (contract section 9): project, story bible, catalog.
        if not (root / "story-bible" / "current.json").is_file():
            raise NovelGateError(CODE_STORY_BIBLE_NOT_READY)
        try:
            LocalFileKnowledgeAdapter(self.group_root)
        except KnowledgeAdapterError:
            raise

        job_id = request.job_id or f"chapter-{project_id}-{request.chapter_number}-{uuid.uuid4().hex[:8]}"
        run_id = f"chapter-context-{datetime.now(SHANGHAI_TZ).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        job_root = root / "chapter-jobs" / job_id
        artifacts = job_root
        storage = framework["FileCheckpointStorage"](job_root / "checkpoints")
        calls: dict[str, int] = {}
        started_at = now_iso()

        def record(stage: str) -> int:
            calls[stage] = calls.get(stage, 0) + 1
            write_json(job_root / "calls.json", calls)
            return calls[stage]

        executor = framework["executor"]
        WorkflowContext = framework["WorkflowContext"]

        @executor(id="chapter-gate")
        async def chapter_gate(message: dict[str, Any], ctx: WorkflowContext[dict[str, Any]]) -> None:
            record("chapter-gate")
            value = {
                "schema_version": "novel-chapter-gate/v0", "project_id": project_id,
                "job_id": job_id, "status": "passed", "story_bible_ready": True,
                "catalog_available": True, "request_schema_valid": True,
                "writes_novel_prose": False, "external_knowledge_accessed": False,
            }
            write_json(artifacts / "chapter-gate.json", value)
            await ctx.send_message({**message, "chapter_gate": value, "request": request.to_dict()})

        @executor(id="knowledge-query")
        async def knowledge_query(message: dict[str, Any], ctx: WorkflowContext[dict[str, Any]]) -> None:
            record("knowledge-query")
            adapter = LocalFileKnowledgeAdapter(self.group_root)
            query = build_knowledge_query(request, job_id)
            result = adapter.query(query)
            if result.status == "blocked":
                raise NovelGateError(result.blocking_codes[0])
            write_json(artifacts / "knowledge-query.json", query.to_dict())
            write_json(artifacts / "knowledge-result.json", result.to_dict())
            await ctx.send_message({**message, "query": query.to_dict(), "result": result.to_dict()})

        @executor(id="token-trim")
        async def token_trim(message: dict[str, Any], ctx: WorkflowContext[dict[str, Any]]) -> None:
            record("token-trim")
            query = KnowledgeQuery(**(message.get("query") or {}))
            result = KnowledgeResult(**(message.get("result") or {}))
            result.snippets = [KnowledgeSnippet(**snippet) for snippet in result.snippets]
            context = build_chapter_context(
                request=request, query=query, result=result,
                adapter_id=result.adapter.get("id", LocalFileKnowledgeAdapter.ADAPTER_ID),
                adapter_version=result.adapter.get("version", LocalFileKnowledgeAdapter.ADAPTER_VERSION),
                job_id=job_id,
            )
            if context.status == "blocked":
                raise NovelGateError(context.blocking_codes[0])
            write_json(artifacts / "knowledge-context.json", context.to_dict())
            write_json(artifacts / "citation-evidence.json", [citation.to_dict() for citation in context.citations])
            await ctx.send_message({**message, "context": context.to_dict()})

        @executor(id="context-package")
        async def context_package(message: dict[str, Any], ctx: WorkflowContext[dict[str, Any], dict[str, Any]]) -> None:
            record("context-package")
            context = ChapterKnowledgeContext(**(message.get("context") or {}))
            context.selected_snippets = [KnowledgeSnippet(**snippet) for snippet in context.selected_snippets]
            context.citations = [KnowledgeCitation(**citation) for citation in context.citations]
            artifact_paths = {
                "request": str(artifacts / "request.json"),
                "knowledge_query": str(artifacts / "knowledge-query.json"),
                "knowledge_result": str(artifacts / "knowledge-result.json"),
                "knowledge_context": str(artifacts / "knowledge-context.json"),
                "citation_evidence": str(artifacts / "citation-evidence.json"),
                "package": str(artifacts / "package.json"),
                "calls": str(artifacts / "calls.json"),
                "checkpoints": str(job_root / "checkpoints"),
                "run": str(artifacts / "run.json"),
            }
            write_json(artifacts / "request.json", request.to_dict())
            package = build_chapter_context_package(
                request=request, context=context, job_id=job_id, artifact_paths=artifact_paths,
            )
            write_json(artifacts / "package.json", package.to_dict())
            await ctx.yield_output(package.to_dict())

        skill = types.SimpleNamespace(
            frontmatter=types.SimpleNamespace(name="novel-chapter-knowledge-context", metadata={}),
        )
        builder = framework["WorkflowBuilder"](
            name="novel-chapter-knowledge-context-v0", start_executor=chapter_gate,
            checkpoint_storage=storage, output_from=[context_package],
        )
        builder.add_edge(chapter_gate, knowledge_query).add_edge(knowledge_query, token_trim).add_edge(token_trim, context_package)
        await builder.build().run({"project_id": project_id, "job_id": job_id, "skill_version": "v0"})
        package_value = read_json(artifacts / "package.json", {})
        run_record = self._run_record(
            run_id=run_id, project_id=project_id, workflow=WORKFLOW_NAME, started_at=started_at,
            skill=skill, nodes=["chapter-gate", "knowledge-query", "token-trim", "context-package"],
            calls=calls, package=package_value,
        )
        write_json(artifacts / "run.json", run_record)
        return run_record

    @staticmethod
    def _agent_framework() -> dict[str, Any]:
        try:
            from agent_framework import (
                AgentSession, FileCheckpointStorage, FileSkillsSource, SkillsSourceContext,
                WorkflowBuilder, WorkflowContext, executor,
            )
        except ImportError as exc:
            raise RuntimeError("agent-framework-core==1.12.0 is required") from exc
        return {
            "AgentSession": AgentSession, "FileCheckpointStorage": FileCheckpointStorage,
            "FileSkillsSource": FileSkillsSource, "SkillsSourceContext": SkillsSourceContext,
            "WorkflowBuilder": WorkflowBuilder, "WorkflowContext": WorkflowContext, "executor": executor,
        }

    async def _load_skill(self, framework: dict[str, Any], name: str):
        AgentSession = framework["AgentSession"]

        class SkillAgent:
            id = "novel-studio"
            name = "Novel Studio"
            description = "Local novel production studio"

            async def run(self, messages=None, *, stream=False, session=None, **kwargs):
                raise RuntimeError("No model call is used by this structural workflow")

            def create_session(self, *, session_id=None):
                return AgentSession(session_id=session_id)

            def get_session(self, service_session_id, *, session_id=None):
                return AgentSession(service_session_id=service_session_id, session_id=session_id)

        skill_root = Path(__file__).resolve().parent / "skills"
        skills = await framework["FileSkillsSource"](skill_root).get_skills(framework["SkillsSourceContext"](agent=SkillAgent()))
        skill = next((item for item in skills if item.frontmatter.name == name), None)
        if skill is None:
            raise RuntimeError(f"{name} skill is missing")
        return skill

    @staticmethod
    def _run_record(*, run_id: str, project_id: str, workflow: str, started_at: str, skill: Any, nodes: list[str], calls: dict[str, int], package: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "novel-run/v1", "run_id": run_id, "project_id": project_id,
            "workflow": workflow, "kernel": "agent-framework-core/1.12.0", "status": "completed",
            "started_at": started_at, "completed_at": now_iso(),
            "skill": {"name": skill.frontmatter.name, "metadata": dict(skill.frontmatter.metadata or {})},
            "nodes": [{"id": node, "status": "completed"} for node in nodes],
            "calls": calls, "package": package,
        }

    def _latest_run(self, project_id: str, workflow: str | None = None) -> dict[str, Any] | None:
        runs = sorted((self._project_root(project_id) / "runs").glob("*/run.json"), reverse=True)
        for run_path in runs:
            value = read_json(run_path)
            if value and (workflow is None or value.get("workflow") == workflow):
                return value
        return None

    @staticmethod
    def pipeline_template() -> dict[str, Any]:
        nodes = [
            {"id": "project-brief", "label": "Project brief", "group": "bootstrap"},
            {"id": "knowledge-slots", "label": "Manifest slots", "group": "bootstrap"},
            {"id": "production-board", "label": "Production board", "group": "bootstrap"},
            {"id": "bootstrap-package", "label": "Bootstrap gate", "group": "bootstrap"},
            {"id": "material-gate", "label": "Material review gate", "group": "story-bible"},
            {"id": "narrative-contract", "label": "Narrative contract", "group": "story-bible"},
            {"id": "continuity-baseline", "label": "Continuity baseline", "group": "story-bible"},
            {"id": "story-bible-package", "label": "Story Bible package", "group": "story-bible"},
        ]
        return {"nodes": nodes, "edges": [[nodes[index]["id"], nodes[index + 1]["id"]] for index in range(len(nodes) - 1)]}


def mount_novel_routes(app, group_root: Path) -> None:
    from .novel_director import NovelDirector, NovelDirectorProtocolError

    studio = NovelStudio(group_root)
    director = NovelDirector(studio.root / "conversations")

    @app.get("/api/novel/studio")
    def novel_overview():
        return studio.overview()

    @app.get("/api/novel/director/sessions/{session_id}")
    def novel_director_transcript(session_id: str):
        try:
            return {"session_id": session_id, "turns": director.transcript(session_id)}
        except ValueError as exc:
            return JSONResponse({"code": "validation-error", "message": str(exc)}, status_code=422)

    @app.post("/api/novel/director/chat")
    async def chat_with_novel_director(request: Request):
        try:
            payload = require_object(await request.json())
            project_id = clean_string(payload.pop("project_id", ""), "project_id", max_length=64)
            project_context = None
            if project_id:
                project_context = next((item for item in studio.list_projects() if item["project_id"] == project_id), None)
                if project_context is None:
                    return JSONResponse({"code": "project-not-found", "message": "Project not found"}, status_code=404)
            return await asyncio.to_thread(director.chat, payload, project_context)
        except ProviderError as exc:
            return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=503)
        except NovelDirectorProtocolError as exc:
            return JSONResponse({"code": "director-protocol-error", "message": str(exc)}, status_code=502)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return JSONResponse({"code": "validation-error", "message": str(exc)}, status_code=422)

    @app.post("/api/novel/projects", status_code=201)
    async def create_novel_project(request: Request):
        try:
            return studio.create_project(await request.json())
        except FileExistsError:
            return JSONResponse({"code": "duplicate-project", "message": "Project already exists"}, status_code=409)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return JSONResponse({"code": "validation-error", "message": str(exc)}, status_code=422)

    @app.post("/api/novel/projects/{project_id}/materials", status_code=201)
    async def register_novel_material(project_id: str, request: Request):
        try:
            return studio.register_material(project_id, await request.json())
        except FileNotFoundError:
            return JSONResponse({"code": "project-not-found", "message": "Project not found"}, status_code=404)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return JSONResponse({"code": "validation-error", "message": str(exc)}, status_code=422)

    @app.post("/api/novel/projects/{project_id}/materials/{material_id}/review")
    async def review_novel_material(project_id: str, material_id: str, request: Request):
        try:
            return studio.review_material(project_id, material_id, await request.json())
        except FileNotFoundError:
            return JSONResponse({"code": "project-not-found", "message": "Project not found"}, status_code=404)
        except KeyError:
            return JSONResponse({"code": "material-not-found", "message": "Material manifest not found"}, status_code=404)
        except NovelGateError as exc:
            return JSONResponse({"code": "material-review-gate", "message": str(exc)}, status_code=409)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return JSONResponse({"code": "validation-error", "message": str(exc)}, status_code=422)

    @app.post("/api/novel/projects/{project_id}/story-bible/seed")
    async def save_novel_story_bible_seed(project_id: str, request: Request):
        try:
            return studio.save_story_bible_seed(project_id, await request.json())
        except FileNotFoundError:
            return JSONResponse({"code": "project-not-found", "message": "Project not found"}, status_code=404)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return JSONResponse({"code": "validation-error", "message": str(exc)}, status_code=422)

    @app.post("/api/novel/projects/{project_id}/bootstrap", status_code=202)
    async def bootstrap_novel_project(project_id: str):
        try:
            return await studio.run_bootstrap(project_id)
        except FileNotFoundError:
            return JSONResponse({"code": "project-not-found", "message": "Project not found"}, status_code=404)
        except RuntimeError as exc:
            return JSONResponse({"code": "runtime-unavailable", "message": str(exc)}, status_code=503)

    @app.post("/api/novel/projects/{project_id}/story-bible/assemble", status_code=202)
    async def assemble_novel_story_bible(project_id: str):
        try:
            return await studio.run_story_bible(project_id)
        except FileNotFoundError:
            return JSONResponse({"code": "project-not-found", "message": "Project not found"}, status_code=404)
        except NovelGateError as exc:
            return JSONResponse({"code": "story-bible-gate-blocked", "message": str(exc)}, status_code=409)
        except RuntimeError as exc:
            return JSONResponse({"code": "runtime-unavailable", "message": str(exc)}, status_code=503)

    @app.post("/api/novel/projects/{project_id}/chapter-context/assemble", status_code=202)
    async def assemble_chapter_context(project_id: str, request: Request):
        try:
            return await studio.run_chapter_context(project_id, await request.json())
        except FileNotFoundError:
            return JSONResponse({"code": "project-not-found", "message": "Project not found"}, status_code=404)
        except KnowledgeAdapterError as exc:
            return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=409)
        except NovelGateError as exc:
            return JSONResponse({"code": "chapter-context-blocked", "message": str(exc)}, status_code=409)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return JSONResponse({"code": "validation-error", "message": str(exc)}, status_code=422)
        except RuntimeError as exc:
            return JSONResponse({"code": "runtime-unavailable", "message": str(exc)}, status_code=503)
