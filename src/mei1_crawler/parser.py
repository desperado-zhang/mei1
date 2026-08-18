from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from .hashutil import sha256_json, sha256_text


JsonDict = dict[str, Any]


LIST_ENDPOINT = "/api/member/list/search"
PROFILE_ENDPOINT_MARKERS = (
    "/api/member/detailInfo/",
    "/api/member/detail/",
)


def endpoint_path(url: str) -> str:
    match = re.search(r"https?://[^/]+(?P<path>/[^?#]*)", url)
    return match.group("path") if match else url.split("?", 1)[0]


def is_member_endpoint(path: str) -> bool:
    lower_path = path.lower()
    excluded_markers = ("wechat", "weixin", "wxchat", "/chat", "conversation", "message/record")
    if any(marker in lower_path for marker in excluded_markers):
        return False
    allowed = (
        "/api/member/list/search",
        "/api/member/detailInfo/",
        "/api/member/detail/",
        "/api/member/list/cardAndPresent",
        "/api/member/list/record",
        "/api/member/amount/",
        "/api/member/reachStore/record",
        "/api/member/consumeTotal",
        "/api/member/memberAttr/",
        "/api/memberSurveys/profile/",
        "/api/member/operatorList/",
        "/api/source/list/",
    )
    return any(path.startswith(prefix) for prefix in allowed)


def page_area_for_endpoint(path: str) -> str:
    if path == "/api/member/list/search":
        return "member_list"
    if path.startswith(("/api/member/detailInfo/", "/api/member/detail/")):
        return "member_profile"
    if path == "/api/member/list/cardAndPresent":
        return "member_account"
    if path == "/api/member/list/record":
        return "member_record"
    if path.startswith("/api/member/amount/") or path in {
        "/api/member/reachStore/record",
        "/api/member/consumeTotal",
    }:
        return "member_metrics"
    if path.startswith("/api/memberSurveys/profile/"):
        return "member_survey"
    if path.startswith("/api/member/memberAttr/"):
        return "member_attributes"
    return "member_detail"


def extract_data(payload: Any) -> Any:
    if isinstance(payload, dict):
        for key in ("data", "result", "body"):
            if key in payload:
                return payload[key]
    return payload


def extract_total_count(payload: Any) -> int | None:
    for obj in _walk_dicts(payload):
        for key in ("total", "totalCount", "total_count", "count", "recordsTotal"):
            value = obj.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
    return None


def extract_rows(payload: Any) -> list[JsonDict]:
    data = extract_data(payload)
    direct = _rows_from_common_shapes(data)
    if direct:
        return direct

    best: list[JsonDict] = []
    best_score = 0
    for candidate in _walk_lists(data):
        if not candidate or not all(isinstance(item, dict) for item in candidate[: min(5, len(candidate))]):
            continue
        score = sum(_member_row_score(item) for item in candidate[: min(5, len(candidate))])
        if score > best_score:
            best = [item for item in candidate if isinstance(item, dict)]
            best_score = score
    return best if best_score >= 2 else []


def extract_member_profile(payload: Any) -> JsonDict | None:
    data = extract_data(payload)
    if isinstance(data, dict):
        if _member_row_score(data) > 0:
            return data
        for key in ("member", "memberInfo", "profile", "userInfo"):
            value = data.get(key)
            if isinstance(value, dict):
                return value
    return None


