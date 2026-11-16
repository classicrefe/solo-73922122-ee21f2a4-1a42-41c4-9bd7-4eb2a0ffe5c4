# 混样组成录入与浓度还原平台

检测数据工程师把已完成混样的组成交给结果还原作业的录入/更正平台，并依据各混池
浓度精确还原各样本浓度的可行上下界。
FastAPI + PostgreSQL + 原生网页（无前端构建步骤），Docker Compose 一键启动，带演示数据。

## 1. 业务规则

三类记录：

| 表 | 主键 | 体积字段 | 约束 |
|---|---|---|---|
| `samples` 样本原量 | `sample_no` | `amount_ul` | 非负整数 μL |
| `pools` 混池总量 | `pool_no` | `total_ul` | 非负整数 μL |
| `pool_details` 入池明细 | `(sample_no, pool_no)` | `amount_ul` | **正**整数 μL |

- **R1** 每个混池的明细用量合计 = 混池总量。
- **R2** 每个样本跨所有混池的累计用量 ≤ 样本原量。
- **R3** 事务内部允许暂时失衡，只有最终合法的事务才能提交。
- **R4** 并发事务若共同导致失衡，整单拒绝（不能各自通过却共同提交失衡数据）；
  与冲突无关的样本/混池仍可并行提交。
- 每单（一次请求）至多 20 项操作；失败全部回滚；规则在数据库内强制，
  独立的 READ COMMITTED SQL 连接对三类记录做增删改同样受约束；重启后规则仍有效。

## 2. 快速启动

```bash
docker compose up --build
# 网页:        http://localhost:8000/
# API 文档:    http://localhost:8000/docs
# PostgreSQL: localhost:5432 (用户/密码/库: lab/lab/labdb)
```

`db/01_schema.sql` 与 `db/02_seed.sql` 通过 `docker-entrypoint-initdb.d`
仅在数据卷为空时自动执行。演示数据：样本 S001–S004、混池 P001–P003，
初始状态完全合法（P003 为空池）。

重新初始化数据：`docker compose down -v && docker compose up --build`

## 3. 规则是怎么强制的（为什么独立 SQL 连接也守规则）

规则的最终防线全部在数据库内（`db/01_schema.sql`），不依赖应用进程：

1. **列级 CHECK**：样本/混池体积非负、明细用量为正。
2. **主键/唯一约束**：明细 `(sample_no, pool_no)` 唯一。
3. **外键 `ON DELETE RESTRICT`**：仍被明细引用的样本/混池不可删除；
   编号修改走 `ON UPDATE CASCADE`。
4. **DEFERRABLE INITIALLY DEFERRED 行级约束触发器**（三张表各一个）：
   PostgreSQL 只有约束触发器可延迟。多语句显式事务中它们挂起，
   在 `COMMIT` 或 `SET CONSTRAINTS ALL IMMEDIATE` 时按事务**最终状态**
   逐行校验 R1/R2，违规以 `SQLSTATE 23514 (check_violation)` + JSON `DETAIL`
   （含违规对象编号与数值）抛错，事务整体回滚。
   自动提交下的单条非法语句在其提交点即被拒绝。
5. **并发加锁**：
   - 应用在每个订单事务的最开头，用单条 `SELECT ... ORDER BY ... FOR UPDATE`
     按全局确定顺序预锁本单涉及的全部混池行与样本行（必须早于明细 INSERT
     触发的外键行锁，否则外键的 `KEY SHARE` 行锁与 `FOR UPDATE` 会形成死锁环）。
   - 表上另有语句级锁触发器（明细/池/样本的 INSERT/UPDATE/DELETE 各一），
     保证任何连接写入时相关父行都被锁定。
   - 重叠事务在同一行上排队、后提交者看到先提交者的数据再校验；
     作用域不相交的事务锁不同的行，互不阻塞。
   - 连接设置 `lock_timeout=10s`，死锁（40P01）/序列化（40001）/锁超时（55P03）
     由应用整单重试，仍失败则整单 409 拒绝。

应用层（`app/service.py`）在提交前还做一次**全量预检查**，作用是一次性把
全部违规对象返回给网页展示；数据库触发器是不依赖应用的硬保证。

## 4. HTTP API

### `POST /api/orders` — 按单录入/更正（一个订单 = 一个事务）

