"""内存存储：试排方案与逐拍试走会话（进程内字典，重启清空）。"""
from __future__ import annotations

import uuid

from .engine import initial_state
from .models import RehearsalCreate
from .planner import plan_rehearsal

_rehearsals: dict[str, dict] = {}
_sessions: dict[str, dict] = {}


def create_rehearsal(payload: RehearsalCreate) -> str:
    plan = plan_rehearsal(payload)
    rid = uuid.uuid4().hex[:12]
    _rehearsals[rid] = {
        "id": rid,
        "payload": payload,
        "plan": plan,
    }
    return rid


def get_rehearsal(rid: str) -> dict:
    if rid not in _rehearsals:
        raise KeyError(rid)
    return _rehearsals[rid]


def list_rehearsals() -> list[dict]:
    return [
        {"rehearsal_id": rid, "fork_beat": r["plan"]["fork_beat"]}
        for rid, r in _rehearsals.items()
    ]


def create_session(rid: str) -> str:
    rec = get_rehearsal(rid)
    sid = uuid.uuid4().hex[:12]
    plan = rec["plan"]
    payload = rec["payload"]
    durations = {int(p): d for p, d in payload.durations.items()}
    _sessions[sid] = {
        "id": sid,
        "rehearsal_id": rid,
        "beat": 0,  # 下一拍要走到的全局拍号
        "state": initial_state(payload.start_page_a, payload.start_page_b),
        "command": None,
        "finished": False,
        "durations": durations,
        "plan": plan,
        "payload": payload,
        "history": [],
    }
    return sid


def get_session(sid: str) -> dict:
    if sid not in _sessions:
        raise KeyError(sid)
    return _sessions[sid]
