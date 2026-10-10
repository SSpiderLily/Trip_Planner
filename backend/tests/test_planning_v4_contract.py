"""planning_v4 模型与跨对象引用校验。"""
from datetime import date, datetime, timedelta
from decimal import Decimal
import unittest

from pydantic import ValidationError

from app.models.planning_v4 import (
    AreaHint,
    CandidateRef,
    DayConstraint,
    DayRevision,
    DaySummary,
    DayValidation,
    DraftActivity,
    DraftDay,
    EditCommand,
    FactSource,
    FactStore,
    HardRequirement,
    ItineraryPreview,
    LegEndpoint,
    LodgingProposal,
    LodgingRef,
    MapCoordinate,
    PlaceFact,
    PlanDraft,
    PlanProposal,
    PlanningInput,
    PreviewCalculation,
    ProposalActivity,
    ProposalDay,
    ReferenceCost,
    RepairState,
    RouteOption,
    RouteFact,
    RouteModeResult,
    RouteSegment,
    TransitLine,
    TransitStop,
    RequirementResult,
    RequirementSummary,
    RouteLeg,
    RuleSet,
    SlotConstraint,
    TripRequest,
    ValidationIssue,
    ValidationReport,
    WeatherFact,
    WeatherForecast,
)
from app.models.planning_v4.plan import DeleteActivity, ReorderActivities
from app.services.planning_v4_validator import (
    ContractValidationError,
    validate_day_revision,
    validate_edit_command,
    validate_plan_proposal,
    validate_preview,
)


TZ = "Asia/Shanghai"
ARRIVAL = datetime.fromisoformat("2026-10-10T09:00:00+08:00")
DEPARTURE = datetime.fromisoformat("2026-10-12T19:00:00+08:00")
DAYS = tuple(date(2026, 10, 10) + timedelta(days=index) for index in range(3))


def make_input():
    request = TripRequest(
        request_id="request-1", city="上海", arrival_at=ARRIVAL, departure_at=DEPARTURE,
        timezone=TZ, remarks="  必须去外滩；不要去动物园 \n",
    )
    requirements = RequirementSummary(
        requirements_id="requirements-1", request_id=request.request_id,
        hard_requirements=(
            HardRequirement(requirement_id="req-bund", kind="must_visit", text="去外滩",
                            source={"field": "remarks", "quote": "必须去外滩"}),
            HardRequirement(requirement_id="req-avoid", kind="must_avoid", text="不去动物园",
                            source={"field": "remarks", "quote": "不要去动物园"}),
        ),
    )
    periods = ("breakfast", "morning", "lunch", "afternoon", "dinner", "evening")
    days = []
    for index, day_date in enumerate(DAYS):
        day_id = f"day-{index + 1}"
        slots = tuple(
            SlotConstraint(
                slot_id=f"slot-{index + 1}-{period}", day_id=day_id, period=period,
                eligibility="required" if period == "morning" else "skipped",
            ) for period in periods
        )
        days.append(DayConstraint(day_id=day_id, day_date=day_date, slots=slots))
    windows = (
        {"period": "breakfast", "start_minute": 420, "end_minute": 600},
        {"period": "morning", "start_minute": 600, "end_minute": 720},
        {"period": "lunch", "start_minute": 720, "end_minute": 840},
        {"period": "afternoon", "start_minute": 840, "end_minute": 1080},
        {"period": "dinner", "start_minute": 1080, "end_minute": 1200},
        {"period": "evening", "start_minute": 1200, "end_minute": 1380},
    )
    rules = RuleSet(rule_version="rules-4.1", windows=windows)
    return PlanningInput(request=request, requirements=requirements, rules=rules, days=tuple(days))


def make_facts():
    queried = datetime.fromisoformat("2026-10-10T08:00:00+08:00")
    sources = tuple(FactSource(
        tool_call_id=call_id, tool_name=tool_name, api_version="v3", queried_at=queried,
        effective_params={"keywords": label},
    ) for call_id, tool_name, label in (
        ("call-bund", "maps_text_search", "外滩"),
        ("call-hotel", "maps_search_detail", "酒店"),
        ("call-weather", "maps_weather", "310101"),
    ))
    places = (
        PlaceFact(fact_id="fact-bund", poi_id="poi-bund", name="外滩", location=MapCoordinate(lng=121.49, lat=31.24),
                  cost=ReferenceCost(raw="免费"), source_refs=("call-bund",)),
        PlaceFact(fact_id="fact-hotel", poi_id="poi-hotel", name="滨江酒店", location=MapCoordinate(lng=121.50, lat=31.23),
                  source_refs=("call-hotel",)),
    )
    weather = WeatherFact(
        fact_id="fact-weather", city="上海", adcode="310101", source=sources[2],
        forecasts=(WeatherForecast(forecast_date=DAYS[0], day_temp_c=-1),),
        missing_dates=DAYS[1:],
    )
    return FactStore(sources=sources, places=places, weather=(weather,))