```json
{
  "samples": [ {"op": "update", "sample_no": "S001", "amount_ul": 1200} ],
  "pools":   [ {"op": "update", "pool_no": "P003", "total_ul": 100} ],
  "details": [
    {"op": "insert", "sample_no": "S004", "pool_no": "P003", "amount_ul": 60},
    {"op": "insert", "sample_no": "S002", "pool_no": "P003", "amount_ul": 40}
  ],
  "dry_run": false
}
```

- `op`：`insert` / `update` / `delete`；三类合计 ≤ 20 项，至少 1 项。
- `insert/update` 必须带体积；`delete` 只需编号；同单编号不可重复操作。
- `dry_run: true` 时执行全部校验但 `ROLLBACK`，不提交。
- 成功 `200`：返回 `committed:true` 与提交后的完整组成。
- 规则违规 `409`：`violations` 数组列出**全部违规对象**，例如
  `{"type":"pool_mismatch","object_kind":"pool","object":"P001","pool_total":1,"detail_sum":500,...}`。
- 目标不存在的 update/delete：`404`（不会静默变成 insert）。
- 唯一键/外键冲突：`409`；入参错误（超 20 项、负体积、明细 0、空编号）：`422`。

### `GET /api/composition` — 取数

单一只读 `REPEATABLE READ` 事务、单条聚合 SQL，返回同一已提交快照下的完整组成：
每个样本的原量/累计用量/剩余，每个混池的总量/明细合计/明细列表及平衡状态。

### `GET /health`

健康检查。

## 5. 浓度还原（新增）

### 5.1 业务语义

选 **1–8 个非空混池**、涉及**至多 8 个样本**，为每个池录入一个**非负有理数**
浓度 `c`（接受 `3`、`0.25`、`2/3`、`1_1/2` 等）。对每个混池满足守恒式

```
Σ(入池明细用量 × 样本浓度) = 混池总量 × c
```

并要求各样本浓度非负。系统在该线性约束下求每个样本浓度的**可行下界与上界**，
并给出三种结论：

| 结论 | 含义 |
|---|---|
| `unique` 唯一 | 每个样本下界=上界，可行域只有一个点，才允许把该值作为还原值 |
| `non_unique` 非唯一 | 至少一个样本下界<上界，只给可行区间，**绝不把任意拟合/顶点值当还原值** |
| `infeasible` 无解 | 非负约束下不存在可行解，单独复测 |

全部计算使用 Python `fractions.Fraction` 精确有理数，求解器为精确两阶段单纯形
（Bland 反循环），8×8 规模毫秒级完成，端点与结论无浮点误差，结果以分数展示。

### 5.2 测值绑定、失效与停供（数据库内强制）

- 测值录入即**绑定当时该池的总量与明细**（`composition_versions` 指纹）。
- 网页或独立 SQL 更正某池的总量/明细后，该池测值自动置 `superseded`、必须重新
  确认；引用该池的还原结果**永久失效**（`valid=false`），**改回原组成也不恢复**；
  与该池无关的结果与测值不受影响。同值 UPDATE 不构成更正、不触发失效。
- 重新确认测值（即使浓度相同）同样令引用该池的旧结果失效。
- 失效由 `pools` / `pool_details` 上的行级触发器在数据库内完成，独立
  READ COMMITTED SQL 连接同样生效，应用重启后规则仍在。
- **计算期间可更正**：后台计算在发布前用 `SELECT ... FOR UPDATE` 锁定涉及池并比对
  实时指纹与测值绑定版本；不一致即判定过期，只登记 `failed` 未发布结果，
  **迟到计算不发布过期结果**。
- **失败不发布半份结果**：任何计算异常只落 `failed`（`published=false`）行；
  应用重启时把上次未完成（pending/running）任务一律置 failed，已完成结论原样保留。
- **供数只开放仍有效且已发布的完整结果**（`valid=true AND published=true`），
  结果内含计算时的组成、测值与分数区间快照供回查；失效/失败结果永久留档、
  可按 id 回查但不进入供数，且没有任何路径把 `valid` 改回 `true`。

### 5.3 还原 API

```bash
# 确认测值并创建后台计算任务 (同步确认事务 + 后台精确求解)
curl -X POST localhost:8000/api/restoration/jobs -H 'Content-Type: application/json' \
  -d '{"measurements":[{"pool_no":"P001","concentration":"3"},
                       {"pool_no":"P002","concentration":"4"}]}'

# 查任务状态 (pending/running/succeeded/failed)
curl localhost:8000/api/restoration/jobs/1

# 供数: 仅仍有效且已发布的完整结果
curl localhost:8000/api/restoration/results

# 按 id 回查任意结果 (含失效/失败留档)
curl localhost:8000/api/restoration/results/1

# 查全部测值及其 confirmed/superseded 状态
curl localhost:8000/api/restoration/measurements
```

