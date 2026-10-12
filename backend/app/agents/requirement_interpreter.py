"""独立的旅行需求整理入口；不查询地点或生成行程。"""
from __future__ import annotations

from app.services.planning_timeout_policy import deadline_after, wait_timeout, timeouts_enabled
import json
import math
import time
from threading import Event
from uuid import uuid4

from pydantic import ValidationError, model_validator

from ..models.planning_v4.base import ContractModel
from ..models.planning_v4.input import (
    HardRequirement,
    LodgingChange,
    RequirementSummary,
    TripRequest,
    validate_requirement_sources,
)
from ..services.llm_service import get_llm
from ..services.planning_diagnostics import safe_path
from ..services.observation_service import span, token_usage
from .execution import PlanningError


DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_TIMEOUT_SECONDS = 120.0
MAX_FORMAT_REPAIRS = 1


class RequirementDraft(ContractModel):
    """仅包含模型可整理的字段；ID 和原始请求由后端保留。"""
    core_goals: tuple[str, ...]
    lodging_changes: tuple[LodgingChange, ...] = ()
    hard_requirements: tuple[HardRequirement, ...]
    preferences: tuple[str, ...]
    unknowns: tuple[str, ...]
    assumptions: tuple[str, ...]

    @model_validator(mode="after")
    def validate_requirement_ids(self):
        ids = [item.requirement_id for item in self.hard_requirements]
        if len(ids) != len(set(ids)):
            raise ValueError("hard requirement IDs must be unique")
        return self


SYSTEM_PROMPT = """你是旅行规划的主智能体，当前只整理用户需求，不查询地图、不生成行程。
只返回符合给定 JSON Schema 的 JSON 对象。只输出 core_goals、hard_requirements、lodging_changes、preferences、unknowns、assumptions。

分类规则：
- 只有用户明确要求必须做或明确禁止的内容，才放进 hard_requirements；must_visit 对应明确必去，must_avoid 对应明确不去，must_include 对应明确必须包含的其他条件。
- 每条 hard requirement 必须提供 source.field 和 source.quote。quote 必须逐字来自对应原始字段；不得根据常识补写引用。requirement_id 在本次摘要中使用唯一的 M1、M2 等编号。
- 可调整的倾向放进 preferences，不要提升成硬性条件。例如“以公共交通结合步行为主”不表示禁止骑行；“节奏适中”不表示每天最多安排固定数量的活动；“安排午餐和晚餐”不表示排除早餐。
- core_goals 只概括用户明确表达的旅行体验。unknowns 记录对后续规划有影响但未提供的信息。assumptions 只记录确实需要且明确标出的工作假设；没有依据时留空。
- 不得捏造人数、机场/车站、酒店、预算金额或硬性要求。预算scope是全程，amount_basis=per_person表示全程人均预算，whole_party表示旧版全程总预算；人数未指定时必须保持未指定。
- lodging_query是后端直接核实的指定住处，不要重复生成酒店hard_requirements；不得把住处归入餐饮条件。
- 只有用户明确在某日期跨城或换住处，才填写lodging_changes。start_date是生效日期，city仅在明确跨城时填，lodging_query仅在明确指定新住处时填。source.quote逐字引用原文，并包含城市/住处名称；相对日期可按到达日期转换。未明确变更时必须为空，不因路线、普通偏好或常识拆分住宿。换住处但未指定新住处时lodging_query为空，后端才推荐。不要将这些变更再复制到hard_requirements。
- TripRequest 是只读原始记录。不要输出或改写城市、到离时间、预算和备注；后端会原样保留这些字段，并绑定 request_id 与 requirements_id。
- explicit_return_buffer_s 是用户表单中的秒数约束；不要推测、改写或转换它。后端原样保留，并将显式值（包括0）交给 F02；未填写时才使用规则默认值。

理解示例：
- “必须去外滩”是 must_visit，引用原句；“不要去动物园”是 must_avoid，引用原句。
- “尽量少折返”是偏好，不是禁止某条路线。
"""


def parse_json(text):
    if not isinstance(text, str):
        raise TypeError("model response must be text")
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) < 3 or lines[-1].strip() != "```":
            raise ValueError("invalid fenced JSON")
        language = lines[0][3:].strip().lower()
        if language not in {"", "json"}:
            raise ValueError("unsupported fenced response")
        text = "\n".join(lines[1:-1]).strip()
        if not text:
            raise ValueError("empty fenced JSON")
    return json.loads(text)


