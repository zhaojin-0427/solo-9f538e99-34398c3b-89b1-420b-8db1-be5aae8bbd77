"""翻页调度求解引擎。

时间语义（拍为最小单位，拍号从 1 开始）：
1. 每拍“开始时”可在空闲屏上启动一次换页（控制器空闲 = 没有任何屏在换页）。
2. 换页持续 duration[目标页] 拍：换页期间该屏不可读；本拍启动则本拍就不可读。
3. 拍尾结算：换页剩余拍数 -1，归零后该屏变为空闲并显示新页，新页从“下一拍”起可见。
4. 换页一旦启动不可中断，口令到达也照常换完。
5. 每拍要求的页必须由该拍当前可见（空闲）的屏提供，一屏一页；同拍最多要求两页。

状态编码（元组，可哈希）：
    (a_page, a_to, a_rem, b_page, b_to, b_rem)
- 空闲屏：to == -1，rem == 0，page 为正在显示的页。
- 换页屏：to 为目标页，rem >= 1 为剩余拍数（含本拍），page 为换页前显示的旧页。
"""
from __future__ import annotations

from dataclasses import dataclass

SCREENS = ("A", "B")
IDLE_TO = -1


class Infeasible(Exception):
    """无解，携带结构化诊断信息。"""

    def __init__(self, reason: str, diagnostics: dict | None = None):
        super().__init__(reason)
        self.reason = reason
        self.diagnostics = diagnostics or {}


# --------------------------------------------------------------------------- #
# 状态基础操作
# --------------------------------------------------------------------------- #
def initial_state(start_a: int, start_b: int) -> tuple:
    return (start_a, IDLE_TO, 0, start_b, IDLE_TO, 0)


def _idx(screen: str) -> tuple[int, int, int]:
    base = 0 if screen == "A" else 3
    return base, base + 1, base + 2


def is_turning(state: tuple, screen: str) -> bool:
    _, i_to, _ = _idx(screen)
    return state[i_to] != IDLE_TO


def controller_busy(state: tuple) -> bool:
    return state[2] > 0 or state[5] > 0


def visible_pages(state: tuple) -> list[int]:
    """本拍进行中可见的页（空闲屏所显示的页）。"""
    pages = []
    for screen in SCREENS:
        i_p, i_to, _ = _idx(screen)
        if state[i_to] == IDLE_TO:
            pages.append(state[i_p])
    return pages


def screen_snapshot(state: tuple, screen: str) -> dict:
    i_p, i_to, i_r = _idx(screen)
    turning = state[i_to] != IDLE_TO
    return {
        "screen": screen,
        "page": state[i_p],
        "turning": turning,
        "target": None if not turning else state[i_to],
        "remaining": state[i_r],
        "visible": not turning,
    }


def requirement_satisfied(state: tuple, required: list[int]) -> bool:
    if not required:
        return True
    visible = visible_pages(state)
    if len(required) == 1:
        return required[0] in visible
    # 两页要求：需要两块空闲屏分别显示这两个不同的页
    return len(visible) >= 2 and set(required).issubset(set(visible))


def candidate_actions(state: tuple, total_pages: int) -> list[tuple[str, int] | None]:
    """本拍开始时所有合法动作：None = 不换页；其余为 (屏, 目标页)。

    任一屏正在换页时控制器忙，只能等待。
    """
    actions: list[tuple[str, int] | None] = [None]
    if controller_busy(state):
        return actions
    for screen in SCREENS:
        i_p, i_to, _ = _idx(screen)
        if state[i_to] != IDLE_TO:
            continue
        current = state[i_p]
        for target in range(1, total_pages + 1):
            if target != current:
                actions.append((screen, target))
    return actions


