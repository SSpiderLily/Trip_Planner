"""提案规范化和事实绑定的共享纯实现；旧生成器保留兼容方法。"""
from __future__ import annotations
from datetime import date
from typing import Any
from uuid import NAMESPACE_URL, uuid5
from app.models.planning_errors import PlanningError, InvalidPlanningReferences as _InvalidReferences
from app.models.planning_v4 import PlanProposal, PlanDraft, DraftActivity, DraftDay, LodgingRef
from app.models.planning_v4.facts import WeatherFact
from app.services.planning_lodging_policy import lodging_stays, stay_for_days, city_for_days, choose_lodging_for_stay
from app.services.planning_candidate_policy import candidate_allows_slot
from app.services.planning_v4_validator import validate_plan_proposal
from .fact_policy import _city_matches, _category_status, _dining_category, _lodging_category, _matches_original_query, _is_region_or_business_area, _has_valid_weather_fields



def _stable_id(kind: str, *parts: str) -> str:
    key = "\x1f".join((kind, *parts))
    return f"{kind}_{uuid5(NAMESPACE_URL, key).hex}"


def _reference_error(codes):
    error = PlanningError(
        "PLAN_REFERENCE_INVALID",
        "每日安排提案引用了无效候选、事实、城市或区域",
        step="daily_plan_reference_validation",
    )
    error.reference_issue_codes = tuple(sorted(set(codes)))
    return error