class RequirementInterpreter:
    """用注入的 HelloAgents LLM 整理单个 TripRequest。"""

    def __init__(self, llm=None, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS):
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds)
                or timeout_seconds <= 0 or timeout_seconds > MAX_TIMEOUT_SECONDS):
            raise ValueError("timeout_seconds 必须大于0且不超过120秒")
        self.llm = llm
        self.timeout_seconds = float(timeout_seconds)
        self.last_usage = None
        self.model_calls = 0

    def interpret(
        self,
        request: TripRequest,
        *,
        deadline_monotonic: float | None = None,
        cancel_event: Event | None = None,
    ) -> RequirementSummary:
        self._reset_usage()
        if not isinstance(request, TripRequest):
            raise TypeError("request 必须是 planning_v4.TripRequest")
        local_deadline = deadline_after(time.monotonic(), self.timeout_seconds)
        if deadline_monotonic is not None:
            if (isinstance(deadline_monotonic, bool)
                    or not isinstance(deadline_monotonic, (int, float))
                    or not math.isfinite(deadline_monotonic)):
                raise ValueError("deadline_monotonic 必须是有限时间戳")
            local_deadline = min(local_deadline, float(deadline_monotonic))

        if self.llm is not None:
            llm = self.llm
        else:
            try:
                llm = get_llm()
            except Exception as exc:
                code, message = self._model_error(exc)
                if code == "MODEL_CALL_FAILED":
                    code, message = "MODEL_INITIALIZATION_FAILED", "需求整理模型初始化失败"
                raise PlanningError(code, message, step="requirement_model_initialization") from None
        base_messages = self._messages(request)
        safe_context = {
            "request_id": request.request_id,
            "model": getattr(llm, "model", None),
            "timeout_seconds": wait_timeout(self.timeout_seconds),
        }

        with span("agent.requirement_interpretation", "agent", safe_context) as agent_record:
            messages = list(base_messages)
            for attempt in range(MAX_FORMAT_REPAIRS + 1):
                if cancel_event is not None and cancel_event.is_set():
                    raise PlanningError(
                        "REQUIREMENT_MODEL_CANCELLED", "需求整理已取消", step="requirement_model_call",
                    )
                remaining = local_deadline - time.monotonic()
                if remaining <= 0:
                    raise PlanningError(
                        "REQUIREMENT_MODEL_TIMEOUT", "需求整理模型调用超时", step="requirement_model_call",
                    )
                draft, response_or_repair = self._invoke_and_validate(
                    llm, messages, attempt + 1,
                    request_timeout=min(self.timeout_seconds, remaining),
                )
                if draft is None:
                    messages = response_or_repair
                    continue

                with span("validation.requirement_sources", "validation") as validation_record:
                    try:
                        validate_requirement_sources(request, draft.hard_requirements)
                        from app.services.planning_lodging_policy import validate_lodging_changes
                        validate_lodging_changes(request, draft.lodging_changes)
                        validation_record.output = {"valid": True}
                    except ValueError:
                        validation_record.output = {"valid": False, "code": "REQUIREMENT_SOURCE_INVALID"}
                        raise PlanningError(
                            "REQUIREMENT_SOURCE_INVALID",
                            "硬性需求来源与原始请求不匹配",
                            step="requirement_source_validation",
                        ) from None

                # 指定酒店已有结构化输入，不再转换成通用must_include交给派发。
                hotel_requirements = tuple(item for item in draft.hard_requirements
                                           if item.source.field == "lodging_query")
                if hotel_requirements:
                    with span("requirements.structured_lodging", "validation", {}) as record:
                        record.output = {"outcome": "use_structured_hotel", "removed_duplicate_count": len(hotel_requirements)}
                draft = draft.model_copy(update={"hard_requirements": tuple(
                    item for item in draft.hard_requirements if item.source.field != "lodging_query"
                )})
                summary = RequirementSummary(
                    requirements_id=f"requirements_{uuid4().hex}",
                    request_id=request.request_id,
                    **draft.model_dump(),
                )
                agent_record.output = summary.model_dump(mode="json")
                return summary

            raise AssertionError("format repair loop exceeded its fixed limit")

    @staticmethod
    def _messages(request: TripRequest) -> list[dict[str, str]]:
        payload = {
            "schema": RequirementDraft.model_json_schema(),
            "trip_request": request.model_dump(mode="json"),
        }
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]

    def _invoke_and_validate(self, llm, messages, attempt: int, *, request_timeout: float):
        model_name = getattr(llm, "model", None)
        safe_context = {"model": model_name, "attempt": attempt, "timeout_seconds": wait_timeout(request_timeout)}
        with span("llm.requirement_interpretation", "llm", safe_context) as llm_record:
            try:
                response, usage = self._invoke(llm, messages, request_timeout=request_timeout)
                self.model_calls += 1
                llm_record.usage = token_usage(usage)
                self._accumulate_usage(usage)
            except Exception as exc:
                self.model_calls += 1
                self._accumulate_usage(None)
                code, message = self._model_error(exc)
                llm_record.output = {"status": "error", "code": code}
                raise PlanningError(code, message, step="requirement_model_call") from None

            try:
                if not isinstance(response, str):
                    raise ValueError("model response must be text")
                draft = RequirementDraft.model_validate(parse_json(response))
            except (AttributeError, ValueError, TypeError, ValidationError):
                issues = self._validation_locations(response)
                llm_record.output = {"status": "invalid", "code": "REQUIREMENT_OUTPUT_INVALID",
                                     "issue_count": len(issues), "diagnostics": [
                                         {"code": item["type"], "path": safe_path(item["loc"])}
                                         for item in issues[:8]],
                                     "diagnostics_truncated": len(issues) > 8}
                if attempt <= MAX_FORMAT_REPAIRS:
                    issues = self._validation_locations(response)
                    repair_messages = [
                        *messages,
                        {"role": "assistant", "content": response if isinstance(response, str) else ""},
                        {
                            "role": "user",
                            "content": "上次回答的 JSON 格式或字段结构不符合 schema。请只修复格式/结构，保留原有语义，不添加推断；错误位置和类型如下："
                            + json.dumps(issues, ensure_ascii=False),
                        },
                    ]
                    # 返回修复上下文给下一次固定迭代；原响应只留在内存中，不进入日志。
                    return None, repair_messages
                raise PlanningError(
                    "REQUIREMENT_OUTPUT_INVALID",
                    "需求整理输出的 JSON 格式或字段结构无效",
                    step="requirement_output_validation",
                ) from None

            llm_record.output = {"status": "valid", "hard_requirement_count": len(draft.hard_requirements)}
            return draft, response

    def _invoke(self, llm, messages, *, request_timeout: float):
        invoke_with_usage = getattr(llm, "invoke_with_usage", None)
        if not callable(invoke_with_usage):
            raise TypeError("注入的 LLM 必须提供 invoke_with_usage")
        return invoke_with_usage(
            messages,
            temperature=0,
            request_timeout=request_timeout,
            max_retries=0,
        )

    @staticmethod
    def _validation_locations(response):
        try:
            parsed = parse_json(response)
        except (AttributeError, ValueError, TypeError):
            return [{"loc": [], "type": "invalid_json"}]
        try:
            RequirementDraft.model_validate(parsed)
        except ValidationError as exc:
            return [
                {"loc": list(error["loc"]), "type": error["type"]}
                for error in exc.errors(include_input=False)
            ]
        except (ValueError, TypeError):
            return [{"loc": [], "type": "invalid_structure"}]
        return [{"loc": [], "type": "invalid_json"}]

    @staticmethod
    def _model_error(exc):
        seen = set()
        current = exc
        names = set()
        statuses = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            names.add(type(current).__name__)
            status = getattr(current, "status_code", None)
            if status is not None:
                statuses.add(status)
            current = current.__cause__ or current.__context__

        if names & {"APITimeoutError", "TimeoutException", "TimeoutError", "ConnectTimeout", "ReadTimeout"}:
            return "MODEL_TIMEOUT", "需求整理模型调用超时"
        if names & {"AuthenticationError", "PermissionDeniedError"} or statuses & {401, 403}:
            return "MODEL_AUTHENTICATION_FAILED", "需求整理模型认证失败"
        return "MODEL_CALL_FAILED", "需求整理模型调用失败"

    def _accumulate_usage(self, usage):
        current = token_usage(usage)
        keys = ("input_tokens", "output_tokens", "total_tokens")
        for key in keys:
            value = current.get(key) if current else None
            if value is None:
                self._usage_known[key] = False
            else:
                self._usage_totals[key] += value
        if not any(self._usage_known[key] for key in keys):
            self.last_usage = None
        else:
            self.last_usage = {key: self._usage_totals[key] if self._usage_known[key] else None for key in keys}

    def _reset_usage(self):
        self.last_usage = None
        self.model_calls = 0
        self._usage_totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        self._usage_known = {"input_tokens": True, "output_tokens": True, "total_tokens": True}


def interpret_requirements(request: TripRequest, llm=None, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS):
    """可直接调用的独立入口；传入 llm 可在测试中完全离线运行。"""
    return RequirementInterpreter(llm=llm, timeout_seconds=timeout_seconds).interpret(request)