def step(state: tuple, action, durations: dict[int, int]) -> tuple[tuple, dict]:
    """推进一拍。

    action 为 None 或 (screen, target)。返回 (拍尾新状态, 本拍视图信息)。
    """
    s = list(state)
    action_desc = None
    turning_during: list[str] = []

    if action is not None:
        screen, target = action
        i_p, i_to, i_r = _idx(screen)
        if s[i_to] != IDLE_TO:
            raise ValueError(f"屏 {screen} 正在换页，不能再次启动")
        if controller_busy(tuple(s)):
            raise ValueError("控制器忙，一次只能换一屏")
        if s[i_p] == target:
            raise ValueError("目标页与当前页相同，无需换页")
        old = s[i_p]
        s[i_to] = target
        s[i_r] = durations[target]
        action_desc = {
            "screen": screen,
            "from": old,
            "to": target,
            "duration": durations[target],
        }

    # 本拍进行中的可见性（启动后即不可读）
    for screen in SCREENS:
        _, i_to, _ = _idx(screen)
        if s[i_to] != IDLE_TO:
            turning_during.append(screen)
    view = {
        "visible_pages": visible_pages(tuple(s)),
        "turning": turning_during,
        "action": action_desc,
    }

    # 拍尾结算
    for screen in SCREENS:
        i_p, i_to, i_r = _idx(screen)
        if s[i_to] != IDLE_TO:
            s[i_r] -= 1
            if s[i_r] == 0:
                s[i_p] = s[i_to]
                s[i_to] = IDLE_TO
    return tuple(s), view


# --------------------------------------------------------------------------- #
# 共用段：BFS 求分岔首拍开始时的全部可达状态
# --------------------------------------------------------------------------- #
@dataclass
class PrefixResult:
    layers: list[set[tuple]]          # layers[k] = 第 k 拍开始时的可达状态集合
    parent_maps: list[dict]           # parent_maps[k-1]: 第 k 拍尾状态 -> (拍首状态, 动作)
    dead_beat: int | None = None      # 首个无路可走的拍（无解时）
    dead_details: list[str] | None = None


def plan_prefix(
    init: tuple, shared_reqs: list[list[int]], total_pages: int, durations: dict[int, int]
) -> PrefixResult:
    layers: list[set[tuple]] = [{init}]
    parent_maps: list[dict] = []

    for k, req in enumerate(shared_reqs, start=1):
        parents: dict[tuple, tuple[tuple, object]] = {}
        for state in layers[k - 1]:
            for action in candidate_actions(state, total_pages):
                new_state, _ = step(state, action, durations)
                if requirement_satisfied(
                    _state_after_start(state, action, durations), req
                ):
                    if new_state not in parents:
                        parents[new_state] = (state, action)
        if not parents:
            details = _dead_end_details(layers[k - 1], req, total_pages, durations)
            return PrefixResult(layers, parent_maps, dead_beat=k, dead_details=details)
        parent_maps.append(parents)
        layers.append(set(parents.keys()))
    return PrefixResult(layers, parent_maps)


def _state_after_start(state: tuple, action, durations: dict[int, int]) -> tuple:
    """只应用“拍首启动”、不做拍尾结算，用于检查本拍进行中的可见性。"""
    if action is None:
        return state
    screen, target = action
    s = list(state)
    _, i_to, i_r = _idx(screen)
    s[i_to] = target
    s[i_r] = durations[target]
    return tuple(s)


def _dead_end_details(states, req, total_pages, durations) -> list[str]:
    details = []
    for st in states:
        if controller_busy(st):
            busy = [f"{sc} 屏换页还剩 {_rem(st, sc)} 拍" for sc in SCREENS if is_turning(st, sc)]
            details.append(
                f"状态 {st}：控制器忙（{'，'.join(busy)}），本拍不能启动换页；"
                f"当前可见 {visible_pages(st)}，要求看见 {req}，无法满足"
            )
        else:
            outcomes = []
            for action in candidate_actions(st, total_pages):
                label = "等待" if action is None else f"{action[0]} 屏换至第 {action[1]} 页"
                during = _state_after_start(st, action, durations)
                outcomes.append(f"{label}→可见{visible_pages(during)}")
            details.append(
                f"状态 {st}：要求看见 {req}，可选动作均不满足：{'；'.join(outcomes)}"
            )
    return details


