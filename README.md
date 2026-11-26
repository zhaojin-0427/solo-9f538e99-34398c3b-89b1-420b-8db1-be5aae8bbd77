# 双屏电子谱翻页试排（FastAPI）

两块电子谱屏（A / B）共用一个翻页控制器，为“共用段 + 分岔后两条路线（再奏一遍 / 接尾声）”
求解逐拍翻页安排，并支持在 `/docs` 里逐拍试走。

## 业务规则（实现口径）

1. 全场最多 **8 页**；每页有换入耗时 `durations[页号]`，取值 **1～3 拍**。
2. 两屏各有起始页。一拍可要求看见 0～2 个页号；要求两页时必须由两块空闲屏分别显示。
3. **一次只换一屏**：任一屏在换页时控制器忙，不能再启动换页。
4. 换页期间该屏**不可读**；拍首启动的换页，本拍就不可读。
5. 新页从换完后的**下一拍**起可见（拍尾结算剩余拍数，归零即换完）。
6. 换页一旦启动**不可中断**，口令到达也照常换完。
7. 口令在**分岔首拍开始时**给出（`repeat` 再奏一遍 / `coda` 接尾声）；
   分岔首拍之前两条路线必须共用同一份换页指令（共用段只有一份动作序列）。
8. 每条路线（含共用段）至多 **24 拍**。
9. 存在满足两条路线逐拍读谱要求的安排才创建试排；无解返回 `409` 并说明卡点。

求解方法：共用段 BFS 枚举分岔首拍开始时的全部可达状态（带父指针可重建动作）；
对每个分岔状态分别用备忘录 DFS 验证两条后段；两条都可行即得到方案。

## 启动（Docker Compose）

```bash
docker compose up --build
# API:  http://localhost:8000
# 文档: http://localhost:8000/docs   （Swagger 表单，可直接填参调试）
```

## 本地开发（无 Docker 时）

```bash
python3 -m venv --without-pip .venv   # 精简系统可先 bootstrap pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload --port 8000
.venv/bin/python -m pytest tests/     # 12 个测试
```

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/rehearsals` | 填两屏起始页、各页换入耗时、共用段与两条后段逐拍要求，创建试排；无解 `409` |
| GET | `/rehearsals` | 列出试排 |
| GET | `/rehearsals/{id}` | 查看方案：共用段/两条路线的逐拍可见性与动作 |
| POST | `/rehearsals/{id}/sessions` | 开始一次逐拍试走 |
| POST | `/sessions/{sid}/step` | 走一拍；分岔首拍必须在 body 给 `{"command": "repeat"|"coda"}` |
| GET | `/sessions/{sid}` | 查看试走历史与状态 |
| GET | `/health` | 健康检查 |

每拍返回：`visible_pages`（本拍可见谱页）、`turning`（正在换页、不可读的屏）、
两屏各自快照（当前页/目标页/剩余拍数/是否可见）、本拍启动的动作、要求是否满足。

### 试走流程（curl）

```bash
# 1. 创建试排（示例场景：分岔拍 B 屏正在换页）
curl -s localhost:8000/rehearsals -H 'Content-Type: application/json' \
  -d @examples/inflight_fork.json

# 2. 新建试走会话，拿到 session_id 与 fork_beat
curl -s -X POST localhost:8000/rehearsals/<id>/sessions

# 3. 走到分岔前都用空 body
curl -s -X POST localhost:8000/sessions/<sid>/step -H 'Content-Type: application/json' -d '{}'

# 4. 分岔首拍不给口令会收到 409 command_required；给口令后才能继续
curl -s -X POST localhost:8000/sessions/<sid>/step -H 'Content-Type: application/json' \
  -d '{"command": "coda"}'
```

`examples/infeasible.json` 是无解示例（第 2 拍要求同拍看见 1、3 页，但 3 页换入需 3 拍）。

## 目录

```
app/
  engine.py    拍状态机、共用段 BFS、后段 DFS、无解诊断
  planner.py   顶层规划（组合共用段与两条路线）
  models.py    请求/响应模型与输入校验
  store.py     进程内存储（试排、试走会话）
  main.py      FastAPI 路由
tests/         12 个端到端与引擎测试（含“口令来时仍在换页”场景）
examples/      可行 / 无解请求样例
```
