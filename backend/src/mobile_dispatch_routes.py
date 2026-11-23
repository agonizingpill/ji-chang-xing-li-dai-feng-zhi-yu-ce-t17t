"""HTTP routes for mobile (flex) crew registration, gap board and dispatch."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import mobile_dispatch_service as service
from .auth import current_user, require
from .db import audit, connect

router = APIRouter(prefix='/mobile-dispatch', tags=['机动班组调度'])


class TeamIn(BaseModel):
    team_code: str = Field(min_length=2, max_length=40)
    terminal: str = Field(min_length=1, max_length=20)
    capacity: int = Field(gt=0, description='单档可保障行李件数')


class RecomputeIn(BaseModel):
    terminal: str = Field(min_length=1, max_length=20)


class AllocationConfirmIn(BaseModel):
    note: str = Field(min_length=3, max_length=500)


@router.get('/teams')
def list_teams(user=Depends(current_user)):
    with connect() as conn:
        service.ensure_schema(conn)
        return [dict(r) for r in conn.execute(
            'SELECT * FROM mobile_teams ORDER BY terminal, team_code').fetchall()]


@router.post('/teams', status_code=201)
def register_team(data: TeamIn, user=Depends(require('operator', 'supervisor'))):
    with connect() as conn:
        service.ensure_schema(conn)
        try:
            row = conn.execute(
                'INSERT INTO mobile_teams(team_code,terminal,capacity) VALUES (%s,%s,%s) RETURNING *',
                (data.team_code, data.terminal, data.capacity)).fetchone()
        except Exception as exc:
            raise HTTPException(409, '机动班组编号已存在') from exc
        audit(conn, user['id'], 'register', 'mobile_team', row['id'],
              {'team_code': row['team_code'], 'terminal': row['terminal'],
               'capacity': row['capacity']})
        conn.commit()
        return dict(row)


@router.post('/teams/{team_id}/deactivate')
def deactivate_team(team_id: int, user=Depends(require('operator', 'supervisor'))):
    with connect() as conn:
        row = conn.execute(
            'UPDATE mobile_teams SET active = FALSE WHERE id = %s AND active = TRUE RETURNING *',
            (team_id,)).fetchone()
        if not row:
            raise HTTPException(404, '机动班组不存在或已停用')
        audit(conn, user['id'], 'deactivate', 'mobile_team', team_id,
              {'team_code': row['team_code']})
        conn.commit()
        return dict(row)


@router.get('/board')
def dispatch_board(terminal: str | None = None, user=Depends(current_user)):
    with connect() as conn:
        return service.board(conn, terminal)


@router.post('/recompute', status_code=201)
def recompute(data: RecomputeIn, user=Depends(require('operator', 'supervisor'))):
    with connect() as conn:
        try:
            result = service.recompute(conn, data.terminal, user, trigger='manual')
        except HTTPException:
            raise
        conn.commit()
        return result


@router.post('/allocations/{allocation_id}/confirm')
def confirm_allocation(allocation_id: int, data: AllocationConfirmIn,
                       user=Depends(require('supervisor'))):
    with connect() as conn:
        try:
            row = service.confirm_allocation(conn, allocation_id, data.note, user)
        except HTTPException:
            raise
        conn.commit()
        return row
