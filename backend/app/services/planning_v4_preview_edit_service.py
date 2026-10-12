"""持久化 v4 按日编辑，并协调可恢复的相邻边查询。"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import asdict
import logging
from typing import Any, Callable

from app.models.planning_v4.facts import FactStore
from app.models.planning_v4.plan import AddActivity, EditCommand, ItineraryPreview, PlanDraft, RouteModeResult
from app.models.planning_v4_preview import (
    DestinationSelectionRequest,
    PreviewEditJobResponse,
    PreviewEditResponse,
    PreviewRouteJobStatus,
    PreviewSnapshot,
)
from app.models.preview_storage import (
    IdempotencyConflict,
    PreviewAccessError,
    PreviewConflict,
    PreviewIdentityConflict,
    PreviewSnapshotUnsupported,
    StoredPreview,
)
from app.services.current_route_service import CurrentRouteError, CurrentRouteService
from app.services.destination_candidate_repository import DestinationCandidateNotFound
from app.services.destination_search_service import DestinationCandidateBundle, DestinationSearchService
from app.services.planning_v4_edit_service import PlanningV4EditError, PlanningV4EditService
from app.services.planning_v4_validation import build_day_summaries, validate_daily_plan
from app.services.planning_v4_validator import ContractValidationError, validate_preview
from app.services.reference_cost import aggregate_reference_costs
from app.services.observation_service import span
from app.services.task_service import TaskError


_FATAL_ROUTE_CODES = frozenset({
    "input_type_invalid",
    "request_id_mismatch",
    "rule_version_mismatch",
    "revision_invalid",
    "input_contract_invalid",
    "route_context_binding_mismatch",
    "route_cache_plan_mismatch",
    "route_result_contract_invalid",
    "requested_live_leg_scope_invalid",
})
_LOGGER = logging.getLogger(__name__)


def _append_destination_bundle(
    snapshot: PreviewSnapshot,
    bundle: DestinationCandidateBundle,
) -> PreviewSnapshot:
    """只按完全相同的不可变 ID 内容去重，不覆盖预览中的事实或候选。"""
    def merge(existing, additions, identity: Callable[[Any], str], label: str):
        values = list(existing)
        by_id = {identity(value): value for value in existing}
        for value in additions:
            value_id = identity(value)
            previous = by_id.get(value_id)
            if previous is not None:
                if previous != value:
                    raise PreviewIdentityConflict(f"目的地候选{label} ID 内容冲突")
                continue
            values.append(value)
            by_id[value_id] = value
        return tuple(values)

    old_preview = snapshot.preview
    sources = merge(
        old_preview.facts.sources,
        bundle.sources,
        lambda source: source.tool_call_id,
        "来源",
    )
    places = merge(
        old_preview.facts.places,
        (bundle.place,),
        lambda place: place.fact_id,
        "事实",
    )
    candidates = merge(
        old_preview.candidates,
        (bundle.candidate,),
        lambda candidate: candidate.candidate_id,
        "候选",
    )
    facts = FactStore.model_validate({
        "sources": [source.model_dump(mode="json") for source in sources],
        "places": [place.model_dump(mode="json") for place in places],
        "weather": [fact.model_dump(mode="json") for fact in old_preview.facts.weather],
        "routes": [fact.model_dump(mode="json") for fact in old_preview.facts.routes],
    })
    preview_payload = old_preview.model_dump(mode="json")
    preview_payload["facts"] = facts.model_dump(mode="json")
    preview_payload["candidates"] = [candidate.model_dump(mode="json") for candidate in candidates]
    preview = ItineraryPreview.model_validate(preview_payload)
    return PreviewSnapshot.model_validate({
        "preview": preview.model_dump(mode="json"),
        "cost": snapshot.cost.model_dump(mode="json"),
    })


@contextmanager
def _best_effort_task_observation(observation, task_id: str):
    """Bind spans to the originating task without making telemetry a dependency."""
    if observation is None:
        yield
        return
    try:
        scope = observation.task(task_id)
        scope.__enter__()
    except Exception as exc:
        _LOGGER.warning("could not bind route observation to task %s (%s)", task_id, type(exc).__name__)
        yield
        return
    try:
        yield
    except BaseException as exc:
        try:
            scope.__exit__(type(exc), exc, exc.__traceback__)
        except Exception as observation_error:
            _LOGGER.warning(
                "could not close route observation for task %s (%s)",
                task_id,
                type(observation_error).__name__,
            )
        raise
    else:
        try:
            scope.__exit__(None, None, None)
        except Exception as exc:
            _LOGGER.warning(
                "could not close route observation for task %s (%s)", task_id, type(exc).__name__,
            )


class PlanningV4PreviewEditService:
    """API 写入、幂等、局部路线后台任务与重启恢复。"""

    def __init__(
        self,
        preview_task_service,
        repository,
        *,
        route_service: CurrentRouteService | None = None,
        edit_service: PlanningV4EditService | None = None,
        destination_search_service: DestinationSearchService | None = None,
        route_deadline_seconds: float = 120,
    ):
        if route_deadline_seconds <= 0:
            raise ValueError("route_deadline_seconds 必须大于 0")
        self.preview_task_service = preview_task_service
        self.repository = repository
        self.route_service = route_service or CurrentRouteService()
        self.edit_service = edit_service or PlanningV4EditService()
        self.destination_search_service = destination_search_service
        self.route_deadline_seconds = float(route_deadline_seconds)
        self.accepting = True
        self.workers: dict[str, asyncio.Task] = {}
        self.scheduler_locks: dict[str, asyncio.Lock] = {}

    async def edit(
        self,
        task_id: str,
        access_token: str,
        command: EditCommand,
        *,
        expected_store_version: int,
        _prepare_edit: Callable[[PreviewSnapshot, EditCommand], tuple[PreviewSnapshot, EditCommand]] | None = None,
    ) -> PreviewEditResponse:
        if not self.accepting:
            raise TaskError("SERVICE_STOPPING", "服务正在关闭，暂不能编辑", 503)
        await self.preview_task_service._require_ready_job(task_id, access_token)

        def transform(snapshot: PreviewSnapshot, inherited_days: tuple[str, ...]):
            edit_snapshot, edit_command = (
                _prepare_edit(snapshot, command) if _prepare_edit is not None else (snapshot, command)
            )
            departure_fact_id = self._verified_departure_fact_id(edit_snapshot.preview)
            execution = self.edit_service.apply(
                edit_snapshot.preview,
                edit_command,
                verified_departure_fact_id=departure_fact_id,
                inherited_pending_day_ids=inherited_days,
            )
            updated = self._refresh_preview(
                execution.preview,
                draft=execution.preview.draft,
                facts=execution.preview.facts,
                verified_departure_fact_id=departure_fact_id,
            )
            return updated, execution.changed_activity_ids, execution.changed_leg_ids

        try:
            write = await asyncio.to_thread(
                self.repository.apply_edit,
                access_token,
                task_id,
                command,
                expected_store_version=expected_store_version,
                transform=transform,
            )
        except PreviewConflict as exc:
            error = TaskError("PREVIEW_VERSION_CONFLICT", str(exc), 409)
            error.details.update({
                "current_store_version": exc.current_store_version,
                "current_plan_revision": exc.current_plan_revision,
            })
            raise error from exc
        except IdempotencyConflict as exc:
            raise TaskError("IDEMPOTENCY_CONFLICT", str(exc), 409) from exc
        except PreviewAccessError as exc:
            raise TaskError("TASK_NOT_FOUND", "预览任务不存在", 404) from exc
        except PreviewSnapshotUnsupported as exc:
            raise TaskError("PREVIEW_CONTRACT_UNSUPPORTED", str(exc), 409) from exc
        except PlanningV4EditError as exc:
            status = 409 if exc.code in {"stale_edit_revision", "plan_mismatch"} else 422
            raise TaskError(exc.code.upper(), "编辑命令无法应用到当前预览", status) from exc
        except DestinationCandidateNotFound as exc:
            raise TaskError(
                "DESTINATION_CANDIDATE_UNAVAILABLE",
                "候选已失效，请重新搜索后添加",
                422,
            ) from exc
        except (PreviewIdentityConflict, ValueError) as exc:
            raise TaskError("INVALID_PREVIEW_EDIT", "编辑结果未通过预览契约校验", 422) from exc
        except Exception as exc:
            raise TaskError("PREVIEW_STORAGE_UNAVAILABLE", "保存编辑预览失败", 503) from exc

        route_job = write.route_job
        if route_job is not None and route_job.status == "pending":
            try:
                await self._ensure_worker(route_job.plan_id)
            except Exception as exc:
                _LOGGER.warning(
                    "edit %s committed; route job remains queued (%s)",
                    route_job.calculation_id,
                    type(exc).__name__,
                )
        envelope = self.preview_task_service._preview_envelope(task_id, write.record)
        return PreviewEditResponse(
            envelope=envelope,
            job=self._job_model(route_job) if route_job is not None else None,
            changed_activity_ids=write.changed_activity_ids,
            changed_leg_ids=write.changed_leg_ids,
            replayed=write.replayed,
            receipt_store_version=write.receipt_store_version,
        )

    async def add_destination_candidate(
        self,
        task_id: str,
        access_token: str,
        request: DestinationSelectionRequest,
    ) -> PreviewEditResponse:
        """在编辑仓库同一事务内解析token、接入事实闭包并加入活动。"""
        destination_search = self.destination_search_service
        if destination_search is None:
            raise TaskError("DESTINATION_SELECT_UNAVAILABLE", "目的地选择暂不可用", 503)
        ready_job = await self.preview_task_service._require_ready_job(task_id, access_token)
        operation = AddActivity(
            type="add_activity",
            client_ref=request.client_ref,
            candidate_ref=request.candidate_token,
            slot_id=request.slot_id,
            day_id=request.day_id,
        )
        command = EditCommand(
            plan_id=ready_job["plan_id"],
            day_id=request.day_id,
            base_revision=request.expected_plan_revision,
            command_id=request.command_id,
            operations=(operation,),
        )

        def prepare(snapshot: PreviewSnapshot, incoming: EditCommand):
            bundle = destination_search.resolve_candidate(
                request.candidate_token,
                task_id=task_id,
                plan_id=snapshot.preview.draft.plan_id,
                access_token=access_token,
            )
            if bundle.task_id != task_id or bundle.plan_id != snapshot.preview.draft.plan_id:
                raise PlanningV4EditError("destination_candidate_binding_mismatch")
            prepared_snapshot = _append_destination_bundle(snapshot, bundle)
            source_operation = incoming.operations[0]
            if not isinstance(source_operation, AddActivity):
                raise PlanningV4EditError("destination_selection_operation_invalid")
            prepared_operation = source_operation.model_copy(update={
                "candidate_ref": bundle.candidate.candidate_id,
            })
            prepared_command = EditCommand.model_validate({
                **incoming.model_dump(mode="json"),
                "operations": (prepared_operation.model_dump(mode="json"),),
            })
            return prepared_snapshot, prepared_command

        return await self.edit(
            task_id,
            access_token,
            command,
            expected_store_version=request.expected_store_version,
            _prepare_edit=prepare,
        )

    async def get_job(
        self, task_id: str, access_token: str, calculation_id: str,
    ) -> PreviewEditJobResponse:
        await self.preview_task_service._require_ready_job(task_id, access_token)
        try:
            job = await asyncio.to_thread(
                self.repository.get_route_job,
                access_token,
                task_id,
                calculation_id,
            )
        except PreviewSnapshotUnsupported as exc:
            raise TaskError("PREVIEW_CONTRACT_UNSUPPORTED", str(exc), 409) from exc
        except Exception as exc:
            raise TaskError("PREVIEW_STORAGE_UNAVAILABLE", "路线任务状态暂不可用", 503) from exc
        if job is None:
            raise TaskError("ROUTE_JOB_NOT_FOUND", "路线重算任务不存在", 404)
        if job.status == "running":
            current_worker = self.workers.get(job.plan_id)
            if current_worker is None or current_worker.done():
                try:
                    await asyncio.to_thread(
                        self.repository.requeue_stale_running_route_job,
                        access_token,
                        task_id,
                        calculation_id,
                    )
                except Exception as exc:
                    _LOGGER.warning(
                        "route job %s could not be recovered (%s)",
                        job.calculation_id,
                        type(exc).__name__,
                    )
                job = await asyncio.to_thread(
                    self.repository.get_route_job,
                    access_token,
                    task_id,
                    calculation_id,
                )
                if job is None:
                    raise TaskError("ROUTE_JOB_NOT_FOUND", "路线重算任务不存在", 404)
        if job.status == "pending":
            try:
                await self._ensure_worker(job.plan_id)
            except Exception as exc:
                _LOGGER.warning(
                    "route job %s remains queued after scheduling error (%s)",
                    job.calculation_id,
                    type(exc).__name__,
                )
            refreshed_job = await asyncio.to_thread(
                self.repository.get_route_job,
                access_token,
                task_id,
                calculation_id,
            )
            if refreshed_job is not None:
                job = refreshed_job
        envelope = await self.preview_task_service.preview(task_id, access_token)
        if envelope is None:
            raise TaskError("PREVIEW_UNAVAILABLE", "编辑预览暂不可用", 503)
        return PreviewEditJobResponse(envelope=envelope, job=self._job_model(job))

    async def recover_pending_jobs(self) -> None:
        """启动时恢复仓库判定仍与当前 pending 快照绑定的路线任务。"""
        for plan_id in await asyncio.to_thread(self.repository.list_pending_route_plans):
            await self._ensure_worker(plan_id)

    async def schedule_for_record(self, record: StoredPreview) -> None:
        """地点初版或还原快照落盘后，安排同事务登记的相邻路线任务。"""
        if not self.accepting:
            return
        job = await asyncio.to_thread(
            self.repository.get_route_job_by_plan,
            record.preview.draft.plan_id,
            record.preview.calculation.calculation_id,
        )
        if job is not None and job.status == "pending":
            await self._ensure_worker(job.plan_id)

    async def fail_scheduling_for_record(self, record: StoredPreview) -> None:
        """启动失败只终结该快照的任务，不触碰后续编辑产生的任务。"""
        claim = await asyncio.to_thread(
            self.repository.claim_route_job, record.preview.draft.plan_id,
            record.preview.calculation.calculation_id,
        )
        if claim is not None:
            await self._publish_failed(
                claim, claim.record.preview, "ROUTE_SCHEDULING_FAILED", "provider_error",
            )

    async def _ensure_worker(self, plan_id: str) -> None:
        lock = self.scheduler_locks.setdefault(plan_id, asyncio.Lock())
        async with lock:
            current = self.workers.get(plan_id)
            if current is not None and not current.done():
                return
            if not await asyncio.to_thread(self.repository.has_pending_route_job, plan_id):
                return
            task = asyncio.create_task(self._drain_plan(plan_id))
            self.workers[plan_id] = task

    async def _drain_plan(self, plan_id: str) -> None:
        current_task = asyncio.current_task()
        try:
            while self.accepting:
                claim = await asyncio.to_thread(self.repository.claim_route_job, plan_id)
                if claim is None:
                    break
                await self._run_claim(claim)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 单个计划后台协调异常不能影响 API；保留可编辑快照并终止本 worker。
            _LOGGER.warning("v4 route worker failed for plan %s (%s)", plan_id, type(exc).__name__)
        finally:
            lock = self.scheduler_locks.setdefault(plan_id, asyncio.Lock())
            async with lock:
                if self.workers.get(plan_id) is current_task:
                    self.workers.pop(plan_id, None)
                if self.accepting and await asyncio.to_thread(
                    self.repository.has_pending_route_job, plan_id,
                ):
                    task = asyncio.create_task(self._drain_plan(plan_id))
                    self.workers[plan_id] = task

    async def _run_claim(self, claim) -> None:
        record = claim.record
        snapshot = record.snapshot
        if snapshot is None:
            await self._publish_failed(claim, record.preview, "ROUTE_SOURCE_INCOMPLETE", "invalid_response")
            return
        job = claim.job
        preview = snapshot.preview
        draft = preview.draft
        facts = preview.facts
        verified_departure_fact_id = self._verified_departure_fact_id(preview)
        shared_contexts = getattr(self.preview_task_service, "route_contexts", {})
        context = shared_contexts.get(job.plan_id)
        owns_context = False

        failed_days: list[tuple[str, str, str]] = []
        try:
            deadline = asyncio.get_running_loop().time() + self.route_deadline_seconds
            for day_index, day_id in enumerate(job.affected_day_ids):
                pending_leg_ids = tuple(
                    leg.leg_id for leg in draft.legs
                    if leg.day_id == day_id and leg.leg_id in preview.calculation.pending_leg_ids
                    and any(mode.status == "pending" for mode in leg.mode_results)
                )
                if not pending_leg_ids:
                    continue
                remaining_seconds = deadline - asyncio.get_running_loop().time()
                if remaining_seconds <= 0:
                    for expired_day in job.affected_day_ids[day_index:]:
                        failed_days.append((
                            expired_day,
                            "ROUTE_RECALCULATION_TIMEOUT",
                            "timeout",
                        ))
                        draft = self._mark_day_failed(draft, expired_day, "timeout")
                    break
                try:
                    context, owns_context = await self._ensure_route_context(
                        preview,
                        context,
                        owns_context=owns_context,
                        shared_contexts=shared_contexts,
                        plan_id=job.plan_id,
                    )
                    remaining_seconds = deadline - asyncio.get_running_loop().time()
                    if remaining_seconds <= 0:
                        failed_days.append((
                            day_id,
                            "ROUTE_RECALCULATION_TIMEOUT",
                            "timeout",
                        ))
                        draft = self._mark_day_failed(draft, day_id, "timeout")
                        for expired_day in job.affected_day_ids[day_index + 1:]:
                            failed_days.append((
                                expired_day,
                                "ROUTE_RECALCULATION_TIMEOUT",
                                "timeout",
                            ))
                            draft = self._mark_day_failed(draft, expired_day, "timeout")
                        break
                    observation = getattr(
                        getattr(self.preview_task_service, "task_service", None),
                        "observation",
                        None,
                    )
                    with _best_effort_task_observation(observation, job.task_id), span(
                        "planning.v4.current_routes" if job.calculation_id.startswith("generation:") else "planning.v4.edit_routes",
                        "planning",
                        {
                            "task_id": job.task_id,
                            "plan_id": job.plan_id,
                            "day_id": day_id,
                            "calculation_id": job.calculation_id,
                            "plan_revision": draft.revision,
                        },
                    ):
                        execution = await asyncio.wait_for(
                            self.route_service.run_async(
                                preview.planning_input,
                                draft,
                                facts,
                                verified_departure_fact_id=verified_departure_fact_id,
                                route_context=context,
                                only_day_id=day_id,
                                only_leg_ids=pending_leg_ids,
                            ),
                            timeout=remaining_seconds,
                        )
                    self._validate_route_result(draft, execution, day_id, pending_leg_ids)
                    draft, facts = execution.draft, execution.fact_store
                    provider_errors = [
                        mode
                        for leg in execution.draft.legs if leg.leg_id in pending_leg_ids
                        for mode in leg.mode_results if mode.status == "error"
                    ]
                    if provider_errors:
                        category = provider_errors[0].error_category or "provider_error"
                        failed_days.append((day_id, "ROUTE_PROVIDER_ERROR", category))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    code, category = self._route_error(exc)
                    _LOGGER.warning(
                        "v4 route recalculation failed for day %s (%s)", day_id, code,
                    )
                    failed_days.append((day_id, code, category))
                    draft = self._mark_day_failed(draft, day_id, category)
                    if isinstance(exc, CurrentRouteError) and exc.code in _FATAL_ROUTE_CODES:
                        for remaining in job.affected_day_ids:
                            if remaining != day_id and not any(item[0] == remaining for item in failed_days):
                                failed_days.append((remaining, "ROUTE_INPUT_INVALID", "invalid_response"))
                                draft = self._mark_day_failed(draft, remaining, "invalid_response")
                        break

            if not failed_days:
                retained_errors = [
                    (leg.day_id, "ROUTE_PROVIDER_ERROR", mode.error_category or "provider_error")
                    for leg in draft.legs for mode in leg.mode_results if mode.status == "error"
                ]
                failed_days.extend(retained_errors)
            final_snapshot = self._refresh_preview(
                preview,
                draft=draft,
                facts=facts,
                route_state="failed" if failed_days else "completed",
                pending_leg_ids=(),
                verified_departure_fact_id=verified_departure_fact_id,
            )
            await asyncio.to_thread(
                self.repository.publish_route_job,
                job.plan_id,
                job.calculation_id,
                final_snapshot,
                status="failed" if failed_days else "completed",
                error_code=(failed_days[0][1] if failed_days else None),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # A malformed route result cannot replace the accepted edit. Turn every
            # still-pending affected leg into an explicit error and publish by CAS.
            try:
                await self._publish_failed(
                    claim,
                    preview,
                    "ROUTE_RESULT_INVALID",
                    "invalid_response",
                    verified_departure_fact_id=verified_departure_fact_id,
                )
            except Exception as exc:
                _LOGGER.warning(
                    "could not publish failed v4 route job %s (%s)", job.calculation_id, type(exc).__name__,
                )
        finally:
            if owns_context and context is not None:
                await self._close_route_context(context)

    async def _publish_failed(
        self,
        claim,
        preview: ItineraryPreview,
        error_code: str,
        error_category: str,
        *,
        draft: PlanDraft | None = None,
        facts=None,
        verified_departure_fact_id: str | None = None,
    ) -> None:
        failed_draft = draft or preview.draft
        for day_id in claim.job.affected_day_ids:
            failed_draft = self._mark_day_failed(failed_draft, day_id, error_category)
        final_snapshot = self._refresh_preview(
            preview,
            draft=failed_draft,
            facts=facts or preview.facts,
            route_state="failed",
            pending_leg_ids=(),
            verified_departure_fact_id=(
                verified_departure_fact_id
                if verified_departure_fact_id is not None
                else self._verified_departure_fact_id(preview)
            ),
        )
        await asyncio.to_thread(
            self.repository.publish_route_job,
            claim.job.plan_id,
            claim.job.calculation_id,
            final_snapshot,
            status="failed",
            error_code=error_code,
        )

    def _refresh_preview(
        self,
        preview: ItineraryPreview,
        *,
        draft: PlanDraft,
        facts,
        route_state: str | None = None,
        pending_leg_ids: tuple[str, ...] | None = None,
        verified_departure_fact_id: str | None = None,
    ) -> PreviewSnapshot:
        calculation_updates: dict[str, Any] = {}
        if route_state is not None:
            calculation_updates["current_routes"] = route_state
        if pending_leg_ids is not None:
            calculation_updates["pending_leg_ids"] = pending_leg_ids
        calculation = preview.calculation.model_copy(update=calculation_updates)
        updated_preview = preview.model_copy(update={
            "draft": draft,
            "facts": facts,
            "calculation": calculation,
        }, deep=True)
        return self._snapshot_with_calculations(
            updated_preview,
        )

    @staticmethod
    def _snapshot_with_calculations(
        preview: ItineraryPreview,
    ) -> PreviewSnapshot:
        report = validate_daily_plan(
            preview.planning_input,
            preview.draft,
            preview.facts,
            preview.candidates,
            repair_states=preview.validation.repair_states,
        )
        notices = tuple(
            issue.model_copy(update={"based_on_revision": preview.draft.revision})
            for issue in preview.validation.issues
            if issue.code in {"INITIAL_ACTIVITY_OMITTED", "DISPATCH_COLLECTION_PARTIAL"}
        )
        if notices:
            report = report.model_copy(update={
                "issues": (*report.issues, *notices),
                "days": tuple(day.model_copy(update={
                    "issue_ids": (*day.issue_ids, *(issue.issue_id for issue in notices if issue.day_id == day.day_id)),
                }) for day in report.days),
            })
        cost = aggregate_reference_costs(
            preview.planning_input,
            preview.draft,
            preview.facts,
        )
        cost_by_day = {day.day_id: day for day in cost.days}
        summaries = build_day_summaries(preview.planning_input, preview.draft, preview.facts)
        summaries = tuple(
            summary.model_copy(update={
                "cost_status": (
                    "available" if cost_by_day[summary.day_id].status in {"all_known", "known_partial"}
                    else "missing"
                ),
                "known_cost_subtotal": str(
                    cost_by_day[summary.day_id].known_sightseeing_meal_per_person_subtotal
                ),
            })
            for summary in summaries
        )
        updated = preview.model_copy(update={
            "validation": report,
            "day_summaries": summaries,
        }, deep=True)
        try:
            validate_preview(updated)
        except ContractValidationError as exc:
            raise ValueError("preview_after_edit_invalid") from exc
        return PreviewSnapshot(preview=updated, cost=cost)

    @staticmethod
    def _verified_departure_fact_id(preview: ItineraryPreview) -> str | None:
        """Use only a uniquely bound departure endpoint already present in the draft."""
        departure_origins = [
            leg for leg in preview.draft.legs if leg.from_endpoint.kind == "departure"
        ]
        departure_destinations = [
            leg.to_endpoint for leg in preview.draft.legs
            if leg.to_endpoint.kind == "departure"
        ]
        if departure_origins:
            raise ValueError("verified_departure_endpoint_direction_invalid")
        if not departure_destinations:
            return None
        reference_ids = {endpoint.reference_id for endpoint in departure_destinations}
        if len(reference_ids) != 1:
            raise ValueError("verified_departure_endpoint_ambiguous")
        fact_id = next(iter(reference_ids))
        places = [place for place in preview.facts.places if place.fact_id == fact_id]
        endpoint_pois = {endpoint.poi_id for endpoint in departure_destinations}
        if (
            len(places) != 1
            or not preview.planning_input.request.departure_place_query
            or not preview.planning_input.request.departure_place_query.strip()
            or places[0].location is None
            or not places[0].city
            or endpoint_pois != {places[0].poi_id}
        ):
            raise ValueError("verified_departure_endpoint_invalid")
        return fact_id

    async def _ensure_route_context(
        self,
        preview: ItineraryPreview,
        context,
        *,
        owns_context: bool,
        shared_contexts: dict,
        plan_id: str,
    ):
        """Replace a cancelled context without ever clearing its shared cancel event."""
        cancel_event = getattr(context, "cancel_event", None) if context is not None else None
        cancelled = bool(cancel_event is not None and cancel_event.is_set())
        if context is not None and not cancelled:
            return context, owns_context

        factory = getattr(self.route_service, "create_route_context", None)
        if factory is None:
            if cancelled:
                await self._close_route_context(context)
            return None, False

        if cancelled:
            await self._close_route_context(context)
        replacement = factory(preview.planning_input, plan_id)
        if replacement is None:
            return None, False

        # A retained generation context belongs to PreviewTaskService and is closed
        # there. A context created only for this worker belongs to this coordinator.
        shared_context = shared_contexts.get(plan_id)
        if preview.calculation.calculation_id.startswith("generation:"):
            shared_contexts[plan_id] = replacement
            return replacement, False
        if context is shared_context or cancelled:
            if shared_context is not None:
                shared_contexts[plan_id] = replacement
                return replacement, False
        return replacement, True

    @staticmethod
    async def _close_route_context(context) -> None:
        close = getattr(context, "aclose", None) if context is not None else None
        if close is None:
            return
        try:
            result = close()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            pass

    @staticmethod
    def _validate_route_result(
        before: PlanDraft, execution, day_id: str, only_leg_ids: tuple[str, ...],
    ) -> None:
        if (
            execution.plan_id != before.plan_id
            or execution.request_id != before.request_id
            or execution.based_on_revision != before.revision
            or execution.draft.plan_id != before.plan_id
            or execution.draft.revision != before.revision
            or execution.draft.days != before.days
        ):
            raise CurrentRouteError("route_result_contract_invalid")
        before_legs = {leg.leg_id: leg for leg in before.legs}
        after_legs = {leg.leg_id: leg for leg in execution.draft.legs}
        selected_ids = set(only_leg_ids)
        if before_legs.keys() != after_legs.keys() or len(after_legs) != len(execution.draft.legs):
            raise CurrentRouteError("route_result_contract_invalid")
        for leg_id, previous in before_legs.items():
            updated = after_legs[leg_id]
            if leg_id not in selected_ids and updated != previous:
                raise CurrentRouteError("route_result_contract_invalid")
            if (
                updated.day_id != previous.day_id
                or updated.plan_revision != previous.plan_revision
                or updated.from_endpoint != previous.from_endpoint
                or updated.to_endpoint != previous.to_endpoint
            ):
                raise CurrentRouteError("route_result_contract_invalid")
        if any(
            result.status == "pending"
            for leg in execution.draft.legs if leg.leg_id in selected_ids
            for result in leg.mode_results
        ):
            raise CurrentRouteError("route_result_contract_invalid")

    @staticmethod
    def _mark_day_failed(draft: PlanDraft, day_id: str, error_category: str) -> PlanDraft:
        legs = []
        for leg in draft.legs:
            if leg.day_id != day_id:
                legs.append(leg)
                continue
            if not any(result.status == "pending" for result in leg.mode_results):
                legs.append(leg)
                continue
            modes = tuple(
                result.model_copy(update={
                    "status": "error",
                    "route_fact_id": None,
                    "option_ids": (),
                    "error_category": error_category,
                }) if result.status == "pending" else result
                for result in leg.mode_results
            )
            legs.append(leg.model_copy(update={
                "mode_results": modes,
                "selected_mode": None,
                "selected_option_id": None,
                "selection_source": None,
            }))
        return draft.model_copy(update={"legs": tuple(legs)}, deep=True)

    @staticmethod
    def _route_error(exc: Exception) -> tuple[str, str]:
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            return "ROUTE_RECALCULATION_TIMEOUT", "timeout"
        if isinstance(exc, CurrentRouteError) and exc.code in _FATAL_ROUTE_CODES:
            return "ROUTE_INPUT_INVALID", "invalid_response"
        if isinstance(exc, CurrentRouteError) and exc.code == "route_result_contract_invalid":
            return "ROUTE_RESULT_INVALID", "invalid_response"
        return "ROUTE_RECALCULATION_FAILED", "provider_error"

    async def close(self) -> None:
        self.accepting = False
        tasks = list(self.workers.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(self.repository.requeue_route_jobs)

    @staticmethod
    def _job_model(job) -> PreviewRouteJobStatus:
        return PreviewRouteJobStatus.model_validate(asdict(job))
