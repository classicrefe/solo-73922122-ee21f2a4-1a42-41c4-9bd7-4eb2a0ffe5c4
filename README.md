# 混样组成录入平台

检测数据工程师把已完成混样的组成交给结果还原作业的录入/更正平台。
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

## 5. 网页

`static/index.html`，原生 HTML/CSS/JS：三列分别录入样本、混池、明细，
实时显示项数（0/20）；支持「提交订单」与「试运行」；违规时用彩色徽章
标出违规对象（混池/样本编号 + 原因）；组成查询页展示平衡/超量状态。

## 6. 独立 SQL 连接验证（READ COMMITTED）

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
```

## 7. 自测

```bash
# 需要本机有 python3 并安装 requirements.txt 中的依赖
DATABASE_URL="postgresql://lab:lab@localhost:5432/labdb" \
BASE_URL="http://localhost:8000" \
python3 tests/selftest.py
```

覆盖：API 录入/更正/试运行、R1/R2 违规与违规对象展示、422/404、
独立 SQL 连接的 CHECK/触发器/回滚、API 与裸 SQL 两条并发路径
（一胜一拒、死锁整单拒绝、无关事务并行）、取数快照一致性。
脚本会重建 schema 并重置演示数据，可重复运行。

## 8. 目录结构

```
db/01_schema.sql          表、CHECK、外键、锁触发器、deferred 校验触发器
db/02_seed.sql            演示数据（显式事务）
app/config.py             环境变量配置
app/database.py           连接池、锁超时、错误映射、全量预检查 SQL
app/models.py             Pydantic 入参模型（20 项上限、非负/正整数）
app/service.py            订单事务（预锁→增删改→预检查→触发器终审→提交）
app/main.py               FastAPI 路由与异常处理
static/index.html         单页网页
tests/selftest.py         端到端自测（37 项断言）
Dockerfile / docker-compose.yml / requirements.txt
```