def normalize_member(row: JsonDict, tenant_key: str) -> JsonDict:
    source_member_id = _first_str(row, "id", "memberId", "member_id", "userId", "customerId")
    member_no = _first_str(row, "memberNo", "member_no", "memberNO", "no", "code")
    name = _first_str(row, "name", "memberName", "customerName", "nickName", "realName")
    mobile = _first_str(row, "mobile", "phone", "memberPhone", "telephone", "tel")

    mobile_masked = None
    mobile_sha256 = None
    if mobile:
        if "*" in mobile or len(re.sub(r"\D", "", mobile)) < 8:
            mobile_masked = mobile
        else:
            mobile_sha256 = sha256_text(re.sub(r"\D", "", mobile))

    return {
        "tenant_key": tenant_key,
        "source_member_id": source_member_id,
        "member_no": member_no,
        "name": name,
        "gender": _normalize_gender(_first(row, "gender", "sex")),
        "mobile_masked": mobile_masked,
        "mobile_sha256": mobile_sha256,
        "wechat_account": _first_str(row, "wechatAccount", "wechat", "wechatNo", "wxAccount"),
        "wechat_bound": _normalize_bool(_first(row, "wechatBound", "isBindWechat", "bindWechat")),
        "qq": _first_str(row, "qq", "customerQQ"),
        "email": _first_str(row, "email", "customerEmail"),
        "grade_name": _first_str(row, "gradeName", "levelName", "memberGradeName", "grade"),
        "member_layer": _first_str(row, "memberLayer", "memberLayerName", "classificationName", "memberLevelName"),
        "source_channel": _first_str(row, "sourceName", "sourceChannel", "channelName", "source"),
        "store_id": _first_str(row, "storeId", "belongStoreId"),
        "store_name": _first_str(row, "storeName", "belongStoreName"),
        "tracking_employee_id": _first_str(row, "trackingEmployeeId", "followEmployeeId"),
        "tracking_employee_name": _first_str(row, "trackingEmployeeName", "followEmployeeName"),
        "exclusive_advisor_id": _first_str(row, "counselorId", "advisorId"),
        "exclusive_advisor_name": _first_str(row, "counselorName", "advisorName"),
        "referrer_member_id": _first_str(row, "referrerMemberId", "recommendMemberId"),
        "referrer_name": _first_str(row, "referrerName", "recommendName", "recommender"),
        "occupation": _first_str(row, "occupation", "job", "career"),
        "height_cm": _first_float(row, "height", "customerHeight"),
        "weight_kg": _first_float(row, "weight", "customerWeight"),
        "blood_type": _first_str(row, "bloodType", "customerBloodType"),
        "address": _first_str(row, "address", "customerAddress"),
        "birthday_type": _first_str(row, "birthdayType", "calendarType"),
        "birthday_date": _first_str(row, "birthday", "birthdayDate"),
        "next_birthday_date": _first_str(row, "nextBirthday", "nextBirthdayDate"),
        "age": _first_int(row, "age"),
        "age_group": _first_str(row, "ageGroup"),
        "joined_at": _first_str(row, "joinTime", "joinedAt", "registerDate", "createTime", "createdAt", "entryTime"),
        "note": _first_str(row, "remark", "note", "memo"),
        "raw_profile_json": row,
    }


def normalize_list_observation(row: JsonDict, tenant_key: str, row_index: int) -> JsonDict:
    member = normalize_member(row, tenant_key)
    raw = {
        "tenant_key": tenant_key,
        "source_member_id": member["source_member_id"],
        "member_no": member["member_no"],
        "name": member["name"],
        "mobile_masked": member["mobile_masked"],
        "grade_name": member["grade_name"],
        "card_count": _first_int(row, "cardCount", "cardNum", "cards"),
        "stored_value_balance_cents": _money_cents(_first(row, "storedValueBalance", "cardBalance", "balance")),
        "total_consume_cents": _money_cents(_first(row, "totalConsume", "totalConsumeAmount", "consumeTotal")),
        "total_visit_count": _first_int(row, "totalReachStoreCount", "totalVisitCount", "totalArrivalCount", "arrivalCount", "arriveCount"),
        "current_month_visit_count": _first_int(row, "monthReachStoreCount", "currentMonthVisitCount", "currentMonthCount"),
        "last_consume_at": _first_str(row, "lastConsumeTime", "lastConsumeAt", "lastConsumeDate", "lastOrderTime"),
        "last_service_employee_name": _first_str(row, "lastServiceEmployeeName", "employeeName", "lastEmployeeName")
        or _nested_str(row, ("order", "employee", "employeeName")),
        "last_consume_amount_cents": _money_cents(_first(row, "lastConsumeAmount", "lastOrderAmount")),
        "raw_row_json": row,
    }
    raw["row_fingerprint"] = sha256_json(
        {
            "source_member_id": raw["source_member_id"],
            "member_no": raw["member_no"],
            "name": raw["name"],
            "row_index": row_index,
            "raw": row,
        }
    )
    raw["row_content_hash"] = sha256_json(
        {
            "source_member_id": raw["source_member_id"],
            "member_no": raw["member_no"],
            "name": raw["name"],
            "mobile_masked": raw["mobile_masked"],
            "grade_name": raw["grade_name"],
            "card_count": raw["card_count"],
            "stored_value_balance_cents": raw["stored_value_balance_cents"],
            "total_consume_cents": raw["total_consume_cents"],
            "total_visit_count": raw["total_visit_count"],
            "current_month_visit_count": raw["current_month_visit_count"],
            "last_consume_at": raw["last_consume_at"],
            "last_service_employee_name": raw["last_service_employee_name"],
            "last_consume_amount_cents": raw["last_consume_amount_cents"],
        }
    )
    return raw


