"""按日后台补齐已选 POI 之间的备用路线事实。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, time
from inspect import isawaitable
from typing import Awaitable, Callable, Literal
from zoneinfo import ZoneInfo

from pydantic import Field

from app.models.planning_v4 import FactStore, MapCoordinate, PlanDraft, PlanningInput, PlaceFact, RouteFact
from app.models.planning_v4.base import ContractModel
from app.services.current_route_service import (
    CurrentRouteError,
    PlanRouteContext,
    RouteMode,
    _MODE_ORDER,
    _fact_matches_request,
    _route_fact_outcome,
    _route_query_key,
    _safe_error_category,
)
from app.services.current_route_service import _UncacheableRoute
from app.services.observation_service import span


BackfillModeStatus = Literal["available", "empty", "error"]
BackfillStatus = Literal["complete", "partial", "cancelled"]
ProgressCallback = Callable[["RouteBackfillProgress"], None | Awaitable[None]]
CurrentRevision = Callable[[], int]


class RouteBackfillModeResult(ContractModel):
    mode: RouteMode
    status: BackfillModeStatus
    route_fact_id: str | None = None
    duration_s: float | None = Field(default=None, ge=0)
    error_category: Literal[
        "invalid_request", "auth", "rate_limit", "timeout", "network",
        "provider_error", "invalid_response",
    ] | None = None
    time_basis: Literal["reference", "unspecified"]


class RouteBackfillPairResult(ContractModel):
    day_id: str
    origin_poi_id: str
    origin: MapCoordinate
    destination_poi_id: str
    destination: MapCoordinate
    modes: tuple[RouteBackfillModeResult, ...] = ()


class RouteBackfillProgress(ContractModel):
    plan_id: str
    based_on_revision: int = Field(ge=1)
    status: Literal["running", "complete", "partial", "cancelled"]
    completed_queries: int = Field(ge=0)
    total_queries: int = Field(ge=0)
    current_day_id: str | None = None
    current_mode: RouteMode | None = None


class RouteBackfillExecution(ContractModel):
    plan_id: str
    request_id: str
    based_on_revision: int = Field(ge=1)
    status: BackfillStatus
    completed_queries: int = Field(ge=0)
    total_queries: int = Field(ge=0)
    pair_results: tuple[RouteBackfillPairResult, ...] = ()
    route_facts: tuple[RouteFact, ...] = ()


@dataclass(frozen=True, slots=True)
class _PairJob:
    day_id: str
    day_date: date
    origin: PlaceFact
    destination: PlaceFact

    @property
    def identity(self) -> tuple[str, str, str, str, float, float, float, float]:
        assert self.origin.location is not None and self.destination.location is not None
        return (
            self.day_id,
            self.origin.poi_id,
            self.destination.poi_id,
            self.origin.location.coordinate_system,
            float(self.origin.location.lng),
            float(self.origin.location.lat),
            float(self.destination.location.lng),
            float(self.destination.location.lat),
        )


class RouteBackfillService:
    """逐项运行后台矩阵；单个plan每次只向共享executor提交一个后台查询。"""

    async def run_async(
        self,
        planning_input: PlanningInput,
        draft: PlanDraft,
        facts: FactStore,
        route_context: PlanRouteContext,
        *,
        progress_callback: ProgressCallback | None = None,
        current_revision: CurrentRevision | None = None,
    ) -> RouteBackfillExecution:
        if route_context is None:
            raise CurrentRouteError("route_context_required")
        if draft.request_id != planning_input.request.request_id:
            raise CurrentRouteError("request_id_mismatch")
        if draft.rule_version != planning_input.rules.rule_version:
            raise CurrentRouteError("rule_version_mismatch")
        route_context.assert_binding(planning_input, draft)
        route_context.register_facts(facts)

        async with route_context.backfill_lock:
            return await self._run_locked(
                planning_input, draft, facts, route_context,
                progress_callback=progress_callback,
                current_revision=current_revision,
            )

    async def _run_locked(
        self,
        planning_input: PlanningInput,
        draft: PlanDraft,
        facts: FactStore,
        route_context: PlanRouteContext,
        *,
        progress_callback: ProgressCallback | None,
        current_revision: CurrentRevision | None,
    ) -> RouteBackfillExecution:
        jobs = self._build_jobs(draft, facts)
        total = len(jobs) * len(_MODE_ORDER)
        completed = 0
        grouped: dict[tuple, list[RouteBackfillModeResult]] = {}
        route_facts: dict[str, RouteFact] = {}
        terminal_status: BackfillStatus = "complete"

        async def emit(
            status: Literal["running", "complete", "partial", "cancelled"],
            *, day_id: str | None = None, mode: RouteMode | None = None,
        ) -> None:
            if progress_callback is None:
                return
            progress = RouteBackfillProgress(
                plan_id=draft.plan_id,
                based_on_revision=draft.revision,
                status=status,
                completed_queries=completed,
                total_queries=total,
                current_day_id=day_id,
                current_mode=mode,
            )
            outcome = progress_callback(progress)
            if isawaitable(outcome):
                await outcome

        if not self._is_current(current_revision, draft.revision):
            terminal_status = "cancelled"
        else:
            await emit("running")
            for job in jobs:
                group = grouped.setdefault(job.identity, [])
                requested_at = datetime.combine(
                    job.day_date,
                    time(9, 0),
                    tzinfo=ZoneInfo(planning_input.request.timezone),
                )
                for mode in _MODE_ORDER:
                    if not self._is_current(current_revision, draft.revision):
                        terminal_status = "cancelled"
                        break
                    await emit("running", day_id=job.day_id, mode=mode)
                    result, fact = await self._query_mode(
                        planning_input, draft, route_context, job,
                        mode, requested_at,
                    )
                    # A late result is valid cache material, but the stale run
                    # must not return it to a preview/facts assembler.
                    if not self._is_current(current_revision, draft.revision):
                        terminal_status = "cancelled"
                        break
                    group.append(result)
                    completed += 1
                    if fact is not None:
                        route_facts.setdefault(fact.fact_id, fact)
                    if result.status == "error":
                        terminal_status = "partial"
                    await emit("running", day_id=job.day_id, mode=mode)
                if terminal_status == "cancelled":
                    break

        pair_results = tuple(
            RouteBackfillPairResult(
                day_id=job.day_id,
                origin_poi_id=job.origin.poi_id,
                origin=job.origin.location,
                destination_poi_id=job.destination.poi_id,
                destination=job.destination.location,
                modes=tuple(grouped.get(job.identity, ())),
            )
            for job in jobs
            if grouped.get(job.identity)
        )
        if terminal_status == "complete" and completed < total:
            terminal_status = "partial"
        await emit(terminal_status)
        return RouteBackfillExecution(
            plan_id=draft.plan_id,
            request_id=draft.request_id,
            based_on_revision=draft.revision,
            status=terminal_status,
            completed_queries=completed,
            total_queries=total,
            pair_results=pair_results,
            route_facts=tuple(route_facts.values()),
        )

    async def _query_mode(
        self,
        planning_input: PlanningInput,
        draft: PlanDraft,
        route_context: PlanRouteContext,
        job: _PairJob,
        mode: RouteMode,
        transit_departure_at: datetime,
    ) -> tuple[RouteBackfillModeResult, RouteFact | None]:
        assert job.origin.location is not None and job.destination.location is not None
        requested_at = transit_departure_at if mode == "transit" else None
        key = _route_query_key(
            planning_input, draft, mode, job.origin, job.destination,
            transit_departure_at=requested_at,
        )
        time_basis: Literal["reference", "unspecified"] = (
            "reference" if mode == "transit" else "unspecified"
        )
        with span("route.backfill_mode", "tool", {
            "plan_id": draft.plan_id,
            "based_on_revision": draft.revision,
            "day_id": job.day_id,
            "origin_poi_id": job.origin.poi_id,
            "destination_poi_id": job.destination.poi_id,
            "mode": mode,
            "time_basis": time_basis,
        }) as record:
            try:
                fact = await route_context.route_cache.get_or_load(key, priority="background")
            except _UncacheableRoute as exc:
                fact = exc.fact
                if fact is not None and _fact_matches_request(
                    fact, mode, job.origin, job.destination,
                ):
                    record.output = {"status": fact.status, "cache": "bypassed"}
                    return self._mode_result(mode, fact, time_basis), fact
                category = exc.error_category or "invalid_response"
                record.output = {"status": "error", "error_category": category}
                return self._error_result(mode, category, time_basis), None
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception as exc:
                category = _safe_error_category(exc)
                record.output = {"status": "error", "error_category": category}
                return self._error_result(mode, category, time_basis), None
            record.output = {"status": fact.status, "cache": "hit_or_loaded"}
            return self._mode_result(mode, fact, time_basis), fact

    @staticmethod
    def _mode_result(
        mode: RouteMode,
        fact: RouteFact,
        time_basis: Literal["reference", "unspecified"],
    ) -> RouteBackfillModeResult:
        if mode == "transit" and fact.time_verification not in {
            "sent_not_echoed", "provider_echoed",
        }:
            time_basis = "unspecified"
        if fact.status == "available":
            outcome = _route_fact_outcome(None, mode, fact).outcome
            return RouteBackfillModeResult(
                mode=mode,
                status="available",
                route_fact_id=fact.fact_id,
                duration_s=outcome.duration_s,
                time_basis=time_basis,
            )
        if fact.status == "empty":
            return RouteBackfillModeResult(
                mode=mode, status="empty", route_fact_id=fact.fact_id,
                time_basis=time_basis,
            )
        return RouteBackfillModeResult(
            mode=mode,
            status="error",
            route_fact_id=fact.fact_id,
            error_category=fact.error_category or "provider_error",
            time_basis=time_basis,
        )

    @staticmethod
    def _error_result(
        mode: RouteMode,
        category: str,
        time_basis: Literal["reference", "unspecified"],
    ) -> RouteBackfillModeResult:
        known_categories = {
            "invalid_request", "auth", "rate_limit", "timeout", "network",
            "provider_error", "invalid_response",
        }
        if category not in known_categories:
            category = "provider_error"
        return RouteBackfillModeResult(
            mode=mode, status="error", error_category=category, time_basis=time_basis,
        )

    @staticmethod
    def _is_current(current_revision: CurrentRevision | None, revision: int) -> bool:
        return current_revision is None or current_revision() == revision

    @staticmethod
    def _build_jobs(draft: PlanDraft, facts: FactStore) -> tuple[_PairJob, ...]:
        places_by_id = {place.fact_id: place for place in facts.places}
        jobs: list[_PairJob] = []
        for day in draft.days:
            selected: dict[tuple[str, str, float, float], PlaceFact] = {}

            def include(poi_id: str | None, fact_id: str | None) -> None:
                if poi_id is None or fact_id is None:
                    return
                place = places_by_id.get(fact_id)
                if place is None or place.poi_id != poi_id or place.location is None:
                    return
                key = (
                    place.poi_id,
                    place.location.coordinate_system,
                    float(place.location.lng),
                    float(place.location.lat),
                )
                selected.setdefault(key, place)

            if day.lodging is not None:
                include(day.lodging.poi_id, day.lodging.place_fact_id)
            for activity in day.activities:
                if activity.kind != "free_time":
                    include(activity.poi_id, activity.place_fact_id)

            places = tuple(selected.values())
            for origin in places:
                for destination in places:
                    if origin.poi_id == destination.poi_id:
                        continue
                    jobs.append(_PairJob(
                        day_id=day.day_id,
                        day_date=day.day_date,
                        origin=origin.model_copy(deep=True),
                        destination=destination.model_copy(deep=True),
                    ))
        return tuple(jobs)
