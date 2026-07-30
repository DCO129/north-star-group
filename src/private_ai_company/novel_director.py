from __future__ import annotations

import json
import re
import threading
import uuid
from pathlib import Path
from typing import Any

from .novel_studio import (
    ALLOWED_MATERIAL_CATEGORIES,
    ALLOWED_PLATFORMS,
    ALLOWED_RIGHTS_STATUS,
    ALLOWED_SOURCE_KINDS,
    STORY_BIBLE_LIST_FIELDS,
    STORY_BIBLE_STRING_FIELDS,
    clean_string,
    clean_string_list,
    now_iso,
    read_json,
)
from .providers import DeepSeekProvider, ModelMessage, ModelProvider, ModelRequest

SESSION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")
PROJECT_PATCH_FIELDS = {"project_id", "title", "pen_name", "genre", "platform", "premise", "target_words"}
MATERIAL_PATCH_FIELDS = {"title", "category", "source_kind", "source_ref", "version", "rights_status", "summary", "tags"}

SYSTEM_PROMPT = """You are the Novel Director inside a private AI group. You talk with the human owner in Chinese and do the data-entry work for a long-running commercial web-novel production line.

Your job is not to write a complete novel. Your job is to understand the owner's intent, ask only necessary questions, and fill structured production fields for them. The human may edit and approve your proposal.

Return one JSON object only with this exact top-level shape:
{
  "assistant_message": "Chinese response to the owner",
  "questions": ["short Chinese question"],
  "project_patch": {
    "project_id": "lowercase-slug",
    "title": "",
    "pen_name": "",
    "genre": "",
    "platform": "fanqie|qidian|internal|undecided",
    "premise": "",
    "target_words": 1200000
  },
  "material_manifest_patch": {
    "title": "",
    "category": "world|character|plot|style|market|reference",
    "source_kind": "distilled-summary|external-reference|manual-note|registered-slot",
    "source_ref": "opaque reference only",
    "version": "v1",
    "rights_status": "owned|licensed|public-domain|reference-only|unknown",
    "summary": "bounded summary, never raw book content",
    "tags": ["tag"]
  },
  "story_bible_seed_patch": {
    "core_conflict": "",
    "protagonist_goal": "",
    "antagonistic_force": "",
    "ending_direction": "",
    "platform_positioning": "",
    "world_rules": [""],
    "character_constraints": [""],
    "style_constraints": [""],
    "forbidden_elements": [""]
  },
  "recommended_next_action": "create-project|register-material|save-seed|approve-material|assemble-story-bible|continue-dialogue"
}

Rules:
- Fill as many fields as the conversation supports. Omit unsupported fields instead of inventing certainty.
- Preserve non-empty human draft values unless the owner explicitly asks to replace them.
- Never claim to have read WorkBuddy, CodeBuddy, WSL, or any external knowledge store.
- Never approve a material manifest. Human approval is mandatory.
- Never include hidden reasoning, credentials, raw copyrighted book content, or full chapter prose.
- Keep assistant_message practical and concise.
- The response must be valid JSON, not Markdown.
"""


class NovelDirectorProtocolError(RuntimeError):
    pass