def normalize_asset_snapshot(payload: Any, member_id: int) -> JsonDict:
    data = extract_data(payload)
    if not isinstance(data, dict):
        data = {}
    row = {
        "member_id": member_id,
        "member_wallet_cents": _money_cents(_first(data, "wallet", "memberWallet", "walletAmount")),
        "remaining_consume_value_cents": _money_cents(_first(data, "remainConsumeValue", "remainingConsumeValue")),
        "points": _first_int(data, "points", "point", "memberPoints"),
        "debt_cents": _money_cents(_first(data, "debt", "debtAmount", "arrearsAmount")),
        "card_count": _first_int(data, "cardCount", "cardNum"),
        "coupon_count": _first_int(data, "couponCount", "ticketCount"),
        "total_consume_cents": _money_cents(_first(data, "totalConsume", "totalConsumeAmount")),
        "total_card_consumed_cents": _money_cents(_first(data, "totalCardConsumed", "cardConsumeAmount")),
        "referral_count": _first_int(data, "referralCount", "recommendCount"),
        "current_year_consume_rank": _first_int(data, "currentYearConsumeRank", "yearConsumeRank"),
        "lifetime_consume_rank": _first_int(data, "lifetimeConsumeRank", "totalConsumeRank"),
        "total_visit_count": _first_int(data, "totalVisitCount", "totalReachStoreCount", "totalArrivalCount", "arrivalCount"),
        "average_visit_interval_days": _first_float(data, "averageVisitIntervalDays", "avgReachStoreInterval"),
        "lifecycle_category": _first_str(data, "lifecycleCategory", "lifeCycleName"),
        "partner_store_balance_cents": _money_cents(_first(data, "partnerStoreBalance", "storeBalance")),
        "partner_withdrawable_cents": _money_cents(_first(data, "partnerWithdrawable", "withdrawable")),
        "direct_referrer_count": _first_int(data, "directReferrerCount"),
        "indirect_referrer_count": _first_int(data, "indirectReferrerCount"),
        "raw_json": data,
    }
    row["snapshot_hash"] = sha256_json(data)
    return row


def extract_account_items(payload: Any) -> list[JsonDict]:
    data = extract_data(payload)
    if isinstance(data, dict):
        for key in ("cards", "cardList", "holderCards", "memberCards"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]

    best: list[JsonDict] = []
    best_score = 0
    for candidate in _walk_lists(data):
        if not candidate or not all(isinstance(item, dict) for item in candidate[: min(5, len(candidate))]):
            continue
        score = sum(_account_item_score(item) for item in candidate[: min(5, len(candidate))])
        if score > best_score:
            best = [item for item in candidate if isinstance(item, dict)]
            best_score = score
    return best if best_score >= 2 else []


def normalize_account_item(row: JsonDict, member_id: int) -> JsonDict:
    balance_text = _first_str(row, "balance", "remainTimes", "remainingTimes", "remainCount", "remainingCount")
    deposit_text = _first_str(row, "deposit", "totalTimes", "times", "totalCount")
    remaining_times_text = None
    if balance_text and deposit_text:
        remaining_times_text = f"{balance_text}/{deposit_text}"
    elif balance_text:
        remaining_times_text = balance_text

    valid_to = _first_str(row, "endTime", "invalidDate", "validEndTime", "expireTime", "expiredAt")
    return {
        "member_id": member_id,
        "item_scope": "held_card",
        "source_item_id": _first_str(row, "id", "cardId", "holderCardId", "memberCardId"),
        "item_no": _first_str(row, "cardNo", "cardCode", "no", "code"),
        "item_name": _first_str(row, "name", "cardName", "itemName"),
        "item_type": _account_item_type(row),
        "status": _first_str(row, "status", "cardStatus", "state", "delFlag"),
        "source_name": _first_str(row, "sourceName", "merchantName", "storeName", "belongStoreName"),
        "valid_from": _first_str(row, "activeDate", "startTime", "validStartTime", "createTime", "createdAt"),
        "valid_to": valid_to,
        "is_permanent": _normalize_bool(_first(row, "isPermanent", "permanent"))
        if _first(row, "isPermanent", "permanent") is not None
        else int(valid_to is None),
        "deal_price_cents": _money_cents(
            _first(row, "dealPrice", "price", "paidCardPrice", "fixedCardMoney", "realMoney", "buyMoney")
        ),
        "remaining_times_text": remaining_times_text,
        "balance_cents": _money_cents(_first(row, "balance", "cardBalance", "remainMoney"))
        if _is_stored_value_card(row)
        else None,
        "display_balance_text": balance_text,
        "raw_json": row,
    }