class DailyPlanRules:
    def _validate_proposal_output(self, output, planning_input, candidates, facts, area_hints):
        refs = [activity.proposal_ref for day in output.days for activity in day.activities]
        if len(refs) != len(set(refs)):
            raise _reference_error(("duplicate_proposal_ref",))
        proposal = self._build_proposal(output, planning_input, area_hints, facts)
        validate_plan_proposal(proposal, planning_input, candidates, facts)
        self._validate_selected_candidates(proposal, planning_input, candidates, facts, area_hints)
        return proposal


    @staticmethod
    def _candidate_rejection(candidate, place, planning_input, facts, area_hints):
        if place is None or place.poi_id != candidate.poi_id:
            return "candidate_fact_mismatch"
        if place.location is None:
            return "candidate_location_missing"
        if not place.city or not any(_city_matches(stay.city, place.city) for stay in lodging_stays(planning_input)):
            return "candidate_city_unverified"
        if candidate.category == "sightseeing" and _category_status(place) != "sightseeing":
            return "candidate_category_unverified"
        if candidate.category == "meal" and _dining_category(place) != "dining":
            return "candidate_category_unverified"
        if candidate.category == "meal" and candidate.slot_ids:
            eligible_ids = {
                slot.slot_id for day in planning_input.days for slot in day.slots
                if slot.eligibility != "skipped" and slot.period in {"breakfast", "lunch", "dinner"}
            }
            if not set(candidate.slot_ids) & eligible_ids:
                return "candidate_slot_scope_unavailable"
        source_ids = {source.tool_call_id for source in facts.sources}
        if not place.source_refs or any(source_id not in source_ids for source_id in place.source_refs):
            return "candidate_source_unverified"
        if candidate.category == "lodging":
            stays = [stay for stay in lodging_stays(planning_input)
                     if not candidate.lodging_day_ids or set(stay.day_ids).issubset(candidate.lodging_day_ids)]
            if not stays:
                return "lodging_candidate_scope_invalid"
            for stay in stays:
                if stay.scope_required and not candidate.lodging_day_ids:
                    continue
                if not _city_matches(stay.city, place.city):
                    continue
                if stay.specified:
                    if candidate.area_hint_id is not None:
                        continue
                    exact = place.poi_id == stay.lodging_poi_id if stay.lodging_poi_id else _matches_original_query(place, stay.lodging_query)
                    if exact and not _is_region_or_business_area(place):
                        return None
                elif (candidate.area_hint_id in {area.area_hint_id for area in area_hints}
                      and _lodging_category(place) == "lodging"):
                    return None
            return "lodging_candidate_binding_invalid"
        return None


    @classmethod
    def _build_proposal(cls, output, planning_input, area_hints, facts=None):
        days = output.days
        if facts is not None:
            # 无目标日期预报时，后端覆盖模型文字，避免模型把当前实况或其他日期天气带进行程。
            days = tuple(
                day.model_copy(update={"weather_advice": cls._weather_advice_for_proposal(day, planning_input, facts)})
                for day in output.days
            )
        # 由后端按实际地点活动覆盖情况补全缺项；free_time 不算地点活动。
        constraints_by_day = {day.day_id: day for day in planning_input.days}
        enriched_days = []
        for day in days:
            if len(day.unfilled_slot_ids) != len(set(day.unfilled_slot_ids)):
                raise _InvalidReferences(("duplicate_unfilled_slot",))
            constraint = constraints_by_day.get(day.day_id)
            if constraint is None:
                # 日期引用错误由已有公共校验器报告；此时不能推导该日的有效时段。
                enriched_days.append(day)
                continue
            slot_by_id = {slot.slot_id: slot for slot in constraint.slots}
            invalid_unfilled = {
                slot_id for slot_id in day.unfilled_slot_ids
                if slot_id not in slot_by_id or slot_by_id[slot_id].eligibility == "skipped"
            }
            if invalid_unfilled:
                raise _InvalidReferences(("invalid_unfilled_slot",))

            covered_slots = set()
            for activity in day.activities:
                slot = slot_by_id.get(activity.slot_id)
                if slot is None or slot.eligibility == "skipped" or activity.kind == "free_time":
                    continue
                if (
                    activity.kind == "sightseeing"
                    and slot.period in {"morning", "afternoon", "evening"}
                ) or (
                    activity.kind == "meal"
                    and slot.period in {"breakfast", "lunch", "dinner"}
                ):
                    covered_slots.add(slot.slot_id)
            unfilled = tuple(
                slot.slot_id for slot in constraint.slots
                if slot.eligibility != "skipped" and slot.slot_id not in covered_slots
            )
            enriched_days.append(day.model_copy(update={"unfilled_slot_ids": unfilled}))
        return PlanProposal(
            request_id=planning_input.request.request_id,
            rule_version=planning_input.rules.rule_version,
            days=tuple(enriched_days),
            area_hints=area_hints,
            missing_notes=output.missing_notes,
        )


    @classmethod
    def _weather_advice_for_proposal(cls, day, planning_input, facts):
        matches = cls._matching_weather_facts(day.day_date, planning_input, facts)
        if len(matches) != 1:
            return "该日期暂无预报，天气建议未知。"
        return day.weather_advice


    def _validate_selected_candidates(self, proposal, planning_input, candidates, facts, area_hints):
        by_id = {candidate.candidate_id: candidate for candidate in candidates}
        place_by_fact = {place.fact_id: place for place in facts.places}
        area_ids = {area.area_hint_id for area in area_hints}
        issues: set[str] = set()
        activity_issue_codes: dict[tuple[int, int], set[str]] = {}

        def add_activity_issue(day_index, activity_index, code):
            issues.add(code)
            activity_issue_codes.setdefault((day_index, activity_index), set()).add(code)

        expected_day_ids = [day.day_id for day in planning_input.days]
        if [day.day_id for day in proposal.days] != expected_day_ids:
            issues.add("day_order_mismatch")
        slots_by_day = {day.day_id: {slot.slot_id: slot for slot in day.slots} for day in planning_input.days}
        for day_index, day in enumerate(proposal.days):
            for activity_index, activity in enumerate(day.activities):
                slot = slots_by_day.get(day.day_id, {}).get(activity.slot_id)
                if activity.kind == "sightseeing" and (slot is None or slot.period not in {"morning", "afternoon", "evening"}):
                    add_activity_issue(day_index, activity_index, "activity_kind_slot_mismatch")
                if activity.kind == "meal" and (slot is None or slot.period not in {"breakfast", "lunch", "dinner"}):
                    add_activity_issue(day_index, activity_index, "activity_kind_slot_mismatch")
                if activity.candidate_ref is None:
                    continue
                candidate = by_id.get(activity.candidate_ref)
                if candidate is None or not candidate_allows_slot(candidate, activity.slot_id):
                    add_activity_issue(day_index, activity_index, "candidate_slot_scope_invalid")
                place = place_by_fact.get(candidate.place_fact_id) if candidate else None
                if place is None or not place.city or not _city_matches(city_for_days(planning_input, (day.day_id,)), place.city):
                    add_activity_issue(day_index, activity_index, "selected_candidate_city_invalid")
                if place is None or place.location is None:
                    add_activity_issue(day_index, activity_index, "selected_candidate_location_missing")
                if place is not None and activity.kind == "sightseeing" and _category_status(place) != "sightseeing":
                    add_activity_issue(day_index, activity_index, "selected_candidate_category_invalid")
                if place is not None and activity.kind == "meal" and _dining_category(place) != "dining":
                    add_activity_issue(day_index, activity_index, "selected_candidate_category_invalid")
                if place is None or not place.source_refs or any(
                    source_id not in {source.tool_call_id for source in facts.sources}
                    for source_id in place.source_refs
                ):
                    add_activity_issue(day_index, activity_index, "selected_candidate_source_invalid")
            lodging = day.lodging
            stay = stay_for_days(planning_input, (day.day_id,))
            selected = choose_lodging_for_stay(planning_input, stay, candidates, facts, area_hints)
            if stay.specified:
                if lodging and lodging.candidate_ref is not None and (
                    selected is None or lodging.candidate_ref != selected.candidate_id
                ):
                    issues.add("specified_lodging_replacement")
                if lodging and lodging.area_hint_id is not None:
                    issues.add("specified_lodging_area_forbidden")
            elif lodging and lodging.candidate_ref is not None:
                candidate = by_id.get(lodging.candidate_ref)
                place = place_by_fact.get(candidate.place_fact_id) if candidate else None
                if candidate is None or candidate.category != "lodging":
                    issues.add("lodging_candidate_invalid")
                if place is None or not _city_matches(stay.city, place.city):
                    issues.add("lodging_candidate_city_invalid")
                if place is None or place.location is None:
                    issues.add("lodging_candidate_location_invalid")
                if place is None or not place.source_refs:
                    issues.add("lodging_candidate_source_invalid")
                if place is not None and _lodging_category(place) != "lodging":
                    issues.add("lodging_candidate_category_invalid")
                if candidate is None or candidate.area_hint_id not in area_ids:
                    issues.add("lodging_candidate_area_invalid")
                if candidate and lodging.area_hint_id != candidate.area_hint_id:
                    issues.add("lodging_area_candidate_mismatch")
                if selected is None or lodging.candidate_ref != selected.candidate_id:
                    issues.add("lodging_reuse_mismatch")
            elif lodging and lodging.area_hint_id is not None:
                issues.add("lodging_area_without_candidate")
            try:
                self._weather_for_day(day.day_date, day.weather_advice, planning_input, facts)
            except _InvalidReferences as exc:
                issues.update(exc.codes)
        if issues:
            raise _InvalidReferences(
                tuple(sorted(issues)),
                activity_issue_codes={key: tuple(sorted(value)) for key, value in activity_issue_codes.items()},
            )


    def _build_draft(
        self, proposal, planning_input, candidates, facts, area_hints, source_run_id,
        *, plan_id: str | None = None,
    ):
        candidate_by_id = {candidate.candidate_id: candidate for candidate in candidates}
        place_by_fact = {place.fact_id: place for place in facts.places}
        draft_days = []
        for proposal_day in proposal.days:
            activities = []
            for activity in proposal_day.activities:
                candidate = candidate_by_id.get(activity.candidate_ref) if activity.candidate_ref else None
                place = place_by_fact.get(candidate.place_fact_id) if candidate else None
                activities.append(DraftActivity(
                    activity_id=self._activity_id(
                        planning_input.request.request_id,
                        source_run_id,
                        proposal_day.day_id,
                        activity.proposal_ref,
                    ),
                    candidate_ref=activity.candidate_ref,
                    slot_id=activity.slot_id,
                    kind=activity.kind,
                    poi_id=candidate.poi_id if candidate else None,
                    place_fact_id=candidate.place_fact_id if candidate else None,
                    activity_source="model",
                    requirement_ids=activity.requirement_ids,
                    recommendation_reason=activity.recommendation_reason,
                ))

            lodging = self._draft_lodging(
                proposal_day, planning_input, candidates, facts, area_hints,
            )
            weather_fact_id, weather_advice = self._weather_for_day(
                proposal_day.day_date,
                proposal_day.weather_advice,
                planning_input,
                facts,
            )
            draft_days.append(DraftDay(
                day_id=proposal_day.day_id,
                day_date=proposal_day.day_date,
                theme=proposal_day.theme,
                activities=tuple(activities),
                unfilled_slot_ids=proposal_day.unfilled_slot_ids,
                weather_fact_id=weather_fact_id,
                weather_advice=weather_advice,
                lodging=lodging,
            ))
        plan_id = plan_id or _stable_id("plan", planning_input.request.request_id, source_run_id)
        return PlanDraft(
            plan_id=plan_id,
            request_id=planning_input.request.request_id,
            source_run_id=source_run_id,
            revision=1,
            base_revision=None,
            rule_version=planning_input.rules.rule_version,
            days=tuple(draft_days),
            area_hints=area_hints,
            legs=(),
        )


    def _draft_lodging(self, proposal_day, planning_input, candidates, facts, area_hints):
        stay = stay_for_days(planning_input, (proposal_day.day_id,))
        candidate = choose_lodging_for_stay(planning_input, stay, candidates, facts, area_hints)
        proposed = proposal_day.lodging
        if candidate is None:
            return LodgingRef(selection_source="unknown")
        if proposed and proposed.candidate_ref is not None and proposed.candidate_ref != candidate.candidate_id:
            raise _InvalidReferences(("specified_lodging_replacement" if stay.specified else "lodging_reuse_mismatch",))
        return LodgingRef(
            candidate_ref=candidate.candidate_id, poi_id=candidate.poi_id,
            place_fact_id=candidate.place_fact_id, area_hint_id=None if stay.specified else candidate.area_hint_id,
            selection_source="user" if stay.specified else "recommended",
        )


    @classmethod
    def _unique_specified_lodging(cls, planning_input, candidates, facts, area_hints):
        stay = lodging_stays(planning_input)[0]
        return choose_lodging_for_stay(planning_input, stay, candidates, facts, area_hints) if stay.specified else None


    @staticmethod
    def _matching_weather_rows(day_date: date, planning_input, facts):
        matches: list[tuple[WeatherFact, Any]] = []
        day = next((item for item in planning_input.days if item.day_date == day_date), None)
        if day is None:
            return []
        for fact in facts.weather:
            if not _city_matches(city_for_days(planning_input, (day.day_id,)), fact.city):
                continue
            if day_date in fact.missing_dates:
                continue
            forecasts = [forecast for forecast in fact.forecasts if forecast.forecast_date == day_date]
            if len(forecasts) != 1 or not _has_valid_weather_fields(forecasts[0]):
                continue
            matches.append((fact, forecasts[0]))
        return matches


    @classmethod
    def _matching_weather_facts(cls, day_date: date, planning_input, facts):
        return [fact for fact, _ in cls._matching_weather_rows(day_date, planning_input, facts)]


    @classmethod
    def _weather_for_day(cls, day_date: date, model_advice, planning_input, facts):
        matches = cls._matching_weather_facts(day_date, planning_input, facts)
        if len(matches) != 1:
            return None, "该日期暂无预报，天气建议未知。"
        advice = model_advice.strip() if isinstance(model_advice, str) else ""
        if not advice:
            return matches[0].fact_id, "天气已查询，出行建议暂未生成，请结合当天天气调整。"
        return matches[0].fact_id, advice


    @staticmethod
    def _activity_id(request_id, source_run_id, day_id, proposal_ref):
        return _stable_id("activity", request_id, source_run_id, day_id, proposal_ref)


    @classmethod
    def _validate_input_references(cls, planning_input, candidates, facts, area_hints, task_results, source_run_id):
        area_ids = [area.area_hint_id for area in area_hints]
        issues: list[str] = []
        if len(area_ids) != len(set(area_ids)):
            issues.append("duplicate_area_hint")
        known = set(area_ids)
        if any(candidate.area_hint_id is not None and candidate.area_hint_id not in known for candidate in candidates):
            issues.append("candidate_area_hint_invalid")

        candidate_ids = [candidate.candidate_id for candidate in candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            issues.append("duplicate_candidate_id")
        known_slots = {
            slot.slot_id for day in planning_input.days for slot in day.slots
        }
        known_requirements = {
            requirement.requirement_id
            for requirement in planning_input.requirements.hard_requirements
        }
        if any(not set(candidate.slot_ids).issubset(known_slots) for candidate in candidates):
            issues.append("candidate_slot_reference_invalid")
        if any(not set(candidate.requirement_ids).issubset(known_requirements) for candidate in candidates):
            issues.append("candidate_requirement_reference_invalid")
        place_by_id = {place.fact_id: place for place in facts.places}
        if len(place_by_id) != len(facts.places):
            issues.append("duplicate_place_fact_id")
        source_by_id = {source.tool_call_id: source for source in facts.sources}
        if len(source_by_id) != len(facts.sources):
            issues.append("duplicate_fact_source_id")
        for place in facts.places:
            if any(source_id not in source_by_id for source_id in place.source_refs):
                issues.append("dangling_place_source")
            if any(source_id not in source_by_id for source_id in place.field_sources.values()):
                issues.append("dangling_place_field_source")
        for kind, items in (("weather", facts.weather), ("route", facts.routes)):
            for fact in items:
                source = getattr(fact, "source", None)
                if source is None or source.tool_call_id not in source_by_id:
                    issues.append(f"dangling_{kind}_source")
                elif source_by_id[source.tool_call_id] != source:
                    issues.append(f"{kind}_source_mismatch")

        for candidate in candidates:
            place = place_by_id.get(candidate.place_fact_id)
            if place is None:
                issues.append("candidate_fact_missing")
                continue
            if place.poi_id != candidate.poi_id:
                issues.append("candidate_poi_mismatch")
            if place.location is None:
                issues.append("candidate_location_missing")

        all_fact_ids = set(place_by_id)
        all_fact_ids.update(item.fact_id for item in facts.weather)
        all_fact_ids.update(item.fact_id for item in facts.routes)
        poi_ids = {place.poi_id for place in facts.places}
        for area in area_hints:
            if any(fact_id not in all_fact_ids for fact_id in area.basis_fact_ids):
                issues.append("dangling_area_basis")
            if any(poi_id not in poi_ids for poi_id in area.anchor_poi_ids):
                issues.append("dangling_area_anchor")
            if area.center is not None and not area.basis_fact_ids:
                issues.append("unsupported_area_center")

        candidate_by_id = {candidate.candidate_id: candidate for candidate in candidates}
        for result in task_results:
            if result.request_id != planning_input.request.request_id:
                issues.append("task_request_mismatch")
            if result.run_id != source_run_id:
                issues.append("task_run_mismatch")
            for task_candidate in result.candidates:
                current = candidate_by_id.get(task_candidate.candidate_id)
                if current is None:
                    issues.append("task_candidate_missing")
                elif current != task_candidate:
                    issues.append("task_candidate_mismatch")
        if issues:
            raise _reference_error(issues)


    @staticmethod
    def _validate_revision_candidates(
        revision, planning_input, candidates, facts, area_hints,
        preserve_ids, modifiable_ids,
    ):
        by_id = {candidate.candidate_id: candidate for candidate in candidates}
        place_by_id = {place.fact_id: place for place in facts.places}
        for activity in revision.replacement_day.activities:
            existing_id = activity.existing_activity_id
            if existing_id is not None and existing_id in preserve_ids and existing_id not in modifiable_ids:
                continue
            if activity.kind == "free_time":
                continue
            candidate = by_id.get(activity.candidate_ref or "")
            if candidate is None:
                raise ValueError("revision candidate missing")
            place = place_by_id.get(candidate.place_fact_id)
            reason = DailyPlanRules._candidate_rejection(
                candidate, place, planning_input, facts, area_hints,
            )
            if reason is not None:
                raise ValueError("revision candidate is not selectable")
