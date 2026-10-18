import json
from utilities.cache import (
    generate_cache_key,
    get_cached_function_call,
    set_cached_function_call,
)
from models.google_calender_db import CalendarEventDB
from engine import get_calendar_engine
from fastapi import APIRouter
from schemas import (
    FunctionCallResponse,
    ScheduleCalendarEventFunction,
    UserScheduleRequest,
)
from sqlmodel import Session

event_parser_router = APIRouter()


@event_parser_router.post("/api/v1/parse-event", response_model=FunctionCallResponse)
def parse_calendar_event(payload: UserScheduleRequest):
    cache_key = generate_cache_key(payload.request_text)
    cached_result = get_cached_function_call(cache_key)

    if cached_result:
        return FunctionCallResponse(
            id=0,
            cached=True,
            function_call=ScheduleCalendarEventFunction.model_validate(cached_result),
        )

    engine_inst = get_calendar_engine()
    function_call = engine_inst.extract_calendar_function(payload.request_text)

    # Cache extraction
    set_cached_function_call(cache_key, function_call.model_dump())

    # Persist to database
    with Session(engine_inst) as session:
        record = CalendarEventDB(
            request_text=payload.request_text,
            summary=function_call.summary,
            start_time=function_call.start.date_time,
            end_time=function_call.end.date_time,
            attendees_json=json.dumps(function_call.attendees),
            status="parsed",
            raw_function_call_json=function_call.model_dump_json(),
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        rec_id = record.id

    return FunctionCallResponse(id=rec_id, cached=False, function_call=function_call)
