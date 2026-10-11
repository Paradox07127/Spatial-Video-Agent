"""Durable session/history/job APIs for protocol v2."""
from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix='/api/agent')


class CreateSession(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(default='Untitled investigation', min_length=1, max_length=160)


@router.post('/sessions', status_code=201)
def create_session(body: CreateSession, request: Request):
    return request.app.state.investigations.create(body.title)


@router.get('/sessions')
def sessions(request: Request):
    return {'sessions': request.app.state.investigations.sessions()}


@router.get('/sessions/{session_id}')
def session(session_id: str, request: Request):
    return request.app.state.investigations.session(session_id)


@router.get('/jobs/{job_id}')
def job(job_id: str, request: Request):
    return request.app.state.investigations.job(job_id)


@router.get('/jobs/{job_id}/events')
def events(job_id: str, request: Request, after_seq: int = Query(default=0, ge=0)):
    service = request.app.state.investigations
    service.job(job_id)
    return StreamingResponse(service.stream(job_id, after_seq), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@router.post('/jobs/{job_id}/cancel')
def cancel(job_id: str, request: Request):
    return request.app.state.investigations.cancel(job_id)