def make_candidates():
    return (
        CandidateRef(candidate_id="candidate-bund", category="sightseeing", poi_id="poi-bund", place_fact_id="fact-bund"),
        CandidateRef(candidate_id="candidate-hotel", category="lodging", poi_id="poi-hotel", place_fact_id="fact-hotel"),
    )


def make_preview():
    planning_input = make_input()
    facts = make_facts()
    candidates = make_candidates()
    area = AreaHint(
        area_hint_id="area-bund", label="外滩周边", anchor_poi_ids=("poi-bund",),
        center=MapCoordinate(lng=121.49, lat=31.24), basis_fact_ids=("fact-bund",),
    )
    draft_days = []
    proposal_days = []
    for index, day_date in enumerate(DAYS):
        day_id = f"day-{index + 1}"
        activity_id = f"activity-{index + 1}"
        slot_id = f"slot-{index + 1}-morning"
        requirements = ("req-bund",) if index == 0 else ()
        draft_days.append(DraftDay(
            day_id=day_id, day_date=day_date,
            activities=(DraftActivity(
                activity_id=activity_id, candidate_ref="candidate-bund", slot_id=slot_id,
                kind="sightseeing", poi_id="poi-bund", place_fact_id="fact-bund",
                duration_s=10800, duration_source="default", requirement_ids=requirements,
            ),),
            weather_fact_id="fact-weather",
            lodging=LodgingRef(candidate_ref="candidate-hotel", poi_id="poi-hotel", place_fact_id="fact-hotel",
                               area_hint_id="area-bund", selection_source="recommended"),
        ))
        proposal_days.append(ProposalDay(
            day_id=day_id, day_date=day_date,
            activities=(ProposalActivity(
                proposal_ref=f"proposal-{index + 1}", candidate_ref="candidate-bund",
                slot_id=slot_id, kind="sightseeing", requirement_ids=requirements,
            ),),
            lodging=LodgingProposal(candidate_ref="candidate-hotel", area_hint_id="area-bund"),
        ))
    draft = PlanDraft(
        plan_id="plan-1", request_id="request-1", source_run_id="run-1", revision=1,
        rule_version="rules-4.1", days=tuple(draft_days), area_hints=(area,),
    )
    report = ValidationReport(
        plan_id="plan-1", based_on_revision=1,
        days=tuple(DayValidation(day_id=f"day-{index + 1}", completeness="complete", feasibility="verified")
                   for index in range(3)),
        requirements=(
            RequirementResult(requirement_id="req-bund", status="satisfied", activity_ids=("activity-1",), place_fact_ids=("fact-bund",)),
            RequirementResult(requirement_id="req-avoid", status="satisfied"),
        ),
        repair_states=tuple(RepairState(day_id=f"day-{index + 1}", completed_rounds=0, status="open") for index in range(3)),
    )
    preview = ItineraryPreview(
        preview_id="preview-1", planning_input=planning_input, draft=draft, facts=facts,
        candidates=candidates, validation=report,
        day_summaries=tuple(DaySummary(day_id=f"day-{index + 1}", known_stay_s=10800,
                                      known_travel_s=0, feasibility="verified") for index in range(3)),
        calculation=PreviewCalculation(based_on_revision=1, calculation_id="calc-1",
                                       current_routes="completed", alternative_backfill="pending"),
    )
    proposal = PlanProposal(
        request_id="request-1", rule_version="rules-4.1", days=tuple(proposal_days), area_hints=(area,),
    )
    return planning_input, facts, candidates, proposal, preview