def _rem(state: tuple, screen: str) -> int:
    _, _, i_r = _idx(screen)
    return state[i_r]


def reconstruct_prefix_actions(
    init: tuple, fork_start_state: tuple, prefix: PrefixResult
) -> list:
    """从分岔首拍开始状态反推共用段（不含分岔首拍）的动作序列。"""
    m = len(prefix.layers) - 1  # 共用段拍数；layers[m-1] 为分岔首拍开始状态
    actions: list = []
    cur = fork_start_state
    for k in range(m - 1, 0, -1):
        prev, action = prefix.parent_maps[k - 1][cur]
        actions.append(action)
        cur = prev
    assert cur == init
    actions.reverse()
    return actions


# --------------------------------------------------------------------------- #
# 后段：备忘录 DFS（分岔首拍起，含分岔首拍；分岔首拍要求沿用共用段最后一拍）
# --------------------------------------------------------------------------- #
def solve_continuation(
    fork_start_state: tuple,
    fork_req: list[int],
    route_reqs: list[list[int]],
    total_pages: int,
    durations: dict[int, int],
) -> tuple[list | None, dict]:
    """返回 (动作序列 or None, 诊断)。动作序列第 0 项对应分岔首拍。"""
    all_reqs = [fork_req] + route_reqs
    memo: dict[tuple, object] = {}       # (state, idx) -> 动作序列 or False
    deepest = {"idx": -1, "state": None, "reasons": []}

    def dfs(state: tuple, idx: int):
        key = (state, idx)
        if key in memo:
            return memo[key]
        if idx == len(all_reqs):
            memo[key] = []
            return []
        if idx > deepest["idx"]:
            deepest.update(idx=idx, state=state, reasons=[])
        req = all_reqs[idx]
        for action in candidate_actions(state, total_pages):
            during = _state_after_start(state, action, durations)
            if not requirement_satisfied(during, req):
                continue
            new_state, _ = step(state, action, durations)
            tail = dfs(new_state, idx + 1)
            if tail is not False:
                memo[key] = [action] + tail
                return memo[key]
        if idx == deepest["idx"]:
            deepest["reasons"] = _dead_end_details(
                [state], req, total_pages, durations
            )
        memo[key] = False
        return False

    actions = dfs(fork_start_state, 0)
    diag = {
        "beats_planned": len(all_reqs),
        "deepest_reached_beat": deepest["idx"],  # 0 = 连分岔首拍都过不去
        "blocking_state": deepest["state"],
        "blocking_reasons": deepest["reasons"],
    }
    return (actions if actions is not False else None), diag


# --------------------------------------------------------------------------- #
# 回放：把动作序列变成逐拍视图
# --------------------------------------------------------------------------- #
def replay(
    init: tuple,
    actions: list,
    reqs: list[list[int]],
    durations: dict[int, int],
    section: str,
    global_start_beat: int,
    fork_beat: int | None = None,
) -> list[dict]:
    state = init
    views = []
    for j, action in enumerate(actions):
        global_beat = global_start_beat + j
        new_state, view = step(state, action, durations)
        req = reqs[j]
        during = _state_after_start(state, action, durations)
        action_desc = None
        if view["action"]:
            action_desc = {"beat": global_beat, **view["action"]}
        views.append(
            {
                "beat": global_beat,
                "section": section,
                "required_pages": req,
                "visible_pages": view["visible_pages"],
                "screen_a": screen_snapshot(during, "A"),
                "screen_b": screen_snapshot(during, "B"),
                "turning": view["turning"],
                "action": action_desc,
                "requirement_satisfied": requirement_satisfied(during, req),
                "is_fork_beat": global_beat == fork_beat if fork_beat else False,
            }
        )
        state = new_state
    return views, state
