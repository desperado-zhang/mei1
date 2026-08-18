from __future__ import annotations

import re
from datetime import datetime, timezone
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
    safe_wechat_paths = (
        "/api/wechatbusinessassists/memberservicelist",
    )
    excluded_markers = ("wechat", "weixin", "wxchat", "/chat", "conversation", "message/record")
    if not any(lower_path.startswith(prefix) for prefix in safe_wechat_paths) and any(
        marker in lower_path for marker in excluded_markers
    ):
        return False
    allowed = (
        "/api/member/list/search",
        "/api/member/detailInfo/",
        "/api/member/detail/",
        "/api/member/memberAttr/",
        "/api/member/queryMemberRemainConsumeValue/",
        "/api/member/",
        "/api/member/list/cardAndPresent",
        "/api/member/list/record",
        "/api/member/amount/",
        "/api/member/reachStore/record",
        "/api/member/consumeTotal",
        "/api/wechatbusinessassists/memberServiceList",
        "/api/couponUser/memberCouponSearch",
        "/api/giveTradeRecord/giveFirendSearch",
        "/api/pointsChangeRecord/search",
        "/api/mallItemTrade/mallMemberTrade",
        "/api/dragonflyBrushFace/brushFaceRecord",
        "/api/deposit/depositStock/searchStockListData",
        "/api/deposit/depositOperateRecord/searchRecordListData",
        "/api/storePartner/getStorePartnerByMemberId/",
        "/api/storePartner/subStorePartnerOrderList",
        "/api/storePartnerLevelChange/search",
        "/api/storePartnerAccount/queryByMemberId/",
        "/api/tduckDataProxy/query",
        "/api/memberSurveys/profile/",
        "/api/member/operatorList/",
        "/api/source/list/",
    )
    if not any(path.startswith(prefix) for prefix in allowed):
        return False
    if path.startswith("/api/member/"):
        return bool(
            path.startswith(
                (
                    "/api/member/list/",
                    "/api/member/detailInfo/",
                    "/api/member/detail/",
                    "/api/member/memberAttr/",
                    "/api/member/queryMemberRemainConsumeValue/",
                    "/api/member/amount/",
                    "/api/member/reachStore/record",
                    "/api/member/consumeTotal",
                    "/api/member/operatorList/",
                )
            )
            or re.fullmatch(r"/api/member/[^/?#]+", path)
        )
    return True


def page_area_for_endpoint(path: str) -> str:
    if path == "/api/member/list/search":
        return "member_list"
    if path.startswith(("/api/member/detailInfo/", "/api/member/detail/")):
        return "member_profile"
    if path == "/api/member/list/cardAndPresent":
        return "member_account"
    if path in {
        "/api/couponUser/memberCouponSearch",
        "/api/giveTradeRecord/giveFirendSearch",
        "/api/deposit/depositStock/searchStockListData",
    }:
        return "member_account"
    if path == "/api/wechatbusinessassists/memberServiceList":
        return "member_service_records"
    if path == "/api/member/list/record":
        return "member_detail_records"
    if path in {
        "/api/pointsChangeRecord/search",
        "/api/mallItemTrade/mallMemberTrade",
        "/api/dragonflyBrushFace/brushFaceRecord",
        "/api/deposit/depositOperateRecord/searchRecordListData",
    }:
        return "member_detail_records"
    if path.startswith("/api/member/amount/") or path.startswith("/api/member/queryMemberRemainConsumeValue/") or path in {
        "/api/member/reachStore/record",
        "/api/member/consumeTotal",
    }:
        return "member_metrics"
    if path.startswith("/api/memberSurveys/profile/") or path == "/api/tduckDataProxy/query":
        return "member_survey"
    if path.startswith("/api/member/memberAttr/"):
        return "member_attributes"
    if path.startswith(("/api/storePartner/getStorePartnerByMemberId/", "/api/storePartnerAccount/queryByMemberId/")):
        return "member_partner"
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
        data = {"remainConsumeValue": data}
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


