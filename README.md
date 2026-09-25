# 血液调剂台（群体伤亡应急血液协调）

纯 Python 标准库实现，SQLite 持久化，HTTP 由 `http.server` 提供。解决血库电话核库存时
**同批血袋被重复许诺**的问题：提交即按规则锁库，后提交的撞批请求留待确认，原占用不动。

## 分层（规则、存储、服务、页面各自独立）

- `src/domain.py`：领域错误、Actor 与输入校验原语。
- `src/rules.py`：**规则层**。ABO/Rh 相容表（红细胞、血浆、血小板）、效期(FEFO)+距离候选排序、
  贪心选袋、请求状态机。纯函数，不碰数据库。
- `src/repository.py`：**存储层**。SQLite 建表、`BEGIN IMMEDIATE` 写事务、锁库汇总、扣库、台账与溯源查询。
- `src/service.py`：**服务层**。用例编排：登记、匹配锁库、实发扣库、取消、超时/过期清扫、排队晋升（FIFO）。
- `src/audit.py`：调剂台账（事件流）薄封装。
- `src/http_api.py`：HTTP 路由与统一错误输出。
- `static/index.html`：**页面层**。登记、匹配总览、实发/取消、超时清扫、伤员溯源；5 秒自动刷新。
- `tests/`：规则计算、端到端流程、失败场景共 22 个用例。

## 业务规则

- **相容血型**
  - 红细胞按供者→受者：O− 万能供血，AB+ 万能受血（Rh 阴性不给 Rh 阳性外的逆向禁忌）。
  - 血浆规则相反：AB 万能供血，O 只能受 O/AB。
  - 血小板按红细胞 ABO/Rh 相容近似处理。
- **匹配排序**：先硬过滤（同成分 + 相容 + 未过期 + 有余量），再按 **效期近者先用(FEFO)**，效期相同按 **距离近者**，最后批次号兜底。
- **占用语义**
  - 库存足够时请求**整体**锁库为 `held`（默认 5 分钟，`--hold-ttl` 或 `BLOOD_DESK_HOLD_TTL` 可调），等待医院实发。
  - 库存不足或与在途占用撞批 → `pending` 排队待确认，**不占用任何血袋**；按提交先后 FIFO 晋升，不插队。
  - 两家同时抢同一批：事务串行化，先提交者拿走，后提交者留待确认，原占用不动。
- **实发才扣库**：`ship` 时才 `quantity -= 实发数`；支持部分实发（`items:[{allocation_id,quantity}]`），余量立即释放回池并触发排队晋升。
- **取消/超时释放**：取消立即释放占用；超时锁库或锁定期间批次过期，由 `sweep`（提交、登记、实发时也会顺带清扫）自动释放，请求回到排队重新确认。
- **过期血袋**：登记即拒收；候选过滤；已锁定的过期批次不能实发，强制释放重排。
- **重启可溯源**：请求、锁库明细、扣库结果、台账全部落库。`GET /api/casualties/{id}/trace`
  沿伤员查到每笔请求最终调剂到了哪家医院、哪个批次、多少袋、状态与完整事件时间线。

## 状态机

```
pending ──(锁库成功)──> held ──(实发)──> shipped
   ▲                     │
   └──(超时/过期释放)─────┤
pending/held ──(取消)──> cancelled
```

## 启动

```bash
python3 app.py --db ./blood-desk.db --port 8323
# 可选：--hold-ttl 180
```

打开 http://127.0.0.1:8323/ 即为调剂台页面（页面本身免鉴权；API 需 `X-User-Id`、`X-Role` 头，
角色：`blood_bank` / `dispatcher` / `hospital` / `admin`）。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` `/` | 健康检查、页面 |
| POST/GET | `/api/hospitals` | 医院登记（名称、距离km）/列表 |
| POST | `/api/hospitals/{id}/batches` | 登记血袋：血型、成分、效期、数量 |
| GET | `/api/batches` | 库存视图：在库/已占用/可调剂/是否过期 |
| POST/GET | `/api/casualties` | 伤员登记（血型）/列表 |
| POST | `/api/requests` | 用血请求：`casualty_id`、血型、成分、用量，提交即匹配 |
| GET | `/api/requests?state=` | 请求列表（held/pending/shipped/cancelled） |
| POST | `/api/requests/{id}/ship` | 医院实发（扣库），可选部分实发明细 |
| POST | `/api/requests/{id}/cancel` | 取消并释放占用（`reason`） |
| POST | `/api/sweep` | 超时/过期清扫并晋升排队 |
| GET | `/api/casualties/{id}/trace` | 沿伤员查调剂去向 |
| GET | `/api/stats` | 请求状态与库存统计 |

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：相容矩阵、血浆反向规则、FEFO+距离排序、两家抢批后提交留待、取消释放晋升、
超时释放与 FIFO 重排、过期拒收与锁库过期拦截、实发扣库与部分实发、重启后伤员溯源、权限与非法状态。