- 非负有理数解析失败：`422 invalid_rational`；空池：`409`；混池不存在：`404`；
  超过 8 池或重复池号：`422`；涉及超过 8 样本：确认阶段同步 `409`。
- 结果 `bounds` 同时给出分数字符串与分子/分母整数；`unique` 字段标识该样本是否唯一确定。

## 6. 网页

`static/index.html`，原生 HTML/CSS/JS，三个标签页：

- **录入 / 更正**：三列分别录入样本、混池、明细，实时显示项数（0/20），
  支持「提交订单」与「试运行」，违规时用彩色徽章标出违规对象。
- **浓度还原**：勾选 1–8 个非空混池并录入浓度，展示计算任务状态、供数结果
  （唯一/非唯一/无解徽章、分数上下界、当时组成与测值快照），并提供失效/失败
  结果按 id 回查入口；空池置灰不可选。
- **组成查询**：展示样本与混池的平衡/超量状态。

## 7. 独立 SQL 连接验证（READ COMMITTED）

```bash
docker compose exec db psql -U lab -d labdb

-- 单条非法语句: 提交点即被触发器拒绝
UPDATE pools SET total_ul = 1 WHERE pool_no = 'P001';

-- 多语句事务: 允许中途暂时失衡, 最终合法才提交
BEGIN;
UPDATE pools SET total_ul = 0 WHERE pool_no = 'P003';
DELETE FROM pool_details WHERE pool_no = 'P003';
INSERT INTO pool_details VALUES ('S004','P003', 25);
UPDATE pools SET total_ul = 25 WHERE pool_no = 'P003';
COMMIT;

-- 独立 SQL 更正混池组成: 该池测值自动 superseded、相关还原结果永久失效
BEGIN;
UPDATE pool_details SET amount_ul = 350 WHERE sample_no='S001' AND pool_no='P001';
UPDATE pool_details SET amount_ul = 150 WHERE sample_no='S002' AND pool_no='P001';
COMMIT;
-- 同值 UPDATE (总量/明细都不变) 不触发失效
```

## 8. 自测

```bash
# 需要本机有 python3 并安装 requirements.txt 中的依赖
# 录入平台回归 (订单/规则/触发器/快照; 并发用例依赖多线程)
DATABASE_URL="postgresql://lab:lab@localhost:5432/labdb" \
BASE_URL="http://localhost:8000" \
python3 tests/selftest.py

# 还原作业端到端 (精确求解、失效停供、改回不恢复、无关池隔离、裸 SQL 触发器、失败不发布)
DATABASE_URL="postgresql://lab:lab@localhost:5432/labdb" \
BASE_URL="http://localhost:8000" \
python3 tests/restore_selftest.py
```

`restore_selftest.py` 覆盖：唯一/非唯一/无解三类结论与分数精度、非负有理数
校验、空池/超池/超样本拒绝、更正后停供与永久失效（改回不恢复）、无关池隔离、
独立 SQL 连接触发失效、同值 UPDATE 不失效、失败不发布半份结果。
两个脚本都会重建 schema 并重置演示数据，可重复运行。

## 9. 目录结构

```
db/01_schema.sql              样本/混池/明细表、CHECK、外键、锁触发器、deferred 校验
db/02_seed.sql                演示数据（显式事务）
db/03_restoration_schema.sql  版本指纹、测值、还原结果/任务表与失效触发器
app/config.py                 环境变量配置
app/database.py               连接池、锁超时、错误映射、全量预检查 SQL
app/models.py                 订单 Pydantic 入参模型（20 项上限、非负/正整数）
app/restore_models.py         还原请求模型（1–8 池、非负有理数字符串）
app/solver.py                 精确有理数两阶段单纯形（Fraction，可行上下界）
app/service.py                订单事务（预锁→增删改→预检查→触发器终审→提交）
app/restore_service.py        测值确认、后台计算、发布守卫、失效与供数/回查
app/main.py                   FastAPI 路由与异常处理（含启动恢复）
static/index.html             单页网页（录入/还原/查询三标签）
tests/selftest.py             录入平台端到端自测
tests/restore_selftest.py     还原作业端到端自测（32 项断言）
Dockerfile / docker-compose.yml / requirements.txt
```
