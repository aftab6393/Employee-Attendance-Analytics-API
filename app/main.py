"""Employee attendance API. All instants are stored in UTC and exposed as epoch ms."""
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError
from bson import json_util

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = (*PRESENCE, "ABSENT", "LEAVE")
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def ensure_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_emp")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_day")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp")
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], name="date_status_emp")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp")


def _error(status: int, detail: str):
    raise HTTPException(status, detail)


def _whole_second(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0)


def _timestamp(ms: Optional[int]) -> datetime:
    if ms is None:
        return _whole_second(datetime.now(UTC))
    if isinstance(ms, bool) or not isinstance(ms, int) or not 100_000_000_000 <= ms <= 4_102_444_800_000:
        _error(422, "timestamp must be epoch milliseconds in the supported range")
    return datetime.fromtimestamp(ms / 1000, UTC).replace(microsecond=0)


def _ms(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def _api_doc(doc):
    if not doc:
        return doc
    doc.pop("_id", None)
    for key in ("punch_in", "punch_out"):
        doc[key] = _ms(doc.get(key))
    doc["half_day"] = bool(doc.get("half_day", False))
    doc["history"] = doc.get("history", [])
    for entry in doc["history"]:
        entry["at"] = _ms(entry.get("at"))
        for field, change in entry.get("changes", {}).items():
            if field in ("punch_in", "punch_out"):
                change["from"] = _ms(change.get("from"))
                change["to"] = _ms(change.get("to"))
    if "created_at" in doc:
        doc["created_at"] = _ms(doc["created_at"])
    return doc


def _employee_api(doc):
    doc.pop("_id", None)
    doc["created_at"] = _ms(doc.get("created_at"))
    return doc


def _shift_dt(day: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(day, time(h, m), tzinfo=IST).astimezone(UTC)


def _attendance_date(punch_in: datetime, emp: dict) -> str:
    local = punch_in.astimezone(IST)
    day = local.date()
    if emp["shift_end"] <= emp["shift_start"] and local.strftime("%H:%M") < emp["shift_end"]:
        day -= timedelta(days=1)
    return day.isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_day: str) -> int:
    start = _shift_dt(date.fromisoformat(attendance_day), shift_start)
    elapsed = int((punch_in - start).total_seconds())
    return elapsed // 60 if elapsed > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    hours = Decimal(int((punch_out - punch_in).total_seconds())) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end: str, attendance_day: str, shift_start: str) -> int:
    day = date.fromisoformat(attendance_day)
    if shift_end <= shift_start:
        day += timedelta(days=1)
    elapsed = int((punch_out - _shift_dt(day, shift_end)).total_seconds())
    return elapsed // 60 if elapsed >= 1800 else 0


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: date

    @model_validator(mode="after")
    def distinct_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True, ge=100_000_000_000, le=4_102_444_800_000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True, ge=100_000_000_000, le=4_102_444_800_000)


