"""端到端测试：求解器 + HTTP API。

重点场景：
- test_walkthrough_in_flight_turn_at_fork：口令到来时换页仍在进行，
  换页不可中断、照常换完，两条口令路线都能走通。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.engine import (
    candidate_actions,
    controller_busy,
    initial_state,
    requirement_satisfied,
    step,
)
from app.main import app
from app.models import BeatRequirement, RehearsalCreate, RouteInput

client = TestClient(app)


# --------------------------------------------------------------------------- #
# 测试夹具
# --------------------------------------------------------------------------- #
def inflight_payload() -> RehearsalCreate:
    """B 屏在分岔首拍（第 4 拍）仍在换页（3 -> 5，耗时 2 拍）。"""
    return RehearsalCreate(
        total_pages=6,
        start_page_a=1,
        start_page_b=3,
        durations={1: 1, 2: 1, 3: 1, 4: 1, 5: 2, 6: 2},
        shared=[
            BeatRequirement(beat=1, pages=[1]),
            BeatRequirement(beat=2, pages=[1, 3]),
            BeatRequirement(beat=3, pages=[1]),
            BeatRequirement(beat=4, pages=[1]),  # 分岔首拍
        ],
        routes=[
            RouteInput(
                name="repeat",
                requirements=[
                    BeatRequirement(beat=1, pages=[5]),
                    BeatRequirement(beat=2, pages=[5]),
                    BeatRequirement(beat=3, pages=[3, 5]),
                    BeatRequirement(beat=4, pages=[5]),
                ],
            ),
            RouteInput(
                name="coda",
                requirements=[
                    BeatRequirement(beat=1, pages=[1]),
                    BeatRequirement(beat=2, pages=[1]),
                    BeatRequirement(beat=3, pages=[6]),
                ],
            ),
        ],
    )


def infeasible_payload() -> RehearsalCreate:
    """第 2 拍要求同拍看见 1、3 两页，但 3 页换入要 3 拍，不可能按时出现。"""
    return RehearsalCreate(
        total_pages=3,
        start_page_a=1,
        start_page_b=2,
        durations={1: 1, 2: 1, 3: 3},
        shared=[
            BeatRequirement(beat=1, pages=[1]),
            BeatRequirement(beat=2, pages=[1, 3]),
        ],
        routes=[
            RouteInput(name="repeat", requirements=[BeatRequirement(beat=1, pages=[1])]),
            RouteInput(name="coda", requirements=[BeatRequirement(beat=1, pages=[1])]),
        ],
    )


def create(payload: RehearsalCreate) -> dict:
    resp = client.post("/rehearsals", json=payload.model_dump())
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# 引擎不变量
# --------------------------------------------------------------------------- #
def test_engine_turning_screen_unreadable_and_next_beat_visible():
    durations = {1: 2, 2: 2, 3: 2}
    st = initial_state(1, 3)
    # A 屏 1 -> 2，耗时 2 拍
    new_st, view = step(st, ("A", 2), durations)
    assert view["visible_pages"] == [3]          # A 本拍不可读
    assert view["turning"] == ["A"]
    assert controller_busy(new_st)
    assert candidate_actions(new_st, 3) == [None]  # 控制器忙，只能等待

    mid, _ = step(new_st, None, durations)       # 换页第 2 拍
    assert not controller_busy(mid)
    assert mid[0] == 2 and mid[1] == -1          # 换完，A 显示 2
    # 下一拍起新页可见
    assert requirement_satisfied(mid, [2, 3])
    assert not requirement_satisfied(mid, [1])


def test_one_turn_at_a_time_enforced():
    durations = {p: 2 for p in range(1, 5)}
    st = initial_state(1, 2)
    st2, _ = step(st, ("A", 3), durations)   # 换页耗时 2 拍，仍在进行
    assert controller_busy(st2)
    with pytest.raises(ValueError):
        step(st2, ("B", 4), durations)       # A 还在换，B 不能动
    # 候选动作只剩“等待”
    assert candidate_actions(st2, 4) == [None]


# --------------------------------------------------------------------------- #
# 求解：可行场景
# --------------------------------------------------------------------------- #
def test_create_inflight_rehearsal():
    data = create(inflight_payload())
    assert data["feasible"] is True
    assert data["fork_beat"] == 4
    for rp in data["routes"]:
        assert rp["feasible"] is True
        assert all(beat["requirement_satisfied"] for beat in rp["timeline"])
    # 共用段每拍都满足要求
    assert all(b["requirement_satisfied"] for b in data["shared_timeline"])


def test_prefork_actions_identical_between_routes():
    """口令之前两条路线必须共用换页指令：共用段动作序列只有一份。"""
    data = create(inflight_payload())
    rid = data["rehearsal_id"]
    s1 = client.post(f"/rehearsals/{rid}/sessions").json()
    s2 = client.post(f"/rehearsals/{rid}/sessions").json()
    pre1, pre2 = [], []
    for sid, bucket in ((s1["session_id"], pre1), (s2["session_id"], pre2)):
        # 走到分岔前一拍（第 3 拍），不给口令
        for _ in range(3):
            r = client.post(f"/sessions/{sid}/step", json={})
            assert r.status_code == 200
            bucket.append(r.json()["action"])
    assert pre1 == pre2


# --------------------------------------------------------------------------- #
# 求解：无解场景
# --------------------------------------------------------------------------- #
def test_infeasible_returns_409_with_reason():
    resp = client.post("/rehearsals", json=infeasible_payload().model_dump())
    assert resp.status_code == 409
    body = resp.json()
    assert body["feasible"] is False
    assert "第 2 拍" in body["reason"]
    assert body["diagnostics"]["kind"] == "prefix_dead_end"
    assert body["diagnostics"]["details"]


def test_branch_infeasible_returns_409():
    """共用段可行，但其中一条后段无论如何都满足不了 -> 409 且指明卡在哪条路线。"""
    payload = RehearsalCreate(
        total_pages=4,
        start_page_a=1,
        start_page_b=2,
        durations={1: 1, 2: 1, 3: 3, 4: 1},
        shared=[
            BeatRequirement(beat=1, pages=[1]),
            BeatRequirement(beat=2, pages=[1]),  # 分岔首拍
        ],
        routes=[
            # repeat：分岔后第 1 拍就要第 3 页，但换入要 3 拍，分岔拍才开始换也来不及
            RouteInput(name="repeat", requirements=[BeatRequirement(beat=1, pages=[3])]),
            RouteInput(name="coda", requirements=[BeatRequirement(beat=1, pages=[1])]),
        ],
    )
    r = client.post("/rehearsals", json=payload.model_dump())
    assert r.status_code == 409
    body = r.json()
    assert body["diagnostics"]["kind"] == "no_common_fork_state"
    assert "repeat" in str(body["diagnostics"]["route_blocking"])


def test_empty_requirement_beat_allows_turning():
    """无读谱要求的拍子允许两屏中的任意一屏换页。"""
    payload = RehearsalCreate(
        total_pages=3,
        start_page_a=1,
        start_page_b=1,
        durations={1: 1, 2: 1, 3: 1},
        shared=[
            BeatRequirement(beat=1, pages=[]),          # 空闲拍：可以换页
            BeatRequirement(beat=2, pages=[2]),        # 分岔首拍
        ],
        routes=[
            RouteInput(name="repeat", requirements=[BeatRequirement(beat=1, pages=[2])]),
            RouteInput(name="coda", requirements=[BeatRequirement(beat=1, pages=[2])]),
        ],
    )
    data = create(payload)
    assert data["feasible"] is True


def test_input_validation_422():
    payload = inflight_payload().model_dump()
    payload["durations"]["5"] = 9  # 超出 1..3
    r = client.post("/rehearsals", json=payload)
    assert r.status_code == 422

    payload = inflight_payload().model_dump()
    payload["total_pages"] = 9     # 超过 8 页
    r = client.post("/rehearsals", json=payload)
    assert r.status_code == 422

    payload = inflight_payload().model_dump()
    del payload["durations"][6]  # 缺页的耗时
    r = client.post("/rehearsals", json=payload)
    assert r.status_code == 422


def test_beats_limit_422():
    payload = inflight_payload().model_dump()
    payload["routes"][0]["requirements"] = [
        {"beat": i, "pages": [1]} for i in range(1, 22)
    ]  # 共用 4 + 21 = 25 > 24
    r = client.post("/rehearsals", json=payload)
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# 逐拍试走：分岔拍必须给口令
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("command", ["repeat", "coda"])
def test_walkthrough_in_flight_turn_at_fork(command):
    data = create(inflight_payload())
    rid = data["rehearsal_id"]
    sid = client.post(f"/rehearsals/{rid}/sessions").json()["session_id"]

    # 第 1-3 拍：无需口令
    for beat in (1, 2, 3):
        r = client.post(f"/sessions/{sid}/step", json={})
        assert r.status_code == 200
        view = r.json()
        assert view["beat"] == beat
    beat3 = client.get(f"/sessions/{sid}").json()["history"][-1]
    # 第 3 拍 B 屏启动 3 -> 5 的两拍换页
    assert beat3["action"]["screen"] == "B"
    assert beat3["action"]["to"] == 5
    assert beat3["turning"] == ["B"]

    # 第 4 拍（分岔首拍）不给口令 -> 409
    r = client.post(f"/sessions/{sid}/step", json={})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "command_required"

    # 非分岔拍给口令 -> 409（新会话验证）
    sid2 = client.post(f"/rehearsals/{rid}/sessions").json()["session_id"]
    r = client.post(f"/sessions/{sid2}/step", json={"command": "repeat"})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "unexpected_command"

    # 给口令走分岔首拍：B 屏仍在换页（不可中断！），A 屏提供第 1 页
    r = client.post(f"/sessions/{sid}/step", json={"command": command})
    assert r.status_code == 200
    fork = r.json()
    assert fork["is_fork_beat"] is True
    assert fork["turning"] == ["B"]
    assert fork["screen_b"]["turning"] is True
    assert fork["screen_b"]["target"] == 5
    assert fork["screen_b"]["remaining"] == 1   # 还剩本拍
    assert fork["visible_pages"] == [1]
    assert fork["required_pages"] == [1]
    assert fork["requirement_satisfied"] is True
    assert "照常换完" in fork["note"]

    # 第 5 拍：上一个换页已在拍尾完成（新页从本拍起可用）
    r = client.post(f"/sessions/{sid}/step", json={})
    beat5 = r.json()
    assert beat5["screen_b"]["page"] == 5  # 若本拍又启动换页，page 仍记录换出前的页
    if command == "coda":
        # coda 方案在本拍紧接着启动 B 屏 5 -> 6 的换页，本拍 B 不可读
        assert beat5["turning"] == ["B"]
        assert beat5["screen_b"]["visible"] is False
        assert beat5["action"] == {
            "beat": 5, "screen": "B", "from": 5, "to": 6, "duration": 2
        }
        assert beat5["visible_pages"] == [1]
    else:
        assert beat5["turning"] == []
        assert beat5["screen_b"]["visible"] is True
        assert 5 in beat5["visible_pages"]

    # 走到该路线结束
    route_len = 4 if command == "repeat" else 3
    remaining = route_len - 1  # 已走分岔后的第 1 拍
    last = beat5
    for _ in range(remaining):
        r = client.post(f"/sessions/{sid}/step", json={})
        assert r.status_code == 200
        last = r.json()
    assert last["finished"] is True
    assert last["section"] == command

    # 全部历史拍都满足读谱要求
    history = client.get(f"/sessions/{sid}").json()["history"]
    assert len(history) == 4 + route_len
    assert all(h["requirement_satisfied"] for h in history)
    # 每拍至多一屏在换
    assert all(len(h["turning"]) <= 1 for h in history)

    # 结束后再走 -> 409
    r = client.post(f"/sessions/{sid}/step", json={})
    assert r.status_code == 409


def test_two_commands_diverge_after_fork():
    """两条口令在分岔之后走出不同的页面安排。"""
    data = create(inflight_payload())
    rid = data["rehearsal_id"]

    def walk(command):
        sid = client.post(f"/rehearsals/{rid}/sessions").json()["session_id"]
        out = []
        for _ in range(3):
            out.append(client.post(f"/sessions/{sid}/step", json={}).json())
        out.append(
            client.post(f"/sessions/{sid}/step", json={"command": command}).json()
        )
        for _ in range(4 if command == "repeat" else 3):
            out.append(client.post(f"/sessions/{sid}/step", json={}).json())
        return out

    rep = walk("repeat")
    coda = walk("coda")
    # 分岔前（1-3 拍）完全一致
    assert [h["action"] for h in rep[:3]] == [h["action"] for h in coda[:3]]
    # 分岔后可见页序列不同（repeat 第 5 拍起出现 5；coda 先保持 1 再换到 6）
    assert rep[4]["visible_pages"] == [1, 5]
    assert coda[4]["visible_pages"] == [1]
    assert coda[6]["visible_pages"] == [1, 6]
