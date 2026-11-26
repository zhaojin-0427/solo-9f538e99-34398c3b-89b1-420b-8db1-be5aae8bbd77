"""请求 / 响应 Pydantic 模型。

约定（与求解器一致）：
- 屏只有 A、B 两块；页编号为 1..total_pages，全场最多 8 页。
- 每页有“换入耗时” duration[page]，取值 1..3 拍，对所有换页一视同仁。
- 一条“逐拍要求”可要求同拍看见两页：pages 给 1 或 2 个页号，
  空列表表示该拍没有必须看见的页（两屏都可在换页）。
- 口令在分岔首拍（共用段的最后一拍）开始时给出，只有 repeat / coda 两种。
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

ScreenId = Literal["A", "B"]
Command = Literal["repeat", "coda"]

MAX_PAGES = 8
MAX_BEATS = 24
MIN_DURATION = 1
MAX_DURATION = 3


class BeatRequirement(BaseModel):
    """某一拍必须能看见的谱页（1 或 2 页；空列表 = 无要求）。"""

    beat: int = Field(..., ge=1, description="拍号，从 1 开始")
    pages: list[int] = Field(
        ...,
        max_length=2,
        description="该拍必须可见的页号；至多 2 页（一屏一页），可为空列表",
    )


class RouteInput(BaseModel):
    """一条后段路线（“再奏一遍”或“接尾声”）的逐拍要求。"""

    name: Command = Field(..., description="口令：repeat=再奏一遍，coda=接尾声")
    requirements: list[BeatRequirement] = Field(
        ...,
        min_length=1,
        description="分岔首拍之后该路线各拍的读谱要求（拍号相对路线起点，从 1 开始）",
    )


class RehearsalCreate(BaseModel):
    """创建翻页试排的完整输入。"""

    total_pages: int = Field(..., ge=1, le=MAX_PAGES, description="全场谱页数，最多 8 页")
    start_page_a: int = Field(..., ge=1, description="A 屏起始页")
    start_page_b: int = Field(..., ge=1, description="B 屏起始页")
    durations: dict[int, int] = Field(
        ...,
        description="每页的换入耗时（拍），键为页号，值 1..3；每页都必须给出",
    )
    shared: list[BeatRequirement] = Field(
        ..., min_length=1, description="共用段逐拍要求；最后一拍即分岔首拍"
    )
    routes: list[RouteInput] = Field(
        ...,
        min_length=2,
        max_length=2,
        description="两条后段路线，必须各一条，口令分别为 repeat 与 coda",
    )

    @model_validator(mode="after")
    def _validate(self) -> "RehearsalCreate":
        n = self.total_pages

        if not (1 <= self.start_page_a <= n and 1 <= self.start_page_b <= n):
            raise ValueError("起始页号必须在 1..total_pages 范围内")

        if set(self.durations.keys()) != set(range(1, n + 1)):
            raise ValueError("durations 必须为每一页（1..total_pages）各给一个换入耗时")
        bad = [p for p, d in self.durations.items() if not (MIN_DURATION <= d <= MAX_DURATION)]
        if bad:
            raise ValueError("换入耗时必须在 1..3 拍之间")

        def check_reqs(reqs: list[BeatRequirement], label: str) -> None:
            beats = [r.beat for r in reqs]
            if beats != list(range(1, len(reqs) + 1)):
                raise ValueError(f"{label} 的拍号必须从 1 开始连续无重复")
            for r in reqs:
                if len(r.pages) != len(set(r.pages)):
                    raise ValueError(f"{label} 第 {r.beat} 拍的 pages 有重复页号")
                for p in r.pages:
                    if not (1 <= p <= n):
                        raise ValueError(f"{label} 第 {r.beat} 拍引用了不存在的页 {p}")

        check_reqs(self.shared, "共用段")
        if len(self.routes) != 2 or {r.name for r in self.routes} != {"repeat", "coda"}:
            raise ValueError("routes 必须恰好包含 repeat 与 coda 两条路线")
        for route in self.routes:
            check_reqs(route.requirements, f"路线 {route.name}")
            if len(self.shared) + len(route.requirements) > MAX_BEATS:
                raise ValueError(
                    f"路线 {route.name} 总拍数（含共用段）超过 {MAX_BEATS} 拍上限"
                )
        return self


class BeatView(BaseModel):
    """一拍的完整快照（试排方案与逐拍试走共用）。"""

    beat: int
    section: Literal["shared", "repeat", "coda", "finished"] = Field(
        ..., description="该拍所属段落"
    )
    required_pages: list[int]
    visible_pages: list[int] = Field(..., description="本拍实际可见的页号（不含正在换的屏）")
    screen_a: dict = Field(..., description="A 屏状态：page/turning/remaining/visible")
    screen_b: dict = Field(..., description="B 屏状态：page/turning/remaining/visible")
    turning: list[ScreenId] = Field(..., description="本拍正在换页（不可读）的屏")
    action: Optional[dict] = Field(
        None, description="本拍开始时启动的换页动作：screen/from/to/duration"
    )
    requirement_satisfied: bool
    is_fork_beat: bool = Field(False, description="是否为分岔首拍（口令在该拍开始时给出）")
    note: Optional[str] = None


class RoutePlan(BaseModel):
    """一条路线的逐拍安排（分岔首拍计入共用段，这里只含分岔之后的拍）。"""

    name: Command
    actions: dict[int, dict] = Field(
        ..., description="按拍记录的启动动作，键为全局拍号，值为换页动作详情"
    )
    timeline: list[BeatView] = Field(..., description="逐拍可见性快照")
    feasible: bool


class RehearsalResult(BaseModel):
    rehearsal_id: str
    feasible: bool
    fork_beat: int = Field(..., description="分岔首拍（= 共用段最后一拍）的全局拍号")
    shared_timeline: Optional[list[BeatView]] = None
    routes: Optional[list[RoutePlan]] = None
    reason: Optional[str] = Field(None, description="无解时的人类可读原因")
    diagnostics: Optional[dict] = None


class StepResult(BaseModel):
    """逐拍试走单拍返回。"""

    rehearsal_id: str
    session_id: str
    beat: int
    section: str
    required_pages: list[int]
    visible_pages: list[int]
    turning: list[ScreenId]
    screen_a: dict
    screen_b: dict
    action: Optional[dict]
    requirement_satisfied: bool
    is_fork_beat: bool
    awaiting_command: bool = Field(
        ..., description="下一拍是否为分岔首拍、需要提交口令才能继续"
    )
    finished: bool
    note: Optional[str] = None
