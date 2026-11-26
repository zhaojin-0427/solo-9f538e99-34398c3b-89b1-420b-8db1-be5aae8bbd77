"""FastAPI 入口：创建试排、逐拍试走。

启动后访问 /docs 可直接在 Swagger 表单里填参数调试。
"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .engine import (
    Infeasible,
    _state_after_start,
    requirement_satisfied,
    screen_snapshot,
    step,
)
from .models import (
    BeatView,
    Command,
    RehearsalCreate,
    RehearsalResult,
    RoutePlan,
    StepResult,
)
from . import store

app = FastAPI(
    title="双屏电子谱翻页试排",
    description=(
        "两块电子谱屏共用一个翻页控制器。在分岔首拍给出 repeat（再奏一遍）"
        "或 coda（接尾声）口令；口令之前两条路线必须共用换页指令。"
        "一次只换一屏，换页期间该屏不可读，新页从换完后的下一拍起可见，"
        "换页不可中断。全场最多 8 页，每条路线至多 24 拍。"
    ),
    version="1.0.0",
)


class StepRequest(BaseModel):
    command: Optional[Command] = Field(
        None, description="仅当走到分岔首拍时必填：repeat=再奏一遍 / coda=接尾声"
    )


class SessionCreated(BaseModel):
    rehearsal_id: str
    session_id: str
    fork_beat: int
    awaiting_command_at_beat: int


@app.post("/rehearsals", response_model=RehearsalResult, summary="创建翻页试排（无解返回 409 并说明原因）")
def create_rehearsal(payload: RehearsalCreate):
    try:
        rid = store.create_rehearsal(payload)
    except Infeasible as exc:
        return JSONResponse(
            status_code=409,
            content={
                "feasible": False,
                "reason": exc.reason,
                "diagnostics": exc.diagnostics,
            },
        )
    rec = store.get_rehearsal(rid)
    plan = rec["plan"]
    return RehearsalResult(
        rehearsal_id=rid,
        feasible=True,
        fork_beat=plan["fork_beat"],
        shared_timeline=[BeatView(**v) for v in plan["shared_views"]],
        routes=[
            RoutePlan(
                name=rp["name"],
                actions=rp["actions"],
                timeline=[BeatView(**v) for v in rp["timeline"]],
                feasible=True,
            )
            for rp in plan["routes"]
        ],
    )


@app.get("/rehearsals", summary="列出全部试排")
def list_rehearsals():
    return store.list_rehearsals()


@app.get("/rehearsals/{rid}", response_model=RehearsalResult, summary="查看试排方案与逐拍安排")
def get_rehearsal(rid: str):
    try:
        rec = store.get_rehearsal(rid)
    except KeyError:
        raise HTTPException(status_code=404, detail="试排不存在")
    plan = rec["plan"]
    return RehearsalResult(
        rehearsal_id=rid,
        feasible=True,
        fork_beat=plan["fork_beat"],
        shared_timeline=[BeatView(**v) for v in plan["shared_views"]],
        routes=[
            RoutePlan(
                name=rp["name"],
                actions=rp["actions"],
                timeline=[BeatView(**v) for v in rp["timeline"]],
                feasible=True,
            )
            for rp in plan["routes"]
        ],
    )


@app.post(
    "/rehearsals/{rid}/sessions",
    response_model=SessionCreated,
    summary="开始一次逐拍试走",
)
def create_session(rid: str):
    try:
        sid = store.create_session(rid)
    except KeyError:
        raise HTTPException(status_code=404, detail="试排不存在")
    plan = store.get_session(sid)["plan"]
    return SessionCreated(
        rehearsal_id=rid,
        session_id=sid,
        fork_beat=plan["fork_beat"],
        awaiting_command_at_beat=plan["fork_beat"],
    )


@app.post(
    "/sessions/{sid}/step",
    response_model=StepResult,
    summary="试走一拍；到分岔首拍必须在请求体里给口令",
)
def step_session(sid: str, req: StepRequest):
    try:
        sess = store.get_session(sid)
    except KeyError:
        raise HTTPException(status_code=404, detail="试走会话不存在")
    if sess["finished"]:
        raise HTTPException(status_code=409, detail="本场试走已结束，请新建会话")

    plan = sess["plan"]
    payload = sess["payload"]
    fork_beat = plan["fork_beat"]
    next_beat = sess["beat"] + 1

    # 分岔首拍：必须给口令
    if next_beat == fork_beat:
        if req.command is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "command_required",
                    "message": f"第 {fork_beat} 拍是分岔首拍，请给出 repeat（再奏一遍）或 coda（接尾声）口令",
                    "fork_beat": fork_beat,
                },
            )
        sess["command"] = req.command
    elif req.command is not None:
        raise HTTPException(
            status_code=409,
            detail={"error": "unexpected_command", "message": "只有分岔首拍才能给口令"},
        )

    # 确定本拍动作与读谱要求
    action, section, required, is_fork = _resolve_beat(sess, next_beat)

    state = sess["state"]
    during = _state_after_start(state, action, sess["durations"])
    if not requirement_satisfied(during, required):
        # 正常不会发生：方案是离线求出来的；到此说明实现有 bug
        raise HTTPException(status_code=500, detail="内部错误：方案动作无法满足本拍要求")
    new_state, view = step(state, action, sess["durations"])

    note = None
    if is_fork:
        note = (
            f"分岔首拍，口令为 {sess['command']}；"
            + (
                "该屏换页跨越分岔点，按规则照常换完、不可中断"
                if view["turning"]
                else "无正在进行的换页"
            )
        )
    elif view["action"]:
        note = f"{view['action']['screen']} 屏开始从第 {view['action']['from']} 页换至第 {view['action']['to']} 页"

    result = StepResult(
        rehearsal_id=sess["rehearsal_id"],
        session_id=sid,
        beat=next_beat,
        section=section,
        required_pages=required,
        visible_pages=view["visible_pages"],
        turning=view["turning"],
        screen_a=screen_snapshot(during, "A"),
        screen_b=screen_snapshot(during, "B"),
        action=(
            {"beat": next_beat, **view["action"]} if view["action"] else None
        ),
        requirement_satisfied=True,
        is_fork_beat=is_fork,
        awaiting_command=False,
        finished=False,
        note=note,
    )

    sess["state"] = new_state
    sess["beat"] = next_beat
    sess["history"].append(result.model_dump())

    # 路线末拍走完即结束（仅分岔之后才可能结束）
    if sess["command"] is not None:
        route_payload = _find_route(payload, sess["command"])
        if next_beat >= fork_beat + len(route_payload.requirements):
            sess["finished"] = True
            result.finished = True
            result.note = (result.note + "；" if note else "") + "本场试走结束"
    return result


def _find_route(payload: RehearsalCreate, name: Command):
    for route in payload.routes:
        if route.name == name:
            return route
    raise HTTPException(status_code=500, detail=f"内部错误：缺少路线 {name}")


def _resolve_beat(sess: dict, beat: int):
    """返回 (动作, 段名, 本拍要求页, 是否分岔首拍)。"""
    plan = sess["plan"]
    payload = sess["payload"]
    fork_beat = plan["fork_beat"]

    if beat < fork_beat:
        action = plan["shared_actions"][beat - 1]
        required = payload.shared[beat - 1].pages
        return action, "shared", required, False

    if beat == fork_beat:
        cmd = sess["command"]
        route = {r["name"]: r for r in plan["routes"]}[cmd]
        return route["_full_actions"][0], cmd, payload.shared[-1].pages, True

    # 分岔之后
    cmd = sess["command"]
    route = {r["name"]: r for r in plan["routes"]}[cmd]
    route_payload = _find_route(payload, cmd)
    idx = beat - fork_beat - 1  # 路线内第 idx 拍（0 基）
    action = route["_full_actions"][idx + 1]
    required = route_payload.requirements[idx].pages
    return action, cmd, required, False


@app.get("/sessions/{sid}", summary="查看试走会话状态与历史")
def get_session(sid: str):
    try:
        sess = store.get_session(sid)
    except KeyError:
        raise HTTPException(status_code=404, detail="试走会话不存在")
    return {
        "session_id": sid,
        "rehearsal_id": sess["rehearsal_id"],
        "next_beat": sess["beat"] + 1,
        "fork_beat": sess["plan"]["fork_beat"],
        "command": sess["command"],
        "finished": sess["finished"],
        "history": sess["history"],
    }


@app.get("/health", summary="健康检查")
def health():
    return {"status": "ok"}
