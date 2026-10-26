"""地点类别、城市及天气字段的纯判断；兼容入口统一转发。"""
import re
import math
import unicodedata
from typing import Literal
from app.models.planning_v4.facts import PlaceFact, WeatherForecast

_SPACE = re.compile(r"\s+")


_NON_ATTRACTION_LABELS = (
    "餐饮", "中餐厅", "西餐厅", "快餐", "咖啡厅", "酒吧", "酒店", "宾馆", "旅馆", "住宿",
    "购物", "商店", "超市", "便利店", "汽车服务", "银行", "医疗保健",
)


_VISITOR_CULTURE_LABELS = (
    "博物馆", "纪念馆", "美术馆", "展览馆", "科技馆", "天文馆", "陈列馆", "遗址", "古迹",
    "历史建筑", "名人故居", "文化遗产", "文化宫",
)


def _normalize(value: str) -> str:
    return _SPACE.sub("", unicodedata.normalize("NFKC", value)).casefold()


def _city_matches(expected: str, actual: str | None) -> bool:
    if not actual:
        return False
    left, right = _normalize(expected), _normalize(actual)
    for suffix in ("特别行政区", "自治州", "地区", "市"):
        if left.endswith(_normalize(suffix)):
            left = left[:-len(_normalize(suffix))]
        if right.endswith(_normalize(suffix)):
            right = right[:-len(_normalize(suffix))]
    return left == right


def _category_status(place: PlaceFact) -> Literal["sightseeing", "excluded", "unknown"]:
    """只根据供应商类型证据认定景点，不采信模型或 POI 名称。"""
    type_name = _normalize(place.type_name or "")
    typecode = re.sub(r"\D", "", place.typecode or "")
    if any(_normalize(label) in type_name for label in _NON_ATTRACTION_LABELS):
        return "excluded"
    # AMap 的 11 类是风景名胜；文化场馆用供应商返回的具体类别名识别，
    # 不把整个科教类别都放行，避免学校、培训机构等进入景点候选。
    if typecode.startswith("11"):
        return "sightseeing"
    if any(_normalize(label) in type_name for label in _VISITOR_CULTURE_LABELS):
        return "sightseeing"
    if any(_normalize(label) in type_name for label in ("公园", "动物园", "植物园", "游乐园", "风景名胜")):
        return "sightseeing"
    return "unknown"

def _dining_category(place: PlaceFact) -> Literal["dining", "excluded", "unknown"]:
    typecode = re.sub(r"\D", "", place.typecode or "")
    type_name = _normalize(place.type_name or "")
    if typecode:
        return "dining" if typecode.startswith("05") else "excluded"
    if any(word in type_name for word in (
        "餐饮服务", "中餐厅", "外国餐厅", "快餐厅", "小吃店", "餐厅", "餐馆", "饭店", "饮品店",
    )):
        return "dining"
    return "unknown"

def _lodging_category(place: PlaceFact) -> Literal["lodging", "excluded"]:
    """推荐模式只接受供应商明确标成住宿服务的类别。"""
    typecode = re.sub(r"\D", "", place.typecode or "")
    if typecode:
        return "lodging" if typecode.startswith("10") else "excluded"
    type_name = _normalize(place.type_name or "")
    return "lodging" if any(label in type_name for label in (
        "酒店", "宾馆", "旅馆", "民宿", "住宿服务", "青年旅舍",
    )) else "excluded"


def _matches_original_query(place: PlaceFact, query: str) -> bool:
    normalized_query = _normalize(query)
    return bool(normalized_query) and normalized_query in {
        _normalize(place.name), _normalize(place.address or ""),
    }


def _is_specific_residence(place: PlaceFact) -> bool:
    typecode = re.sub(r"\D", "", place.typecode or "")
    type_name = _normalize(place.type_name or "")
    return typecode.startswith("12") or any(label in type_name for label in (
        "住宅", "小区", "公寓", "楼宇", "居民楼", "商务住宅",
    ))


def _is_region_or_business_area(place: PlaceFact) -> bool:
    """供应商地名/商圈类别表示区域位置，不是用户的具体住处。"""
    if _is_specific_residence(place):
        return False
    typecode = re.sub(r"\D", "", place.typecode or "")
    type_name = _normalize(place.type_name or "")
    return typecode.startswith("19") or any(label in type_name for label in (
        "行政区划", "行政区", "普通地名", "地名地址信息", "商圈名", "商圈", "区域名",
    ))

_WEATHER_TEXT_FIELDS = (
    "day_weather", "night_weather", "day_wind", "night_wind", "day_power", "night_power",
)


_WEATHER_VALUE_FIELDS = (*_WEATHER_TEXT_FIELDS, "day_temp_c", "night_temp_c")


def _has_valid_weather_fields(forecast: WeatherForecast) -> bool:
    for name in _WEATHER_VALUE_FIELDS:
        value = getattr(forecast, name)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return True
    return False