def make_full_baseline_preview():
    """固定上海三日合成事实：沿用CHG-003请求与CONTEXT六窗口。"""
    remarks = "想看上海代表性景点，必须去外滩。以公共交通结合步行为主，节奏适中，尽量少折返；安排顺路午餐和晚餐。"
    request = TripRequest(
        request_id="synthetic-shanghai-3d", city="上海", arrival_at=ARRIVAL, departure_at=DEPARTURE,
        timezone=TZ, preferences=("城市漫步", "美食"), transport_preferences=("transit", "walking"),
        remarks=remarks,
    )
    requirements = RequirementSummary(
        requirements_id="synthetic-req-summary", request_id=request.request_id,
        hard_requirements=(HardRequirement(
            requirement_id="must-visit-bund", kind="must_visit", text="必须去外滩",
            source={"field": "remarks", "quote": "必须去外滩"},
        ),),
        preferences=("城市漫步", "美食"),
    )
    windows = (
        {"period": "breakfast", "start_minute": 480, "end_minute": 540},
        {"period": "morning", "start_minute": 540, "end_minute": 720},
        {"period": "lunch", "start_minute": 720, "end_minute": 840},
        {"period": "afternoon", "start_minute": 840, "end_minute": 1020},
        {"period": "dinner", "start_minute": 1020, "end_minute": 1140},
        {"period": "evening", "start_minute": 1140, "end_minute": 1320},
    )
    rules = RuleSet(rule_version="synthetic-rules-v4", windows=windows)
    periods = ("breakfast", "morning", "lunch", "afternoon", "dinner", "evening")
    eligibility = (
        ("skipped", "skipped", "required", "required", "required", "required"),
        ("required", "required", "required", "required", "required", "required"),
        ("required", "required", "required", "required", "conditional", "skipped"),
    )
    constraints = []
    for day_index, day_date in enumerate(DAYS):
        day_id = f"synthetic-day-{day_index + 1}"
        slots = tuple(SlotConstraint(
            slot_id=f"synthetic-{day_index + 1}-{period}", day_id=day_id,
            period=period, eligibility=eligibility[day_index][period_index],
            condition_reason="核对19:00离开与返程时间后再决定" if eligibility[day_index][period_index] == "conditional" else None,
        ) for period_index, period in enumerate(periods))
        constraints.append(DayConstraint(day_id=day_id, day_date=day_date, slots=slots))
    planning_input = PlanningInput(request=request, requirements=requirements, rules=rules, days=tuple(constraints))

    queried = datetime.fromisoformat("2026-10-10T00:30:00+00:00")
    sources = tuple(FactSource(
        tool_call_id=call_id, tool_name=tool_name, api_version=api_version, queried_at=queried,
        effective_params=params,
    ) for call_id, tool_name, api_version, params in (
        ("synthetic-pois", "maps_text_search", "v3", {"city": "上海"}),
        ("synthetic-weather", "maps_weather", "v3", {"city": "310101"}),
        ("synthetic-route", "maps_direction_transit_integrated_by_coordinates", "v3", {}),
    ))
    facts = FactStore(
        sources=sources,
        places=(
            PlaceFact(fact_id="synthetic-fact-bund", poi_id="synthetic-poi-bund", name="外滩",
                      location=MapCoordinate(lng=121.4903, lat=31.2397), source_refs=("synthetic-pois",)),
            PlaceFact(fact_id="synthetic-fact-yuyuan", poi_id="synthetic-poi-yuyuan", name="豫园",
                      location=MapCoordinate(lng=121.4922, lat=31.2271), source_refs=("synthetic-pois",)),
            PlaceFact(fact_id="synthetic-fact-nanjing", poi_id="synthetic-poi-nanjing", name="南京路步行街",
                      location=MapCoordinate(lng=121.4845, lat=31.2380), source_refs=("synthetic-pois",)),
            PlaceFact(fact_id="synthetic-fact-food", poi_id="synthetic-poi-food", name="本帮菜馆",
                      location=MapCoordinate(lng=121.4880, lat=31.2320), source_refs=("synthetic-pois",),
                      cost=ReferenceCost(raw="约80元", value=None)),
            PlaceFact(fact_id="synthetic-fact-hotel", poi_id="synthetic-poi-hotel", name="黄浦区酒店",
                      location=MapCoordinate(lng=121.4860, lat=31.2350), source_refs=("synthetic-pois",)),
        ),
        weather=(WeatherFact(
            fact_id="synthetic-fact-weather", city="上海", adcode="310101", source=sources[1],
            forecasts=tuple(WeatherForecast(forecast_date=day_date, day_weather="晴", night_weather="多云") for day_date in DAYS),
        ),),
    )
    candidates = (
        CandidateRef(candidate_id="synthetic-cand-bund", category="sightseeing", poi_id="synthetic-poi-bund", place_fact_id="synthetic-fact-bund"),
        CandidateRef(candidate_id="synthetic-cand-yuyuan", category="sightseeing", poi_id="synthetic-poi-yuyuan", place_fact_id="synthetic-fact-yuyuan"),
        CandidateRef(candidate_id="synthetic-cand-nanjing", category="sightseeing", poi_id="synthetic-poi-nanjing", place_fact_id="synthetic-fact-nanjing"),
        CandidateRef(candidate_id="synthetic-cand-food", category="meal", poi_id="synthetic-poi-food", place_fact_id="synthetic-fact-food"),
        CandidateRef(candidate_id="synthetic-cand-hotel", category="lodging", poi_id="synthetic-poi-hotel", place_fact_id="synthetic-fact-hotel"),
    )
    areas = tuple(AreaHint(
        area_hint_id=f"synthetic-area-{day_index + 1}", label="黄浦区住宿区域",
        anchor_poi_ids=("synthetic-poi-bund", "synthetic-poi-hotel"),
        center=MapCoordinate(lng=121.4880, lat=31.2360), basis_fact_ids=("synthetic-fact-bund", "synthetic-fact-hotel"),
    ) for day_index in range(3))
    activity_specs = (
        (("lunch", "meal", "synthetic-cand-food", "synthetic-poi-food", "synthetic-fact-food"),
         ("afternoon", "sightseeing", "synthetic-cand-bund", "synthetic-poi-bund", "synthetic-fact-bund"),
         ("dinner", "meal", "synthetic-cand-food", "synthetic-poi-food", "synthetic-fact-food"),
         ("evening", "sightseeing", "synthetic-cand-bund", "synthetic-poi-bund", "synthetic-fact-bund")),
        (("breakfast", "meal", "synthetic-cand-food", "synthetic-poi-food", "synthetic-fact-food"),
         ("morning", "sightseeing", "synthetic-cand-bund", "synthetic-poi-bund", "synthetic-fact-bund"),
         ("afternoon", "sightseeing", "synthetic-cand-bund", "synthetic-poi-bund", "synthetic-fact-bund"),
         ("dinner", "meal", "synthetic-cand-food", "synthetic-poi-food", "synthetic-fact-food"),
         ("evening", "sightseeing", "synthetic-cand-nanjing", "synthetic-poi-nanjing", "synthetic-fact-nanjing")),
        (("breakfast", "meal", "synthetic-cand-food", "synthetic-poi-food", "synthetic-fact-food"),
         ("morning", "sightseeing", "synthetic-cand-yuyuan", "synthetic-poi-yuyuan", "synthetic-fact-yuyuan"),
         ("lunch", "meal", "synthetic-cand-food", "synthetic-poi-food", "synthetic-fact-food"),
         ("afternoon", "sightseeing", "synthetic-cand-yuyuan", "synthetic-poi-yuyuan", "synthetic-fact-yuyuan")),
    )
    draft_days, proposal_days = [], []
    activity_by_day_and_period = {}
    for day_index, specs in enumerate(activity_specs):
        day_id = f"synthetic-day-{day_index + 1}"
        draft_activities, proposal_activities = [], []
        for period, kind, candidate_ref, poi_id, fact_id in specs:
            activity_id = f"synthetic-activity-{day_index + 1}-{period}"
            slot_id = f"synthetic-{day_index + 1}-{period}"
            activity_by_day_and_period[(day_index, period)] = activity_id
            common_requirements = ("must-visit-bund",) if kind == "sightseeing" and poi_id == "synthetic-poi-bund" else ()
            duration = 3600 if kind == "meal" else 10800 if kind == "sightseeing" else 0
            draft_activities.append(DraftActivity(
                activity_id=activity_id, candidate_ref=candidate_ref, slot_id=slot_id, kind=kind,
                poi_id=poi_id, place_fact_id=fact_id, duration_s=duration, duration_source="default",
                requirement_ids=common_requirements,
            ))
            proposal_activities.append(ProposalActivity(
                proposal_ref=f"proposal-{activity_id}", candidate_ref=candidate_ref,
                slot_id=slot_id, kind=kind, duration_s=duration, requirement_ids=common_requirements,
            ))
        unfilled = ("synthetic-2-lunch",) if day_index == 1 else ("synthetic-3-dinner",) if day_index == 2 else ()
        lodging = LodgingRef(
            candidate_ref="synthetic-cand-hotel", poi_id="synthetic-poi-hotel", place_fact_id="synthetic-fact-hotel",
            area_hint_id=f"synthetic-area-{day_index + 1}", selection_source="recommended",
        )
        draft_days.append(DraftDay(
            day_id=day_id, day_date=DAYS[day_index], activities=tuple(draft_activities), unfilled_slot_ids=unfilled,
            weather_fact_id="synthetic-fact-weather", lodging=lodging,
        ))
        proposal_days.append(ProposalDay(
            day_id=day_id, day_date=DAYS[day_index], activities=tuple(proposal_activities),
            unfilled_slot_ids=unfilled,
            lodging=LodgingProposal(candidate_ref="synthetic-cand-hotel", area_hint_id=f"synthetic-area-{day_index + 1}"),
        ))

    route_option = RouteOption(
        option_id="synthetic-route-option", duration_s=780, distance_m=1900,
        segments=(
            RouteSegment(sequence=0, mode="walking", duration_s=120, distance_m=140,
                         polyline=(MapCoordinate(lng=121.4860, lat=31.2350), MapCoordinate(lng=121.4870, lat=31.2360))),
            RouteSegment(sequence=1, mode="subway", duration_s=660, distance_m=1760,
                         transit_alternatives=(TransitLine(
                             mode="subway", line_name="地铁10号线", line_type="地铁线路", duration_s=600,
                             polyline=(MapCoordinate(lng=121.4870, lat=31.2360), MapCoordinate(lng=121.4903, lat=31.2397)),
                             departure_stop=TransitStop(name="豫园站"), arrival_stop=TransitStop(name="南京东路站"),
                         ),)),
        ), geometry_status="complete",
    )
    route_fact = RouteFact(
        fact_id="synthetic-route-fact", origin_poi_id="synthetic-poi-hotel", destination_poi_id="synthetic-poi-bund",
        origin=facts.places[4].location, destination=facts.places[0].location,
        mode="transit", status="available", options=(route_option,), source=sources[2],
    )
    facts = facts.model_copy(update={"routes": (route_fact,)})
    leg = RouteLeg(
        leg_id="synthetic-leg-day2", plan_revision=1, day_id="synthetic-day-2",
        from_endpoint=LegEndpoint(kind="lodging", reference_id="lodging:synthetic-day-2", poi_id="synthetic-poi-hotel"),
        to_endpoint=LegEndpoint(kind="activity", reference_id=activity_by_day_and_period[(1, "morning")], poi_id="synthetic-poi-bund"),
        mode_results=(RouteModeResult(mode="transit", status="available", route_fact_id="synthetic-route-fact",
                                      option_ids=("synthetic-route-option",)),),
        selected_mode="transit", selected_option_id="synthetic-route-option", selection_source="backend_fastest_available",
    )
    draft = PlanDraft(
        plan_id="synthetic-plan", request_id=request.request_id, source_run_id="synthetic-run", revision=1,
        rule_version=rules.rule_version, days=tuple(draft_days), area_hints=areas, legs=(leg,),
    )
    issues = (
        ValidationIssue(issue_id="synthetic-issue-day2-lunch", code="missing_meal", category="missing_arrangement",
                        severity="warning", scope="slot", day_id="synthetic-day-2", target_id="synthetic-2-lunch",
                        message="午餐餐馆尚未补齐", based_on_revision=1),
        ValidationIssue(issue_id="synthetic-issue-day3-dinner", code="conditional_dinner", category="unknown_info",
                        severity="info", scope="slot", day_id="synthetic-day-3", target_id="synthetic-3-dinner",
                        message="需核对离开地点与返程时间", based_on_revision=1),
    )
    report = ValidationReport(
        plan_id=draft.plan_id, based_on_revision=1, issues=issues,
        days=(
            DayValidation(day_id="synthetic-day-1", completeness="complete", feasibility="unverified"),
            DayValidation(day_id="synthetic-day-2", completeness="incomplete", feasibility="unverified",
                          issue_ids=("synthetic-issue-day2-lunch",)),
            DayValidation(day_id="synthetic-day-3", completeness="conditional", feasibility="unverified",
                          issue_ids=("synthetic-issue-day3-dinner",)),
        ),
        requirements=(RequirementResult(
            requirement_id="must-visit-bund", status="satisfied",
            activity_ids=(activity_by_day_and_period[(0, "afternoon")],), place_fact_ids=("synthetic-fact-bund",),
        ),),
        repair_states=tuple(RepairState(day_id=f"synthetic-day-{index + 1}", completed_rounds=0, status="open") for index in range(3)),
    )
    preview = ItineraryPreview(
        preview_id="synthetic-preview", planning_input=planning_input, draft=draft, facts=facts,
        candidates=candidates, validation=report,
        day_summaries=(
            DaySummary(day_id="synthetic-day-1", known_stay_s=8 * 3600, known_travel_s=0, feasibility="unverified"),
            DaySummary(day_id="synthetic-day-2", known_stay_s=11 * 3600, known_travel_s=780,
                       feasibility="unverified"),
            DaySummary(day_id="synthetic-day-3", known_stay_s=8 * 3600, known_travel_s=0, feasibility="unverified"),
        ),
        calculation=PreviewCalculation(based_on_revision=1, calculation_id="synthetic-calculation",
                                       current_routes="completed", alternative_backfill="pending"),
    )
    proposal = PlanProposal(
        request_id=request.request_id, rule_version=rules.rule_version, days=tuple(proposal_days), area_hints=areas,
    )
    return planning_input, facts, candidates, proposal, preview