def account_scope_for_endpoint(path: str, request_body: Any = None) -> str | None:
    if path == "/api/member/list/cardAndPresent":
        value = _first_from_any(request_body, "type")
        return {
            "1": "held_card",
            "2": "coupon",
            "3": "present",
            1: "held_card",
            2: "coupon",
            3: "present",
        }.get(value)
    if path == "/api/couponUser/memberCouponSearch":
        return "mall_coupon"
    if path == "/api/giveTradeRecord/giveFirendSearch":
        return "transferred"
    if path == "/api/deposit/depositStock/searchStockListData":
        return "deposit_item"
    return None


def detail_category_for_endpoint(path: str, request_body: Any = None) -> str | None:
    if path == "/api/member/list/record":
        value = _first_from_any(request_body, "type")
        return {
            "1": "appointment",
            "2": "consume",
            "4": "gift",
            "5": "modification",
            "6": "wallet",
            1: "appointment",
            2: "consume",
            4: "gift",
            5: "modification",
            6: "wallet",
        }.get(value, "other")
    if path == "/api/pointsChangeRecord/search":
        return "points"
    if path == "/api/mallItemTrade/mallMemberTrade":
        return "mall_order"
    if path == "/api/dragonflyBrushFace/brushFaceRecord":
        return "face_scan"
    if path == "/api/member/reachStore/record":
        return "reach_store"
    if path == "/api/deposit/depositOperateRecord/searchRecordListData":
        return "deposit"
    return None


def normalize_account_item(row: JsonDict, member_id: int, item_scope: str = "held_card") -> JsonDict:
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
        "item_scope": item_scope,
        "source_item_id": _first_str(
            row,
            "id",
            "cardId",
            "holderCardId",
            "memberCardId",
            "couponId",
            "couponUserId",
            "ticketInstanceId",
            "presentId",
            "presentTypeId",
            "tradeId",
            "depositStockId",
            "depositId",
        ),
        "item_no": _first_str(row, "cardNo", "cardCode", "couponNo", "ticketNo", "no", "code"),
        "item_name": _first_str(
            row,
            "name",
            "cardName",
            "couponName",
            "ticketName",
            "presentName",
            "productName",
            "goodsName",
            "itemName",
            "serviceItemName",
            "title",
        ),
        "item_type": _account_item_type(row),
        "status": _first_str(row, "statusName", "status", "cardStatus", "state", "delFlag"),
        "source_name": _first_str(
            row,
            "sourceName",
            "source",
            "giveSource",
            "couponSource",
            "merchantName",
            "storeName",
            "belongStoreName",
        ),
        "valid_from": _first_str(
            row,
            "activeDate",
            "startTime",
            "validStartTime",
            "createTime",
            "createdAt",
            "giveTime",
            "receiveTime",
        ),
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


def normalize_service_record(row: JsonDict, member_id: int) -> JsonDict:
    record_type = _service_record_type(_first(row, "type", "recordType"))
    record_at = _first_datetime_str(
        row,
        "createTime",
        "createDate",
        "recordTime",
        "recordDate",
        "createTimestamp",
        "lastUpdateTimestamp",
    )
    content = _content_text(row)
    normalized = {
        "member_id": member_id,
        "source_record_id": _first_str(row, "id", "recordId", "serviceRecordId"),
        "record_type": record_type,
        "record_at": record_at,
        "employee_id": _first_str(row, "employeeId", "operatorId", "userId", "creatorId"),
        "employee_name": _first_str(row, "employeeName", "operatorName", "userName", "creatorName", "name"),
        "content": content,
        "related_items_text": _joined_text(
            _first(row, "serviceItemNames", "serviceItems", "projectNames", "consumeContentList", "itemNames")
        ),
        "raw_json": row,
    }
    normalized["record_hash"] = sha256_json(
        {
            "source_record_id": normalized["source_record_id"],
            "record_type": normalized["record_type"],
            "record_at": normalized["record_at"],
            "content": normalized["content"],
            "raw": row if not normalized["source_record_id"] else None,
        }
    )
    return normalized


