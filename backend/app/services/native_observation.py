"""原生业务事件适配现有观测仓库；不保存消息历史或供应商原始响应。"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from threading import Lock

from .observation_service import span, mark_observation_missing

_scope = ContextVar("native_observation_scope", default={})
_run = ContextVar("native_observation_run", default=None)

# 只接受已声明的业务事件及顶层字段；未知事件不落盘。
EVENT_FIELDS = {
    "requirements.start": "request_id",
    "requirements.accepted": "requirements",
    "requirements.submission_rejected": "code",
    "dispatch.round_start": "round call_ids tool_names max_workers available_anchors fact_count",
    "dispatch.round_end": "round task_ids fact_count source_count available_anchors",
    "master.task": "round arguments task",
    "master.task_rejected": "round code",
    "map.request": "task_id tool_name arguments",
    "map.result": "task_id tool_name query_status result_status source returned_places available_candidates error",
    "child.selection": "task_id selections status",
    "child.selection_rejected": "task_id code",
    "child.result": "task_id task_result places unmet_needs",
    "master.result": "run_id rounds candidates child_statuses",
    "planning.phase": "phase day_id round candidate_count",
    "planning.submission_rejected": "codes",
    "planning.initial": "draft report",
    "repair.collect": "day_id round targets",
    "repair.collected": "day_id stop_reason remaining_targets",
    "repair.submission_rejected": "day_id codes",
    "repair.revision": "round revision",
    "repair.validation": "day_id round revision report",
    "planning.result": "status draft report repair_states attempts",
    "preview.converted": "task_id plan_id revision route_day_ids",
    "preview.route_context_unavailable": "plan_id exception_type",
}
_excluded = frozenset({"raw", "raw_data", "raw_response", "response", "messages", "content",
    "reasoning", "reasoning_content", "provider_reasoning", "photos", "opening_hours_raw"})


def _project(value):
    if isinstance(value, dict):
        return {key: _project(item) for key, item in value.items() if key not in _excluded}
    if isinstance(value, (list, tuple)):
        return [_project(item) for item in value]
    return value


def native_metadata():
    run = _run.get()
    return {**(run[0] if run else {}), **_scope.get()}


class _RejectedEvent(Exception):
    code = "NATIVE_BUSINESS_REJECTED"


def native_event_emitter(sink=None):
    """幂等包装：共享回调贯穿主子智能体时，同一事件只采集一次。"""
    if getattr(sink, "_native_observation_emitter", False):
        return sink

    def emit(event, data):
        if event in EVENT_FIELDS:
            try:
                payload = _project({key: data[key] for key in EVENT_FIELDS[event].split() if key in data})
                if event == "map.result" and isinstance(payload.get("error"), dict):
                    payload["error"] = {key: payload["error"][key] for key in ("code", "category", "provider_code", "retryable") if key in payload["error"]}
                metadata = native_metadata()
                if event == "master.task":
                    metadata["target_role"] = metadata.get("role")
                    metadata["role"] = "master"
                elif event.startswith(("master.", "planning.", "requirements.", "dispatch.", "repair.")):
                    metadata.setdefault("role", "master")
                run = _run.get()
                if run:
                    with run[1]:
                        run[2][0] += 1
                        metadata["sequence"] = run[2][0]
                rejected = event.endswith("_rejected")
                with span("native." + event, "validation" if rejected or event == "repair.validation" else "agent",
                          {**metadata, "native_event": event, "record_kind": "business_event"}) as record:
                    record.output = {"outcome": "rejected" if rejected else "recorded", **payload}
                    if rejected:
                        raise _RejectedEvent()
            except _RejectedEvent:
                pass
            except Exception as exc:
                mark_observation_missing("native_event_projection_failed", exc)
        if sink is not None:
            sink(event, data)
    emit._native_observation_emitter = True
    return emit


@contextmanager
def native_agent_scope(role, subtask_id, round_number):
    metadata = {"role": role, "subtask_id": subtask_id, "round": round_number, "stage": "collection"}
    token = _scope.set(metadata)
    try:
        with span("native.agent." + role, "agent", {**native_metadata()}) as record:
            yield record
    finally:
        _scope.reset(token)


def observe_native_run(function):
    @wraps(function)
    async def observed(self, request, *, task_id, plan_id, progress=None):
        token = _run.set(({"task_id": task_id, "plan_id": plan_id}, Lock(), [0]))
        try:
            return await function(self, request, task_id=task_id, plan_id=plan_id, progress=progress)
        finally:
            _run.reset(token)
    return observed