class PlanningV4ContractTest(unittest.TestCase):
    def test_raw_remarks_are_preserved_and_arrival_departure_are_required(self):
        planning_input = make_input()
        self.assertEqual(planning_input.request.remarks, "  必须去外滩；不要去动物园 \n")
        with self.assertRaises(ValidationError):
            TripRequest(request_id="r", city="上海", timezone=TZ,
                        departure_at=DEPARTURE)
        with self.assertRaises(ValidationError):
            TripRequest(request_id="r", city="上海", timezone=TZ,
                        arrival_at=ARRIVAL)

    def test_input_requires_unique_contiguous_dates_covering_trip(self):
        planning_input = make_input()
        with self.assertRaises(ValidationError):
            PlanningInput.model_validate({
                **planning_input.model_dump(),
                "days": (*planning_input.days, planning_input.days[-1]),
            })
        with self.assertRaises(ValidationError):
            PlanningInput.model_validate({**planning_input.model_dump(), "days": planning_input.days[:-1]})

    def test_valid_proposal_preview_lodging_and_must_avoid_without_activity(self):
        planning_input, facts, candidates, proposal, preview = make_preview()
        validate_plan_proposal(proposal, planning_input, candidates, facts)
        validate_preview(preview)

    def test_proposal_rejects_unknown_candidate_extra_cost_and_date_set_mismatch(self):
        planning_input, facts, candidates, proposal, _ = make_preview()
        first = proposal.days[0].model_copy(update={
            "activities": (proposal.days[0].activities[0].model_copy(update={"candidate_ref": "missing"}),)
        })
        invalid = proposal.model_copy(update={"days": (first, *proposal.days[1:])})
        with self.assertRaises(ContractValidationError) as caught:
            validate_plan_proposal(invalid, planning_input, candidates, facts)
        self.assertIn("dangling_candidate", {item.code for item in caught.exception.issues})
        with self.assertRaises(ValidationError):
            PlanProposal.model_validate({**proposal.model_dump(), "cost": 100})
        with self.assertRaises(ContractValidationError) as caught:
            validate_plan_proposal(proposal.model_copy(update={"days": proposal.days[:-1]}), planning_input, candidates, facts)
        self.assertIn("day_set_mismatch", {item.code for item in caught.exception.issues})

    def test_candidate_and_formal_activity_reject_missing_location(self):
        planning_input, facts, candidates, proposal, preview = make_preview()
        places = tuple(place.model_copy(update={"location": None}) if place.poi_id == "poi-bund" else place for place in facts.places)
        bad_facts = facts.model_copy(update={"places": places})
        with self.assertRaises(ContractValidationError) as caught:
            validate_plan_proposal(proposal, planning_input, candidates, bad_facts)
        self.assertIn("candidate_location_missing", {item.code for item in caught.exception.issues})
        bad_preview = preview.model_copy(update={"facts": bad_facts})
        with self.assertRaises(ContractValidationError) as caught:
            validate_preview(bad_preview)
        self.assertIn("candidate_location_missing", {item.code for item in caught.exception.issues})
        with self.assertRaises(ValidationError):
            MapCoordinate(lng=181, lat=31)

    def test_dangling_sources_and_area_refs_are_structured_errors(self):
        planning_input, facts, candidates, proposal, _ = make_preview()
        bad_place = facts.places[0].model_copy(update={"source_refs": ("absent-source",)})
        bad_facts = facts.model_copy(update={"places": (bad_place, *facts.places[1:])})
        with self.assertRaises(ContractValidationError) as caught:
            validate_plan_proposal(proposal, planning_input, candidates, bad_facts)
        self.assertIn("dangling_source", {item.code for item in caught.exception.issues})

    def test_unknown_day_and_issue_day_return_contract_errors_without_key_error(self):
        _, _, _, _, preview = make_preview()
        unknown_day = preview.draft.days[0].model_copy(update={"day_id": "unknown-day"})
        draft = preview.draft.model_copy(update={"days": (unknown_day, *preview.draft.days[1:])})
        invalid = preview.model_copy(update={"draft": draft})
        with self.assertRaises(ContractValidationError) as caught:
            validate_preview(invalid)
        self.assertIn("day_set_mismatch", {item.code for item in caught.exception.issues})
        issue = ValidationIssue(
            issue_id="issue-unknown-day", code="missing", category="missing_arrangement", severity="warning",
            scope="slot", day_id="unknown-day", target_id="slot-x", message="缺项",
            based_on_revision=1,
        )
        report = preview.validation.model_copy(update={"issues": (issue,)})
        invalid_issue = preview.model_copy(update={"validation": report})
        with self.assertRaises(ContractValidationError) as caught:
            validate_preview(invalid_issue)
        self.assertIn("dangling_issue_day", {item.code for item in caught.exception.issues})

    def test_unknown_info_cannot_trigger_repair_and_skipped_slot_cannot_be_missing(self):
        _, _, _, _, preview = make_preview()
        issue = ValidationIssue(
            issue_id="issue-skipped", code="unknown", category="unknown_info", severity="warning",
            scope="slot", day_id="day-1", target_id="slot-1-breakfast", message="未知",
            triggers_repair=True, based_on_revision=1,
        )
        report = preview.validation.model_copy(update={"issues": (issue,)})
        invalid = preview.model_copy(update={"validation": report})
        with self.assertRaises(ContractValidationError) as caught:
            validate_preview(invalid)
        self.assertTrue({"unknown_info_triggers_repair", "skipped_slot_missing_issue"}.intersection(
            {item.code for item in caught.exception.issues}
        ))

    def test_satisfied_must_visit_requires_same_activity_and_place_binding(self):
        _, _, _, _, preview = make_preview()
        requirement = preview.validation.requirements[0].model_copy(update={"place_fact_ids": ("fact-hotel",)})
        report = preview.validation.model_copy(update={"requirements": (requirement, preview.validation.requirements[1])})
        invalid = preview.model_copy(update={"validation": report})
        with self.assertRaises(ContractValidationError) as caught:
            validate_preview(invalid)
        self.assertIn("requirement_activity_fact_mismatch", {item.code for item in caught.exception.issues})

    def test_reorder_requires_complete_id_permutation_and_remove_has_no_confirmation_flag(self):
        _, _, candidates, _, preview = make_preview()
        day = preview.draft.days[0]
        valid = EditCommand(
            plan_id="plan-1", day_id="day-1", base_revision=1, command_id="edit-1",
            operations=(ReorderActivities(type="reorder_activities", day_id="day-1", activity_ids=("activity-1",)),),
        )
        validate_edit_command(valid, preview.draft, candidates=candidates, planning_input=preview.planning_input)
        for ids in (("new-id",), ()):
            command = valid.model_copy(update={"operations": (
                ReorderActivities(type="reorder_activities", day_id="day-1", activity_ids=ids),
            )})
            with self.assertRaises(ContractValidationError):
                validate_edit_command(command, preview.draft, candidates=candidates)
        remove = EditCommand(
            plan_id="plan-1", day_id="day-1", base_revision=1, command_id="remove-1",
            operations=(DeleteActivity(type="remove_activity", day_id="day-1", activity_id="activity-1"),),
        )
        validate_edit_command(remove, preview.draft)
        with self.assertRaises(ValidationError):
            DeleteActivity.model_validate({"type": "remove_activity", "day_id": "day-1", "activity_id": "activity-1", "confirmation": True})

    def test_day_revision_preserves_named_activity_and_report_version(self):
        planning_input, facts, candidates, _, preview = make_preview()
        issue = ValidationIssue(
            issue_id="issue-repair", code="conflict", category="known_conflict", severity="error",
            scope="day", day_id="day-1", message="日期有已知冲突", triggers_repair=True,
            based_on_revision=1,
        )
        report = preview.validation.model_copy(update={"issues": (issue,)})
        activity = ProposalActivity(
            proposal_ref="proposal-preserved", existing_activity_id="activity-1",
            candidate_ref="candidate-bund", slot_id="slot-1-morning", kind="sightseeing",
            requirement_ids=("req-bund",),
        )
        revision = DayRevision(
            day_id="day-1", base_revision=1, revision=2, based_on_report_revision=1,
            replacement_day=ProposalDay(day_id="day-1", day_date=DAYS[0], activities=(activity,)),
            preserve_activity_ids=("activity-1",), source_issue_ids=("issue-repair",),
        )
        validate_day_revision(revision, preview.draft, report, planning_input, candidates, facts)
        invalid = revision.model_copy(update={"replacement_day": revision.replacement_day.model_copy(update={"activities": ()})})
        with self.assertRaises(ContractValidationError):
            validate_day_revision(invalid, preview.draft, report, planning_input, candidates, facts)

    def test_fixed_shanghai_three_day_synthetic_preview_roundtrips_and_validates(self):
        planning_input, facts, candidates, proposal, preview = make_full_baseline_preview()
        roundtrip = ItineraryPreview.model_validate_json(preview.model_dump_json())
        validate_plan_proposal(proposal, planning_input, candidates, facts)
        validate_preview(roundtrip)
        self.assertEqual(roundtrip.planning_input.request.remarks,
                         "想看上海代表性景点，必须去外滩。以公共交通结合步行为主，节奏适中，尽量少折返；安排顺路午餐和晚餐。")
        self.assertEqual(roundtrip.planning_input.request.preferences, ("城市漫步", "美食"))
        arrival_day = roundtrip.planning_input.days[0]
        self.assertEqual(next(slot.eligibility for slot in arrival_day.slots if slot.period == "morning"), "skipped")
        full_day = roundtrip.planning_input.days[1]
        self.assertEqual({slot.period for slot in full_day.slots}, {"breakfast", "morning", "lunch", "afternoon", "dinner", "evening"})
        departure_day = roundtrip.planning_input.days[2]
        self.assertEqual(next(slot.eligibility for slot in departure_day.slots if slot.period == "evening"), "skipped")
        self.assertEqual(next(slot.eligibility for slot in departure_day.slots if slot.period == "dinner"), "conditional")
        day2_bund = [activity for activity in roundtrip.draft.days[1].activities if activity.poi_id == "synthetic-poi-bund"]
        self.assertEqual(len(day2_bund), 2)
        self.assertNotEqual(day2_bund[0].activity_id, day2_bund[1].activity_id)
        self.assertEqual(roundtrip.draft.days[0].lodging.area_hint_id, "synthetic-area-1")
        self.assertEqual(roundtrip.draft.days[1].lodging.area_hint_id, "synthetic-area-2")
        self.assertEqual(roundtrip.draft.days[2].lodging.area_hint_id, "synthetic-area-3")
        self.assertEqual(roundtrip.draft.legs[0].selected_option_id, "synthetic-route-option")
        self.assertIn("synthetic-issue-day2-lunch", roundtrip.validation.days[1].issue_ids)

    def test_transit_timeout_without_http_metadata_can_remain_in_preview(self):
        _, facts, _, _, preview = make_full_baseline_preview()
        queried = datetime.fromisoformat("2026-10-10T00:30:00+00:00")
        source = FactSource(
            tool_call_id="timeout-call", tool_name="maps_direction_transit_integrated_by_coordinates",
            api_version="v3", queried_at=queried, effective_params={},
        )
        hotel = next(place for place in facts.places if place.poi_id == "synthetic-poi-hotel")
        bund = next(place for place in facts.places if place.poi_id == "synthetic-poi-bund")
        requested = datetime.fromisoformat("2026-10-11T09:00:00+08:00")
        failed_route = RouteFact(
            fact_id="timeout-route-fact", origin_poi_id=hotel.poi_id, destination_poi_id=bund.poi_id,
            origin=hotel.location, destination=bund.location, mode="transit", status="error",
            requested_departure_at=requested, requested_strategy=0, time_verification="not_sent",
            source=source, error_category="timeout",
        )
        updated_facts = facts.model_copy(update={
            "sources": (*facts.sources, source), "routes": (failed_route,),
        })
        leg = RouteLeg(
            leg_id="timeout-leg", plan_revision=1, day_id="synthetic-day-2",
            from_endpoint=LegEndpoint(kind="lodging", reference_id="lodging:synthetic-day-2", poi_id=hotel.poi_id),
            to_endpoint=LegEndpoint(kind="activity", reference_id="synthetic-activity-2-morning", poi_id=bund.poi_id),
            requested_departure_at=requested,
            mode_results=(RouteModeResult(mode="transit", status="error", route_fact_id=failed_route.fact_id,
                                          error_category="timeout"),),
        )
        issue = ValidationIssue(
            issue_id="timeout-issue", code="route_timeout", category="unknown_info", severity="warning",
            scope="leg", day_id="synthetic-day-2", target_id="timeout-leg", message="公共交通查询超时",
            based_on_revision=1,
        )
        day_results = tuple(
            item.model_copy(update={"issue_ids": (*item.issue_ids, "timeout-issue")}) if item.day_id == "synthetic-day-2" else item
            for item in preview.validation.days
        )
        report = preview.validation.model_copy(update={"issues": (*preview.validation.issues, issue), "days": day_results})
        draft = preview.draft.model_copy(update={"legs": (leg,)})
        result = preview.model_copy(update={"draft": draft, "facts": updated_facts, "validation": report})
        validate_preview(result)


if __name__ == "__main__":
    unittest.main()