def normalize_detail_record(row: JsonDict, member_id: int, category: str) -> JsonDict:
    happened_at = _first_datetime_str(
        row,
        "appointmentTime",
        "bookingTime",
        "startTime",
        "consumeTime",
        "consumeDate",
        "giveTime",
        "operationTime",
        "operateTime",
        "createTime",
        "createdAt",
        "reachStoreDate",
        "recordTime",
        "payTime",
    )
    content = _content_text(row)
    normalized = {
        "member_id": member_id,
        "category": category,
        "source_record_id": _first_str(
            row,
            "id",
            "recordId",
            "orderId",
            "appointmentId",
            "tradeId",
            "orderIds",
            "ticketId",
            "depositRecordId",
        ),
        "happened_at": happened_at,
        "title": _first_str(
            row,
            "title",
            "name",
            "itemName",
            "productName",
            "goodsName",
            "cardName",
            "couponName",
            "typeName",
            "ruleName",
        ),
        "status": _first_str(row, "statusName", "status", "state", "recordStatus"),
        "amount_cents": _money_cents(
            _first(
                row,
                "amount",
                "money",
                "consumeMoney",
                "consumeAmount",
                "cardMoney",
                "achievementMoney",
                "payAmount",
                "totalAmount",
                "walletAmount",
            )
        ),
        "store_id": _first_str(row, "storeId", "orderStoreId", "reachStoreId"),
        "store_name": _first_str(row, "storeName", "orderStoreName", "reachStoreName", "consumeStoreName"),
        "employee_id": _first_str(row, "employeeId", "operatorId", "serviceEmployeeId", "userId"),
        "employee_name": _first_str(row, "employeeName", "operatorName", "serviceEmployeeName", "userName", "technicianName"),
        "room_name": _first_str(row, "roomName", "room"),
        "content": content,
        "order_no": _first_str(row, "orderNo", "orderCode", "tradeNo", "orderSn", "tradeSn"),
        "duration_minutes": _duration_minutes(_first(row, "duration", "appointmentDuration", "serviceDuration")),
        "raw_json": row,
    }
    normalized["record_hash"] = sha256_json(
        {
            "category": category,
            "source_record_id": normalized["source_record_id"],
            "happened_at": normalized["happened_at"],
            "title": normalized["title"],
            "content": normalized["content"],
            "order_no": normalized["order_no"],
            "raw": row if not normalized["source_record_id"] else None,
        }
    )
    return normalized


def extract_attachments(payload: Any) -> list[JsonDict]:
    data = extract_data(payload)
    rows: list[JsonDict] = []
    for key in ("enclosures", "attachments", "files", "memberFiles"):
        value = _first(data, key)
        if isinstance(value, list):
            rows.extend(item for item in value if isinstance(item, dict))
    return rows


def normalize_attachment(row: JsonDict, member_id: int) -> JsonDict:
    url = _first_str(row, "url", "fileUrl", "src", "path")
    source_file_id = _first_str(row, "id", "fileId", "sourceFileId", "enclosureId")
    if not source_file_id:
        source_file_id = "hash:" + sha256_json(row)[:24]
    return {
        "member_id": member_id,
        "source_file_id": source_file_id,
        "file_name": _first_str(row, "name", "fileName", "title"),
        "content_type": _first_str(row, "contentType", "mimeType", "fileType", "type"),
        "file_url_hash": sha256_text(url) if url else None,
        "note": _first_str(row, "note", "remark", "description"),
        "uploaded_at": _first_datetime_str(row, "createTime", "createdAt", "uploadTime", "lastUpdateTime"),
        "raw_json": row,
    }


def normalize_survey_profile(row: JsonDict, member_id: int) -> JsonDict:
    source_profile_id = _first_str(row, "id", "profileId", "formId", "recordId", "submitId")
    if not source_profile_id:
        source_profile_id = "hash:" + sha256_json(row)[:24]
    return {
        "member_id": member_id,
        "source_profile_id": source_profile_id,
        "profile_name": _first_str(row, "name", "profileName", "formName", "title"),
        "profile_url": _first_str(row, "url", "profileUrl", "formUrl"),
        "field_values_json": _first(row, "fieldValues", "answers", "formData", "data"),
        "raw_json": row,
    }


