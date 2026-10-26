"""住宿按原文确认的日期范围复用；不根据偏好或路线自动拆分。"""
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from app.models.planning_v4.input import validate_requirement_sources, HardRequirement


@dataclass(frozen=True)
class LodgingStay:
    day_ids: tuple[str, ...]
    city: str
    lodging_query: str | None = None
    lodging_poi_id: str | None = None
    scope_required: bool = False

    @property
    def specified(self):
        return bool(self.lodging_poi_id or (self.lodging_query and self.lodging_query.strip()))


def validate_lodging_changes(request, changes):
    zone = ZoneInfo(request.timezone)
    start = request.arrival_at.astimezone(zone).date()
    end = request.departure_at.astimezone(zone).date()
    dates = [change.start_date for change in changes]
    if len(dates) != len(set(dates)) or any(day < start or day > end for day in dates):
        raise ValueError("住宿变更日期必须在旅行范围内且唯一")
    for change in changes:
        if change.source is None:
            continue  # 原生需求只检查日期范围/冲突，不检查引用和名称逐字关系。
        if change.source.field not in {"remarks", "preferences", "lodging_query"}:
            raise ValueError("住宿变更必须引用用户的住宿要求")
        validate_requirement_sources(request, (HardRequirement(
            requirement_id="lodging_change", kind="must_include", text="住宿变更", source=change.source,
        ),))
        for value in (change.city, change.lodging_query):
            if value is not None and (not value.strip() or value.strip() not in change.source.quote):
                raise ValueError("住宿变更的城市和住处名称必须逐字来自原文")


def lodging_stays(planning_input):
    request = planning_input.request
    changes = planning_input.requirements.lodging_changes
    validate_lodging_changes(request, changes)
    by_date = {change.start_date: change for change in changes}
    city, query, poi_id = request.city, request.lodging_query, request.lodging_poi_id
    result, days = [], []
    for day in planning_input.days:
        change = by_date.get(day.day_date)
        if change is not None:
            if days:
                result.append(LodgingStay(tuple(days), city, query, poi_id, bool(changes)))
            days = []
            city, query, poi_id = change.city or city, change.lodging_query, None
        days.append(day.day_id)
    if days:
        result.append(LodgingStay(tuple(days), city, query, poi_id, bool(changes)))
    return tuple(result)


def stay_for_days(planning_input, day_ids):
    chosen = set(day_ids)
    matches = [stay for stay in lodging_stays(planning_input) if chosen and chosen.issubset(stay.day_ids)]
    if len(matches) != 1:
        raise ValueError("住宿任务必须属于一个连续住宿范围")
    return matches[0]


def city_for_days(planning_input, day_ids):
    chosen = set(day_ids)
    stays = lodging_stays(planning_input)
    if not chosen and len({stay.city for stay in stays}) == 1:
        return stays[0].city  # 无日期的命名地点核实仍使用唯一已确认城市。
    cities = {stay.city for stay in stays if chosen.intersection(stay.day_ids)}
    if not chosen or not chosen.issubset({day for stay in stays for day in stay.day_ids}) or len(cities) != 1:
        raise ValueError("地点任务必须属于一个已确认城市")
    return cities.pop()


def candidate_for_stay(stay, candidates, facts, area_hints, *, excluded_poi_ids=()):
    from app.planning.fact_policy import _city_matches
    from app.planning.fact_policy import _lodging_category, _matches_original_query, _is_region_or_business_area
    places = {place.fact_id: place for place in facts.places}
    sources = {source.tool_call_id for source in facts.sources}
    areas = {hint.area_hint_id for hint in area_hints}
    matches = []
    for candidate in candidates:
        if candidate.category != "lodging" or candidate.poi_id in excluded_poi_ids:
            continue
        if stay.scope_required and not candidate.lodging_day_ids:
            continue
        if candidate.lodging_day_ids and not set(stay.day_ids).issubset(candidate.lodging_day_ids):
            continue
        place = places.get(candidate.place_fact_id)
        if (place is None or place.poi_id != candidate.poi_id or place.location is None
                or not _city_matches(stay.city, place.city) or not place.source_refs
                or not set(place.source_refs).issubset(sources)):
            continue
        if stay.specified:
            exact = place.poi_id == stay.lodging_poi_id if stay.lodging_poi_id else _matches_original_query(place, stay.lodging_query)
            if not exact or candidate.area_hint_id is not None or _is_region_or_business_area(place):
                continue
        elif _lodging_category(place) != "lodging" or candidate.area_hint_id not in areas:
            continue
        matches.append(candidate)
    if stay.specified and len({candidate.poi_id for candidate in matches}) != 1:
        return None
    # 保留已收集顺序：第一次可用酒店成为本范围的固定住处，后续日期不再另选。
    return next(iter(matches), None)


def previous_lodging_poi_ids(planning_input, stay, candidates, facts, areas):
    stays = lodging_stays(planning_input)
    index = stays.index(stay)
    if index == 0 or stay.specified or stays[index - 1].city != stay.city:
        return ()
    previous = choose_lodging_for_stay(planning_input, stays[index - 1], candidates, facts, areas)
    return (previous.poi_id,) if previous else ()


def choose_lodging_for_stay(planning_input, stay, candidates, facts, areas):
    return candidate_for_stay(stay, candidates, facts, areas, excluded_poi_ids=previous_lodging_poi_ids(
        planning_input, stay, candidates, facts, areas,
    ))


def pending_lodging_stays(planning_input, candidates, facts, areas):
    return tuple(stay for stay in lodging_stays(planning_input)
                 if not stay.specified and choose_lodging_for_stay(planning_input, stay, candidates, facts, areas) is None)
