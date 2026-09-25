# 血液调剂台

纯Python标准库实现的应急血液调剂协调服务，使用SQLite持久化，HTTP接口由`http.server`提供。

伤员送医后，医院在台面上登记血袋批次（血型、成分、效期、可调剂数量），伤员侧登记所需血型与用量；系统按相容血型、效期、距离匹配占用，两家同时抢同一批时后提交的一方留待确认，医院实发后才真正扣库存，取消或超时自动释放，过期血袋不进候选。重启后仍可沿伤员查到每一笔调剂去向。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：血型相容表、候选排序（效期+距离）、占用计划与状态转换，纯计算不接触存储。
- `src/repository.py`：SQLite建表、`Tx`事务对象（`BEGIN IMMEDIATE`串行化并发）和查询。
- `src/service.py`：用例编排——登记、匹配、实发扣库存、取消/超时释放、过期清扫、伤员追溯。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：调剂台演示页面。
- `tests/`：规则计算、完整流程（含重启追溯）和失败场景测试。

## 领域模型

- `blood_lot`（血袋批次）：`registered → expired / closed`。记录`quantity_total / available / reserved / issued`。
- `blood_request`（用血需求）：`requested → matched → fulfilled`，可`cancelled`。
- `allocation`（调剂单）：`reserved`（已占用）/ `pending`（留待确认）→ `issued`（实发）/ `released`（取消或超时释放）。

关键规则：

- 匹配候选：同成分、血型相容（红细胞/全血、血浆、血小板/冷沉淀分别规则）、未过期；按效期升序、距离升序排序。
- 占用只减`quantity_available`防止重复许诺；`issue`（实发）才把数量从`reserved`转入`issued`真正扣库存。
- 库存不足时，后到需求在最优候选批次上生成`pending`；原`reserved`不动，待库存释放后可`confirm`转正。
- 占用有`hold_until`（默认30分钟，`--hold-minutes`可调），任何读写操作触发惰性清扫：超时释放回补库存，过期批次退出候选并释放其占用。

## 启动

```bash
python3 app.py --db ./data.db --port 8323
```

默认端口为`8323`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：调剂台演示页面。
- `POST /api/lots`：登记血袋，请求体`{"reference":"LOT-1","data":{"hospital":"...","blood_type":"A+","component":"rbc","expires_at":"...","quantity":4,"lat":..,"lng":..}}`。
- `GET /api/lots`、`GET /api/lots/{id}`：库存列表与批次详情（含其调剂单）。
- `POST /api/requests`：登记伤员用血需求并立即匹配，请求体`{"reference":"REQ-1","data":{"casualty_ref":"CAS-1","blood_type":"A+","component":"rbc","units":2,"lat":..,"lng":..}}`。
- `GET /api/requests`、`GET /api/requests/{id}`：需求列表与详情（含调剂单）。
- `POST /api/requests/{id}/actions/{match,cancel}`：重新匹配未满足部分 / 取消需求并释放全部占用。
- `GET /api/allocations`：调剂单列表，可带`state`、`casualty`、`request_id`参数。
- `POST /api/allocations/{id}/actions/{issue,confirm,cancel,expire}`：实发扣库存 / 留待确认转正 / 取消释放 / 超时释放。
- `GET /api/casualties/{ref}/trail`：沿伤员查调剂去向（需求、调剂单、供血批次与各自时间线）。
- `GET /api/records/{id}/audit`：任一记录的审计时间线。
- `GET /api/stats`：按类别与状态的统计。

动作接口请求体为`{"expected_version":1,"data":{...}}`（乐观并发）。除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。角色：`blood_bank_clerk`（血库）、`hospital_liaison`（医院）、`incident_commander`（指挥）、`admin`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖血型相容与候选排序、完整流程与重启追溯、同批争抢留待确认、超时释放、过期禁发、权限拒绝、重复引用和版本冲突。