class NovelDirector:
    def __init__(self, root: Path, provider: ModelProvider | None = None) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.provider = provider or DeepSeekProvider()
        self._lock = threading.RLock()

    def chat(self, payload: dict[str, Any], project_context: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        unknown = sorted(set(payload) - {"session_id", "message", "current_draft"})
        if unknown:
            raise ValueError(f"unknown fields: {', '.join(unknown)}")
        message = clean_string(payload.get("message"), "message", required=True, max_length=12000)
        session_id = self._session_id(payload.get("session_id"))
        current_draft = payload.get("current_draft") or {}
        if not isinstance(current_draft, dict):
            raise ValueError("current_draft must be an object")
        context = {
            "current_draft": current_draft,
            "persisted_project": self._bounded_project_context(project_context),
            "hard_boundaries": {
                "writes_full_novel": False,
                "workbuddy_access": False,
                "human_approval_required": True,
            },
        }
        messages = [ModelMessage("system", SYSTEM_PROMPT)]
        messages.extend(self._history_messages(session_id))
        messages.append(ModelMessage("user", json.dumps({"owner_message": message, "context": context}, ensure_ascii=False)))
        result = self.provider.complete(ModelRequest(
            messages=tuple(messages),
            max_output_tokens=2200,
            temperature=0.25,
            response_format="json_object",
            thinking="disabled",
            timeout_seconds=90.0,
        ))
        if not isinstance(result.parsed_json, dict):
            raise NovelDirectorProtocolError("model did not return a JSON object")
        proposal = self._normalize_proposal(result.parsed_json)
        record = {
            "schema_version": "novel-director-turn/v0",
            "session_id": session_id,
            "created_at": now_iso(),
            "user_message": message,
            "assistant_message": proposal["assistant_message"],
            "questions": proposal["questions"],
            "proposal": {
                "project_patch": proposal["project_patch"],
                "material_manifest_patch": proposal["material_manifest_patch"],
                "story_bible_seed_patch": proposal["story_bible_seed_patch"],
                "recommended_next_action": proposal["recommended_next_action"],
            },
            "provider": result.provider,
            "model": result.model,
            "usage": {
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "total_tokens": result.usage.total_tokens,
            },
            "private_reasoning_persisted": False,
        }
        self._append_turn(session_id, record)
        return record

    def transcript(self, session_id: str) -> list[dict[str, Any]]:
        session_id = self._session_id(session_id)
        path = self.root / f"{session_id}.jsonl"
        if not path.is_file():
            return []
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
        return records

    @staticmethod
    def _session_id(value: Any) -> str:
        candidate = clean_string(value, "session_id", max_length=64).lower()
        if not candidate:
            return f"novel-{uuid.uuid4().hex[:16]}"
        if not SESSION_ID_RE.fullmatch(candidate):
            raise ValueError("session_id must use lowercase letters, numbers, and hyphens")
        return candidate

    def _history_messages(self, session_id: str) -> list[ModelMessage]:
        messages: list[ModelMessage] = []
        for item in self.transcript(session_id)[-8:]:
            user = clean_string(item.get("user_message"), "user_message", max_length=12000)
            assistant = clean_string(item.get("assistant_message"), "assistant_message", max_length=6000)
            if user:
                messages.append(ModelMessage("user", user))
            if assistant:
                messages.append(ModelMessage("assistant", assistant))
        return messages

    def _append_turn(self, session_id: str, record: dict[str, Any]) -> None:
        path = self.root / f"{session_id}.jsonl"
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        with self._lock:
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line)

    @staticmethod
    def _bounded_project_context(project: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(project, dict):
            return None
        return {
            "project_id": project.get("project_id"),
            "title": project.get("title"),
            "genre": project.get("genre"),
            "platform": project.get("platform"),
            "premise": project.get("premise"),
            "target_words": project.get("target_words"),
            "status": project.get("status"),
            "approved_material_count": project.get("approved_material_count", 0),
            "material_summaries": [
                {
                    "title": item.get("title"), "category": item.get("category"),
                    "summary": item.get("summary"), "status": item.get("status"),
                }
                for item in (project.get("materials") or [])[:12]
                if isinstance(item, dict)
            ],
            "story_bible_seed": project.get("story_bible_seed"),
        }

    @staticmethod
    def _normalize_proposal(value: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "assistant_message", "questions", "project_patch", "material_manifest_patch",
            "story_bible_seed_patch", "recommended_next_action",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise NovelDirectorProtocolError(f"model returned unknown fields: {', '.join(unknown)}")
        assistant_message = clean_string(value.get("assistant_message"), "assistant_message", required=True, max_length=6000)
        questions = clean_string_list(value.get("questions"), "questions", max_items=8, max_length=500)
        project_patch = NovelDirector._project_patch(value.get("project_patch"))
        material_patch = NovelDirector._material_patch(value.get("material_manifest_patch"))
        seed_patch = NovelDirector._seed_patch(value.get("story_bible_seed_patch"))
        action = clean_string(value.get("recommended_next_action", "continue-dialogue"), "recommended_next_action", max_length=80)
        allowed_actions = {"create-project", "register-material", "save-seed", "approve-material", "assemble-story-bible", "continue-dialogue"}
        if action not in allowed_actions:
            action = "continue-dialogue"
        return {
            "assistant_message": assistant_message,
            "questions": questions,
            "project_patch": project_patch,
            "material_manifest_patch": material_patch,
            "story_bible_seed_patch": seed_patch,
            "recommended_next_action": action,
        }

    @staticmethod
    def _project_patch(value: Any) -> dict[str, Any]:
        if value in (None, {}):
            return {}
        if not isinstance(value, dict):
            raise NovelDirectorProtocolError("project_patch must be an object")
        patch: dict[str, Any] = {}
        for field in PROJECT_PATCH_FIELDS:
            if field not in value or value[field] in (None, ""):
                continue
            if field == "target_words":
                try:
                    words = int(value[field])
                except (TypeError, ValueError) as exc:
                    raise NovelDirectorProtocolError("target_words must be an integer") from exc
                if 50_000 <= words <= 10_000_000:
                    patch[field] = words
                continue
            text = clean_string(value[field], field, max_length=4000)
            if field == "project_id":
                slug = re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")[:64]
                if len(slug) < 3:
                    continue
                text = slug
            if field == "platform":
                text = text.lower()
                if text not in ALLOWED_PLATFORMS:
                    continue
            patch[field] = text
        return patch

    @staticmethod
    def _material_patch(value: Any) -> dict[str, Any]:
        if value in (None, {}):
            return {}
        if not isinstance(value, dict):
            raise NovelDirectorProtocolError("material_manifest_patch must be an object")
        patch: dict[str, Any] = {}
        for field in MATERIAL_PATCH_FIELDS:
            if field not in value or value[field] in (None, "", []):
                continue
            if field == "tags":
                patch[field] = clean_string_list(value[field], field, max_items=12, max_length=80)
                continue
            text = clean_string(value[field], field, max_length=4000)
            if field == "category":
                text = text.lower()
                if text not in ALLOWED_MATERIAL_CATEGORIES:
                    continue
            if field == "source_kind":
                text = text.lower()
                if text not in ALLOWED_SOURCE_KINDS:
                    continue
            if field == "rights_status":
                text = text.lower()
                if text not in ALLOWED_RIGHTS_STATUS:
                    continue
            patch[field] = text
        return patch

    @staticmethod
    def _seed_patch(value: Any) -> dict[str, Any]:
        if value in (None, {}):
            return {}
        if not isinstance(value, dict):
            raise NovelDirectorProtocolError("story_bible_seed_patch must be an object")
        patch: dict[str, Any] = {}
        for field in STORY_BIBLE_STRING_FIELDS:
            if field in value and value[field] not in (None, ""):
                patch[field] = clean_string(value[field], field, max_length=4000)
        for field in STORY_BIBLE_LIST_FIELDS:
            if field in value and value[field] not in (None, "", []):
                patch[field] = clean_string_list(value[field], field, max_items=30, max_length=500)
        return patch
