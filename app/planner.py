"""顶层试排规划：组合共用段 BFS 与两条后段 DFS。"""
from __future__ import annotations

from .engine import (
    Infeasible,
    initial_state,
    plan_prefix,
    reconstruct_prefix_actions,
    replay,
    solve_continuation,
)
from .models import RehearsalCreate


def _req_lists(reqs) -> list[list[int]]:
    return [r.pages for r in reqs]


def plan_rehearsal(payload: RehearsalCreate) -> dict:
    durations = {int(p): d for p, d in payload.durations.items()}
    init = initial_state(payload.start_page_a, payload.start_page_b)
    shared_reqs = _req_lists(payload.shared)
    m = len(shared_reqs)
    fork_req = shared_reqs[-1]  # 分岔首拍的读谱要求（两条路线同一拍、同一要求）

    # 1) 共用段
    prefix = plan_prefix(init, shared_reqs, payload.total_pages, durations)
    if prefix.dead_beat is not None:
        raise Infeasible(
            f"共用段第 {prefix.dead_beat} 拍不存在任何满足读谱要求的换页安排",
            {
                "kind": "prefix_dead_end",
                "beat": prefix.dead_beat,
                "required_pages": shared_reqs[prefix.dead_beat - 1],
                "details": prefix.dead_details,
            },
        )

    fork_states = prefix.layers[m - 1]
    routes_in = {r.name: _req_lists(r.requirements) for r in payload.routes}

    # 2) 对每个分岔状态分别验证两条路线
    route_diags: dict[str, dict] = {}
    for name in ("repeat", "coda"):
        route_diags[name] = {}

    chosen = None
    for st in sorted(fork_states):
        per_route = {}
        ok = True
        for name in ("repeat", "coda"):
            actions, diag = solve_continuation(
                st, fork_req, routes_in[name], payload.total_pages, durations
            )
            per_route[name] = (actions, diag)
            if actions is None:
                ok = False
                route_diags[name][repr(st)] = diag
        if ok and chosen is None:
            chosen = (st, per_route)

    if chosen is None:
        raise Infeasible(
            "共用段可行，但分岔首拍的每个可达状态都无法同时满足两条后段路线",
            {
                "kind": "no_common_fork_state",
                "fork_beat": m,
                "fork_states": [list(st) for st in sorted(fork_states)],
                "route_blocking": route_diags,
            },
        )

    fork_state, per_route = chosen

    # 3) 回放共用段（动作只到分岔首拍前一拍；分岔首拍动作随路线）
    shared_actions = reconstruct_prefix_actions(init, fork_state, prefix)
    shared_views, end_state = replay(
        init,
        shared_actions,
        shared_reqs[:-1],
        durations,
        section="shared",
        global_start_beat=1,
    )
    assert end_state == fork_state

    route_plans = []
    for name in ("repeat", "coda"):
        full_actions, _ = per_route[name]          # 第 0 项 = 分岔首拍动作
        route_reqs_all = [fork_req] + routes_in[name]
        views, _ = replay(
            fork_state,
            full_actions,
            route_reqs_all,
            durations,
            section=name,
            global_start_beat=m,
            fork_beat=m,
        )
        # 分岔首拍计入共用段；路线自身时间线去掉第 0 拍
        route_views = views[1:]
        action_book = {
            v["beat"]: v["action"] for v in route_views if v["action"]
        }
        route_plans.append(
            {
                "name": name,
                "actions": action_book,
                "timeline": route_views,
                "feasible": True,
                "_fork_action": full_actions[0],
                "_full_actions": full_actions,
                "_fork_view": views[0],
            }
        )

    return {
        "fork_beat": m,
        "init": init,
        "fork_state": fork_state,
        "shared_actions": shared_actions,
        "shared_views": shared_views,
        "routes": route_plans,
    }
