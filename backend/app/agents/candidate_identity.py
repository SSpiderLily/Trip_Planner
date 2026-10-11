"""候选绑定变更时生成可复现的新身份。"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from app.models.planning_v4.tasks import CandidateRef


def version_candidate_ref(
    candidate: CandidateRef,
    *,
    source_candidate_ids: Iterable[str] | None = None,
    **updates: Any,
) -> CandidateRef:
    """保留输入引用；内容变化时以输入ID和新内容生成稳定版本ID。"""
    if "candidate_id" in updates:
        raise ValueError("candidate_id 由版本助手生成")
    changed = {
        field: value for field, value in updates.items()
        if getattr(candidate, field) != value
    }
    if not changed:
        return candidate
    version = candidate.model_copy(update=changed)
    source_ids = sorted(set(source_candidate_ids or (candidate.candidate_id,)))
    identity = {
        "source_candidate_ids": source_ids,
        "candidate": version.model_dump(mode="json", exclude={"candidate_id"}),
    }
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return version.model_copy(update={"candidate_id": f"candidate-version-{digest}"})