def normalize_partner_info(payload: Any, member_id: int) -> JsonDict | None:
    data = extract_data(payload)
    if not isinstance(data, dict):
        return None
    partner_member_id = _first_str(data, "id", "partnerId", "partnerMemberId", "memberId")
    if not partner_member_id:
        partner_member_id = "hash:" + sha256_json(data)[:24]
    return {
        "member_id": member_id,
        "partner_member_id": partner_member_id,
        "partner_level": _first_str(data, "levelName", "partnerLevel", "gradeName"),
        "store_balance_cents": _money_cents(_first(data, "storeBalance", "storeAmount", "consumeBalance")),
        "withdrawable_cents": _money_cents(_first(data, "withdrawable", "withdrawableAmount", "canWithdrawAmount")),
        "direct_referrer_name": _first_str(data, "directReferrerName", "directRecommendName", "parentName"),
        "indirect_referrer_name": _first_str(data, "indirectReferrerName", "grandParentName"),
        "raw_json": data,
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
        {"id", "cardId", "holderCardId", "memberCardId", "couponId", "presentId", "tradeId", "depositStockId"},
        {"cardNo", "cardCode", "couponNo", "ticketNo", "orderNo", "no", "code"},
        {"name", "cardName", "couponName", "presentName", "productName", "goodsName", "itemName"},
        {"balance", "deposit", "remainTimes", "remainingTimes", "remainCount"},
        {"kind", "categoryLevel", "holderCardType", "cardType", "couponType", "presentType"},
    ):
        if keys.intersection(names):
            score += 1
    return score


def _account_item_type(row: JsonDict) -> str | None:
    label = _first_str(row, "categoryLevelName", "cardTypeName", "couponTypeName", "presentTypeName", "kindName", "typeName")
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


def _service_record_type(value: Any) -> str:
    text = str(value).strip() if value not in (None, "") else ""
    if text in {"1", "2", "return_visit"} or "回访" in text:
        return "return_visit"
    if text in {"3", "service_note"} or "服务" in text:
        return "service_note"
    if text in {"4", "development_plan"} or "开发" in text:
        return "development_plan"
    return "other"


def _content_text(row: JsonDict) -> str | None:
    direct = _first_str(
        row,
        "content",
        "remark",
        "note",
        "memo",
        "operationContent",
        "operateContent",
        "changeContent",
        "consumeContent",
        "giveContent",
        "recordContent",
        "description",
    )
    if direct:
        return direct
    parts = []
    for key in (
        "consumeContentList",
        "serviceItemNames",
        "projectNames",
        "itemNames",
        "productNames",
        "goodsNames",
        "employeeNameList",
    ):
        text = _joined_text(row.get(key))
        if text:
            parts.append(text)
    return " | ".join(parts) or None


def _joined_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                text = _first_str(item, "name", "title", "itemName", "productName", "employeeName")
                if text:
                    parts.append(text)
            elif item not in (None, ""):
                parts.append(str(item).strip())
        return ", ".join(part for part in parts if part) or None
    if isinstance(value, dict):
        return _first_str(value, "name", "title", "itemName", "productName")
    return str(value).strip() or None


def _first_datetime_str(row: JsonDict, *keys: str) -> str | None:
    value = _first(row, *keys)
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp = timestamp / 1000
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{13}", text):
        return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if re.fullmatch(r"\d{10}", text):
        return datetime.fromtimestamp(int(text), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return text


def _duration_minutes(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, int):
        return value
    text = str(value).replace(",", "")
    match = re.search(r"-?\d+", text)
    return int(match.group(0)) if match else None


def _first_from_any(value: Any, *keys: str) -> Any:
    if isinstance(value, dict):
        if value.get("field") in keys and value.get("value") not in (None, ""):
            return value.get("value")
        for key in keys:
            if key in value and value[key] not in (None, ""):
                return value[key]
        for child in value.values():
            found = _first_from_any(child, *keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_from_any(child, *keys)
            if found is not None:
                return found
    return None


def _first(row: Any, *keys: str) -> Any:
    if not isinstance(row, dict):
        return None
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