class RegularizeIn(StrictModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(default=None, strict=True, ge=100_000_000_000, le=4_102_444_800_000)
    punch_out: Optional[int] = Field(default=None, strict=True, ge=100_000_000_000, le=4_102_444_800_000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        _error(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["joined_on"] = doc["joined_on"].isoformat()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        _error(409, "emp_code already exists")
    return _api_doc(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    docs = db.employees.find(q).sort("emp_code", ASCENDING).skip((page-1)*page_size).limit(page_size)
    return {"items": [_employee_api(doc) for doc in docs], "total": db.employees.count_documents(q), "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if not emp:
        _error(404, "employee not found")
    ts = _timestamp(body.punched_at)
    d = _attendance_date(ts, emp)
    doc = {"emp_code": body.emp_code, "date": d, "status": body.status, "punch_in": ts, "punch_out": None,
           "work_hours": None, "late_minutes": compute_late_minutes(ts, emp["shift_start"], d),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        _error(409, "already punched in for this date")
    return _api_doc(doc)


def _attendance_filter(emp_code, date_from, date_to, status):
    if date_from:
        _valid_date(date_from)
    if date_to:
        _valid_date(date_to)
    if date_from and date_to and date_from > date_to:
        _error(422, "date_from must not be after date_to")
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from
        if date_to: q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = _attendance_filter(emp_code, date_from, date_to, status)
    docs = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page-1)*page_size).limit(page_size)
    return {"items": [_api_doc(d) for d in docs], "total": db.attendance_logs.count_documents(q), "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if not emp:
        _error(404, "employee not found")
    ts = _timestamp(body.punched_at)
    rec = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if not rec:
        _error(404, "no punch-in found")
    pin = _whole_second(rec["punch_in"])
    if rec.get("punch_out") is not None:
        _error(409, "record is already punched out")
    if ts <= pin or ts - pin > timedelta(hours=24):
        _error(422, "punch-out must be after punch-in and within 24 hours")
    hours = compute_work_hours(pin, ts)
    update = {"punch_out": ts, "work_hours": hours,
              "overtime_minutes": compute_overtime(ts, emp["shift_end"], rec["date"], emp["shift_start"]),
              "half_day": hours < 4.50}
    updated = db.attendance_logs.find_one_and_update({"_id": rec["_id"], "punch_out": None}, {"$set": update}, return_document=ReturnDocument.AFTER)
    if not updated:
        _error(409, "record is already punched out")
    return _api_doc(updated)


def _valid_date(value):
    try:
        date.fromisoformat(value)
        if len(value) != 10: raise ValueError
    except (TypeError, ValueError):
        _error(422, "date must be YYYY-MM-DD")


def _half_up(value, digits=2):
    return float(Decimal(str(value)).quantize(Decimal("1." + "0"*digits), rounding=ROUND_HALF_UP))


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    _valid_date(date)
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp or not db.attendance_logs.find_one({"emp_code": emp_code, "date": date}):
        _error(404, "employee or attendance record not found")
    original = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    before = {k: original.get(k) for k in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")}
    status = body.status or original["status"]
    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            _error(422, "ABSENT and LEAVE cannot include punch times")
        pin = pout = None
    else:
        pin = _timestamp(body.punch_in) if body.punch_in is not None else original.get("punch_in")
        pout = _timestamp(body.punch_out) if body.punch_out is not None else original.get("punch_out")
        if pin is None:
            _error(422, "presence status requires punch_in")
        if _attendance_date(pin, emp) != date:
            _error(422, "punch_in must belong to attendance date")
        if pout is not None and (pout <= pin or pout - pin > timedelta(hours=24)):
            _error(422, "punch_out must be after punch_in and within 24 hours")
    late = compute_late_minutes(pin, emp["shift_start"], date) if pin else 0
    hours = compute_work_hours(pin, pout) if pin and pout else None
    overtime = compute_overtime(pout, emp["shift_end"], date, emp["shift_start"]) if pin and pout else 0
    half = bool(hours is not None and hours < 4.50)
    after = {"status": status, "punch_in": pin, "punch_out": pout, "work_hours": hours, "late_minutes": late, "overtime_minutes": overtime, "half_day": half}
    changes = {k: {"from": before[k], "to": after[k]} for k in after if before[k] != after[k]}
    if not changes:
        _error(422, "request does not change the record")
    entry = {"at": _whole_second(datetime.now(UTC)), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    selector = {"_id": original["_id"]}
    if "history" in original:
        selector["history"] = original["history"]
    else:
        selector["$or"] = [{"history": {"$exists": False}}, {"history": []}]
    result = db.attendance_logs.find_one_and_update(selector,
        {"$set": after, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if not result:
        _error(409, "record changed concurrently; retry")
    return _api_doc(result)


def _month_bounds(month):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        _error(422, "month must be YYYY-MM")
    start = date.fromisoformat(month + "-01")
    end = date(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1) - timedelta(days=1)
    return start, end


def _month_pipeline(match, month):
    start, end = _month_bounds(month)
    return [
        {"$match": match},
        {"$addFields": {"_weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, "_half": {"$ifNull": ["$half_day", False]}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$in": ["$_weekday", [2,3,4,5,6]]}]}, {"$cond": ["$_half", 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}},
        {"$project": {"_id": 0, "present_days": 1, "leave_days": 1, "late_count": 1, "total_late_minutes": 1, "total_overtime_minutes": 1}}
    ], (end-start).days + 1


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, end = _month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp: _error(404, "employee not found")
    wdstart = max(start, date.fromisoformat(emp["joined_on"]))
    working = sum(1 for n in range(max(0, (end-wdstart).days+1)) if (wdstart+timedelta(days=n)).weekday() < 5)
    p, _ = _month_pipeline({"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}, month)
    a = next(db.attendance_logs.aggregate(p), {"present_days": 0, "leave_days": 0, "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0})
    a["present_days"] = _half_up(a["present_days"])
    pct = _half_up(a["present_days"] / working * 100) if working else None
    return {"emp_code": emp_code, "month": month, "working_days": working, **a, "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start, end = _month_bounds(month)
    # Employee-first lookup retains zero-log employees and applies R9 to headcount.
    pipeline = [{"$match": {"joined_on": {"$lte": end.isoformat()}, **({"department": department} if department is not None else {})}},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start.isoformat()]}, {"$lte": ["$date", end.isoformat()]}]}}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$addFields": {"_weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", start.isoformat()]}}}}, "_half": {"$ifNull": ["$logs.half_day", False]}}},
        {"$group": {"_id": "$department", "headcount": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$in": ["$_weekday", [2,3,4,5,6]]}]}, {"$cond": ["$_half", 0.5, 1]}, 0]}},
            "work_hours": {"$avg": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", None]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}}, "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}}, "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$project": {"department": "$_id", "_id": 0, "headcount": {"$size": "$headcount"}, "present_days": {"$round": ["$present_days", 2]}, "avg_work_hours": {"$cond": [{"$eq": ["$work_hours", None]}, None, {"$divide": [{"$floor": {"$add": [{"$multiply": ["$work_hours", 100]}, 0.5]}}, 100]}]}, "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}}, {"$sort": {"department": 1}}]
    rows = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": rows}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start, end = _month_bounds(month)
    pipeline = [{"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}}, {"$unwind": "$employee"}]
    if department is not None: pipeline.append({"$match": {"employee.department": department}})
    pipeline += [{"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}}, {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}}]
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


def _trend_pipeline(department, start, end):
    startdt = datetime.combine(start, time.min, tzinfo=UTC)
    enddt = datetime.combine(end, time.min, tzinfo=UTC)
    return [
        {"$match": {"department": department}},
        {"$set": {"_day": startdt}},
        {"$densify": {"field": "_day", "partitionByFields": ["emp_code"], "range": {"step": 1, "unit": "day", "bounds": [startdt, enddt + timedelta(days=1)]}}},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code", "day": {"$dateToString": {"date": "$_day", "format": "%Y-%m-%d"}}}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$eq": ["$date", "$$day"]}]}}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$_day", "headcount": {"$sum": {"$cond": [{"$lte": ["$joined_on", {"$dateToString": {"date": "$_day", "format": "%Y-%m-%d"}}]}, 1, 0]}}, "present_count": {"$sum": {"$cond": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]}, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$set": {"is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$_id"}, 2]}, {"$lte": [{"$dayOfWeek": "$_id"}, 6]}]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]}, 0.5]}}, 10000]}, None]}}},
        {"$setWindowFields": {"sortBy": {"_id": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": {"$dateToString": {"date": "$_id", "format": "%Y-%m-%d"}}, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": {"$cond": [{"$eq": ["$moving_avg_7d", None]}, None, {"$divide": [{"$floor": {"$add": [{"$multiply": ["$moving_avg_7d", 10000]}, 0.5]}}, 10000]}]}}},
        {"$sort": {"date": 1}}
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    _valid_date(from_); _valid_date(to)
    start, end = date.fromisoformat(from_), date.fromisoformat(to)
    days = (end-start).days+1
    if days <= 0 or days > 92: _error(422, "range must be 1 to 92 days and to >= from")
   # if not db.employees.find_one({"department": department}, {"_id": 1}): _error(404, "department not found")
    if not db.employees.find_one({"department": department}, {"_id": 1}): _error(404, "department not found")
    #return {"department": department, "items": list(db.employees.aggregate(_trend_pipeline(department, start, end)))}
    return {"department": department, "items": list(db.employees.aggregate(_trend_pipeline(department, start, end)))}



def _jsonable(value):
    return json_util.loads(json_util.dumps(value))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,
                     department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),
                     date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None,
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                     from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None):
    if endpoint == "attendance_list":
        q = _attendance_filter(emp_code, date_from, date_to, status)
        find_command = {"find": "attendance_logs", "filter": q,
                        "sort": {"date": -1, "emp_code": 1},
                        "skip": (page - 1) * page_size, "limit": page_size}
        collection = "attendance_logs"
        raw = db.command("explain", find_command, verbosity="executionStats")
    elif endpoint == "employee_monthly":
        if not emp_code or not month: _error(422, "emp_code and month are required")
        start, end = _month_bounds(month)
        p, _ = _month_pipeline({"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}, month)
        collection = "attendance_logs"; raw = db.command("explain", {"aggregate": "attendance_logs", "pipeline": p, "cursor": {}}, verbosity="executionStats")
    elif endpoint == "department_summary":
        if not month: _error(422, "month is required")
        start, end = _month_bounds(month)
        # Explain the indexed employee-driven join used to preserve zero-log headcount.
        q = {"joined_on": {"$lte": end.isoformat()}, **({"department": department} if department else {})}
        collection = "employees"; raw = db.command("explain", {"find": "employees", "filter": q}, verbosity="executionStats")
    elif endpoint == "late_leaderboard":
        if not month: _error(422, "month is required")
        start, end = _month_bounds(month)
        q = {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}
        collection = "attendance_logs"; raw = db.command("explain", {"find": "attendance_logs", "filter": q}, verbosity="executionStats")
    elif endpoint == "department_trend":
        if not department or not from_ or not to: _error(422, "department, from and to are required")
        _valid_date(from_); _valid_date(to)
        start, end = date.fromisoformat(from_), date.fromisoformat(to)
        if end < start or (end-start).days+1 > 92: _error(422, "invalid date range")
        collection = "employees"; raw = db.command("explain", {"aggregate": "employees", "pipeline": _trend_pipeline(department, start, end), "cursor": {}}, verbosity="executionStats")
    else:
        _error(422, "unknown endpoint")
    return {"endpoint": endpoint, "collection": collection, "explain": _jsonable(raw)}