def _rows_from_common_shapes(value: Any) -> list[JsonDict]:
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return value
    if not isinstance(value, dict):
        return []
    for key in ("rows", "list", "records", "items", "data"):
        child = value.get(key)
        if isinstance(child, list) and all(isinstance(item, dict) for item in child):
            return child
        if isinstance(child, dict):
            rows = _rows_from_common_shapes(child)
            if rows:
                return rows
    return []


def _walk_lists(value: Any) -> list[list[Any]]:
    found: list[list[Any]] = []
    if isinstance(value, list):
        found.append(value)
        for item in value:
            found.extend(_walk_lists(item))
    elif isinstance(value, dict):
        for child in value.values():
            found.extend(_walk_lists(child))
    return found


def _walk_dicts(value: Any) -> list[JsonDict]:
    found: list[JsonDict] = []
    if isinstance(value, dict):
        found.append(value)
        for child in value.values():
            found.extend(_walk_dicts(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_dicts(child))
    return found


def _member_row_score(row: JsonDict) -> int:
    keys = set(row)
    score = 0
    for names in (
        {"id", "memberId", "member_id", "userId"},
        {"memberNo", "member_no", "memberNO"},
        {"name", "memberName", "customerName"},
        {"mobile", "phone", "memberPhone", "telephone"},
        {"totalConsume", "cardCount", "gradeName", "sourceName"},
    ):
        if keys.intersection(names):
            score += 1
    return score


def _account_item_score(row: JsonDict) -> int:
    keys = set(row)
    score = 0
    for names in (
        {"id", "cardId", "holderCardId", "memberCardId"},
        {"cardNo", "cardCode", "no", "code"},
        {"name", "cardName", "itemName"},
        {"balance", "deposit", "remainTimes", "remainingTimes"},
        {"kind", "categoryLevel", "holderCardType", "cardType"},
    ):
        if keys.intersection(names):
            score += 1
    return score


def _account_item_type(row: JsonDict) -> str | None:
    label = _first_str(row, "categoryLevelName", "cardTypeName", "kindName", "typeName")
    if label:
        return label
    parts = []
    for key in ("kind", "categoryLevel", "holderCardType", "cardType"):
        value = _first_str(row, key)
        if value:
            parts.append(f"{key}:{value}")
    return "|".join(parts) or None


def _is_stored_value_card(row: JsonDict) -> bool:
    category = _first_str(row, "categoryLevel")
    if category == "CD00100001":
        return True
    text = " ".join(
        value
        for value in (
            _first_str(row, "categoryLevelName", "cardTypeName", "kindName", "typeName", "name", "cardName"),
            _first_str(row, "kind", "cardType", "holderCardType"),
        )
        if value
    )
    return bool(re.search(r"储值|充值|现金|wallet|stored|value|cash", text, re.I))


def _first(row: JsonDict, *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _first_str(row: JsonDict, *keys: str) -> str | None:
    value = _first(row, *keys)
    if value is None:
        return None
    return str(value).strip() or None


def _nested_str(row: JsonDict, path: tuple[str, ...]) -> str | None:
    value: Any = row
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    if value is None:
        return None
    return str(value).strip() or None


def _first_int(row: JsonDict, *keys: str) -> int | None:
    value = _first(row, *keys)
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    text = str(value).replace(",", "").strip()
    match = re.search(r"-?\d+", text)
    return int(match.group(0)) if match else None


def _first_float(row: JsonDict, *keys: str) -> float | None:
    value = _first(row, *keys)
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def _money_cents(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    text = str(value).replace("￥", "").replace("¥", "").replace(",", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return int((Decimal(match.group(0)) * Decimal("100")).quantize(Decimal("1")))
    except InvalidOperation:
        return None


def _normalize_bool(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "已绑", "已绑定"}:
        return 1
    if text in {"0", "false", "no", "n", "未绑", "未绑定"}:
        return 0
    return None


def _normalize_gender(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if text in {"1", "男", "male", "M", "m"}:
        return "male"
    if text in {"2", "女", "female", "F", "f"}:
        return "female"
    return text or None
