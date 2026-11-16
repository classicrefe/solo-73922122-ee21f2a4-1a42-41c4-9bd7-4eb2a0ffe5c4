-- ============================================================================
-- 混样组成数据平台 - 数据库结构与规则强制执行
--
-- 三类记录:
--   samples       样本原量 (sample_no, amount_ul)  体积: 非负整数微升
--   pools         混池总量 (pool_no,   total_ul)   体积: 非负整数微升
--   pool_details  入池明细 (sample_no, pool_no, amount_ul)  用量: 正整数
--
-- 业务规则 (在数据库内强制, 任何独立 READ COMMITTED 连接都必须遵守):
--   R1 每池明细用量合计 = 混池总量
--   R2 每样本跨池累计用量 <= 样本原量
--   R3 允许事务内部暂时失衡, 仅在提交时要求最终合法
--   R4 并发事务若共同导致失衡则整单拒绝; 互不相关的样本/混池仍可并行提交
--
-- 实现要点 (PostgreSQL 15):
--   * 列级 CHECK 保证非负/正整数; 主键保证明细 (样本,混池) 唯一;
--     外键 RESTRICT 阻止删除仍被引用的样本/混池。
--   * PostgreSQL 只有约束触发器可 DEFERRABLE, 且仅支持行级。
--     INITIALLY DEFERRED 约束触发器在多语句显式事务中挂起, 到
--     COMMIT 或 SET CONSTRAINTS ... IMMEDIATE 时, 按事务最终状态
--     对每个受影响行统一执行校验, 因此事务内可暂时失衡, 只有最终
--     合法的事务才能提交; 自动提交下的单条非法语句在其提交点被拒。
--     应用将一单内的多语句组织为一个原子事务, 提交前先做全量预检查。
--   * 并发: 应用在订单事务开头用 SELECT ... ORDER BY ... FOR UPDATE 按全局
--     确定顺序预锁本单涉及的全部混池/样本行 (必须早于明细 INSERT 触发的
--     外键 KEY SHARE 行锁, 否则与 FOR UPDATE 构成死锁环); 表上的语句级
--     锁触发器 (明细/池/样本) 保证任何连接写入时相关父行同样按序锁定。
--     重叠事务在同一行排队、后提交者看到先提交者的数据再校验;
--     作用域不相交的事务锁不同行, 互不阻塞。
--   * 违规 RAISE 带 ERRCODE check_violation 与 JSON DETAIL (违规对象),
--     事务失败整体回滚; 触发器随 schema 持久化, 重启后依然有效。
-- ============================================================================

-- ---------- 表结构 ----------------------------------------------------------

CREATE TABLE IF NOT EXISTS samples (
    sample_no text PRIMARY KEY,
    amount_ul bigint NOT NULL CHECK (amount_ul >= 0)
);

CREATE TABLE IF NOT EXISTS pools (
    pool_no   text PRIMARY KEY,
    total_ul  bigint NOT NULL CHECK (total_ul >= 0)
);

CREATE TABLE IF NOT EXISTS pool_details (
    sample_no text NOT NULL
        REFERENCES samples(sample_no) ON UPDATE CASCADE ON DELETE RESTRICT,
    pool_no   text NOT NULL
        REFERENCES pools(pool_no)   ON UPDATE CASCADE ON DELETE RESTRICT,
    amount_ul bigint NOT NULL CHECK (amount_ul > 0),
    PRIMARY KEY (sample_no, pool_no)
);

CREATE INDEX IF NOT EXISTS idx_details_pool_no   ON pool_details(pool_no);
CREATE INDEX IF NOT EXISTS idx_details_sample_no ON pool_details(sample_no);

-- ---------- 立即执行的锁触发器 (明细增删改) --------------------------------
-- 带 transition table 的触发器只能绑定单个事件, 故 INSERT/UPDATE/DELETE 各一个。
-- 按编号排序锁定涉及的混池与样本, 让重叠事务以确定顺序排队, 避免死锁,
-- 并保证后提交者的 deferred 校验能读到先提交者的已提交数据。

CREATE OR REPLACE FUNCTION trg_details_lock_parents() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    r record;
BEGIN
    -- 按编号排序锁定涉及的混池
    IF TG_OP = 'DELETE' THEN
        FOR r IN SELECT DISTINCT pool_no AS k FROM old_table ORDER BY k LOOP
            PERFORM 1 FROM pools WHERE pool_no = r.k FOR UPDATE;
        END LOOP;
    ELSIF TG_OP = 'INSERT' THEN
        FOR r IN SELECT DISTINCT pool_no AS k FROM new_table ORDER BY k LOOP
            PERFORM 1 FROM pools WHERE pool_no = r.k FOR UPDATE;
        END LOOP;
    ELSE -- UPDATE: 新旧键都要锁 (本项目不允许改键, 保留通用处理)
        FOR r IN
            SELECT DISTINCT pool_no AS k FROM (
                SELECT pool_no FROM new_table
                UNION
                SELECT pool_no FROM old_table
            ) affected ORDER BY k
        LOOP
            PERFORM 1 FROM pools WHERE pool_no = r.k FOR UPDATE;
        END LOOP;
    END IF;

    -- 按编号排序锁定涉及的样本
    IF TG_OP = 'DELETE' THEN
        FOR r IN SELECT DISTINCT sample_no AS k FROM old_table ORDER BY k LOOP
            PERFORM 1 FROM samples WHERE sample_no = r.k FOR UPDATE;
        END LOOP;
    ELSIF TG_OP = 'INSERT' THEN
        FOR r IN SELECT DISTINCT sample_no AS k FROM new_table ORDER BY k LOOP
            PERFORM 1 FROM samples WHERE sample_no = r.k FOR UPDATE;
        END LOOP;
    ELSE
        FOR r IN
            SELECT DISTINCT sample_no AS k FROM (
                SELECT sample_no FROM new_table
                UNION
                SELECT sample_no FROM old_table
            ) affected ORDER BY k
        LOOP
            PERFORM 1 FROM samples WHERE sample_no = r.k FOR UPDATE;
        END LOOP;
    END IF;

    RETURN NULL;
END
$$;

DROP TRIGGER IF EXISTS trg_details_lock_insert ON pool_details;
CREATE TRIGGER trg_details_lock_insert
AFTER INSERT ON pool_details
REFERENCING NEW TABLE AS new_table
FOR EACH STATEMENT EXECUTE FUNCTION trg_details_lock_parents();

DROP TRIGGER IF EXISTS trg_details_lock_update ON pool_details;
CREATE TRIGGER trg_details_lock_update
AFTER UPDATE ON pool_details
REFERENCING NEW TABLE AS new_table OLD TABLE AS old_table
FOR EACH STATEMENT EXECUTE FUNCTION trg_details_lock_parents();

DROP TRIGGER IF EXISTS trg_details_lock_delete ON pool_details;
CREATE TRIGGER trg_details_lock_delete
AFTER DELETE ON pool_details
REFERENCING OLD TABLE AS old_table
FOR EACH STATEMENT EXECUTE FUNCTION trg_details_lock_parents();

-- pools / samples 自身的增删改也立即按编号排序锁定本语句涉及的行。
-- 作用: 消除 "外键行锁(无序) + 池总量更新锁" 之间的加锁顺序差异,
--       使所有父行锁都按全局确定顺序 (先 pools 按 pool_no, 再 samples 按
--       sample_no) 获取, 重叠并发事务只会排队不会死锁; 不相交事务互不阻塞。

CREATE OR REPLACE FUNCTION trg_parent_lock_self() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    r record;
    v_col text;
    v_nt  text;
    v_ot  text;
BEGIN
    IF TG_TABLE_NAME = 'pools' THEN
        v_col := 'pool_no';
    ELSE
        v_col := 'sample_no';
    END IF;

    IF TG_OP = 'INSERT' THEN
        v_nt := 'new_table'; v_ot := NULL;
    ELSIF TG_OP = 'UPDATE' THEN
        v_nt := 'new_table'; v_ot := 'old_table';
    ELSE
        v_nt := NULL;        v_ot := 'old_table';
    END IF;

    IF v_nt IS NOT NULL THEN
        FOR r IN EXECUTE format(
            'SELECT DISTINCT %1$I AS k FROM %2$I ORDER BY k', v_col, v_nt
        )
        LOOP
            EXECUTE format('SELECT 1 FROM %I WHERE %I = $1 FOR UPDATE',
                           TG_TABLE_NAME, v_col)
                USING r.k;
        END LOOP;
    END IF;
    IF v_ot IS NOT NULL THEN
        FOR r IN EXECUTE format(
            'SELECT DISTINCT %1$I AS k FROM %2$I ORDER BY k', v_col, v_ot
        )
        LOOP
            EXECUTE format('SELECT 1 FROM %I WHERE %I = $1 FOR UPDATE',
                           TG_TABLE_NAME, v_col)
                USING r.k;
        END LOOP;
    END IF;

    RETURN NULL;
END
$$;

DROP TRIGGER IF EXISTS trg_pools_lock_insert ON pools;
CREATE TRIGGER trg_pools_lock_insert
AFTER INSERT ON pools REFERENCING NEW TABLE AS new_table
FOR EACH STATEMENT EXECUTE FUNCTION trg_parent_lock_self();
DROP TRIGGER IF EXISTS trg_pools_lock_update ON pools;
CREATE TRIGGER trg_pools_lock_update
AFTER UPDATE ON pools REFERENCING NEW TABLE AS new_table OLD TABLE AS old_table
FOR EACH STATEMENT EXECUTE FUNCTION trg_parent_lock_self();
DROP TRIGGER IF EXISTS trg_pools_lock_delete ON pools;
CREATE TRIGGER trg_pools_lock_delete
AFTER DELETE ON pools REFERENCING OLD TABLE AS old_table
FOR EACH STATEMENT EXECUTE FUNCTION trg_parent_lock_self();

DROP TRIGGER IF EXISTS trg_samples_lock_insert ON samples;
CREATE TRIGGER trg_samples_lock_insert
AFTER INSERT ON samples REFERENCING NEW TABLE AS new_table
FOR EACH STATEMENT EXECUTE FUNCTION trg_parent_lock_self();
DROP TRIGGER IF EXISTS trg_samples_lock_update ON samples;
CREATE TRIGGER trg_samples_lock_update
AFTER UPDATE ON samples REFERENCING NEW TABLE AS new_table OLD TABLE AS old_table
FOR EACH STATEMENT EXECUTE FUNCTION trg_parent_lock_self();
DROP TRIGGER IF EXISTS trg_samples_lock_delete ON samples;
CREATE TRIGGER trg_samples_lock_delete
AFTER DELETE ON samples REFERENCING OLD TABLE AS old_table
FOR EACH STATEMENT EXECUTE FUNCTION trg_parent_lock_self();

-- ---------- 提交时校验: 入池明细侧 ------------------------------------------

CREATE OR REPLACE FUNCTION trg_details_check_deferred() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_pool   text;
    v_sample text;
    v_total  bigint;
    v_sum    bigint;
    v_amount bigint;
    v_used   bigint;
    v_detail jsonb;
BEGIN
    v_pool   := COALESCE(NEW.pool_no,   OLD.pool_no);
    v_sample := COALESCE(NEW.sample_no, OLD.sample_no);

    -- R1: 该混池总量必须等于明细合计
    SELECT p.total_ul, COALESCE(d.s, 0)
      INTO v_total, v_sum
      FROM pools p
      LEFT JOIN (SELECT pool_no, sum(amount_ul) AS s
                   FROM pool_details GROUP BY pool_no) d
        ON d.pool_no = p.pool_no
     WHERE p.pool_no = v_pool;

    IF FOUND AND v_total IS DISTINCT FROM v_sum THEN
        v_detail := jsonb_build_object(
            'type', 'pool_mismatch', 'object_kind', 'pool', 'object', v_pool,
            'pool_total', v_total, 'detail_sum', v_sum,
            'message', format('混池 %s: 总量 %s μL 与明细合计 %s μL 不一致',
                              v_pool, v_total, v_sum));
        RAISE EXCEPTION '混样组成规则校验未通过'
            USING DETAIL = v_detail::text, ERRCODE = 'check_violation';
    END IF;

    -- R2: 该样本跨池累计用量不得超过原量
    SELECT s.amount_ul, COALESCE(d.u, 0)
      INTO v_amount, v_used
      FROM samples s
      LEFT JOIN (SELECT sample_no, sum(amount_ul) AS u
                   FROM pool_details GROUP BY sample_no) d
        ON d.sample_no = s.sample_no
     WHERE s.sample_no = v_sample;

    IF FOUND AND v_used > v_amount THEN
        v_detail := jsonb_build_object(
            'type', 'sample_overuse', 'object_kind', 'sample', 'object', v_sample,
            'sample_amount', v_amount, 'used', v_used,
            'message', format('样本 %s: 累计用量 %s μL 超过原量 %s μL',
                              v_sample, v_used, v_amount));
        RAISE EXCEPTION '混样组成规则校验未通过'
            USING DETAIL = v_detail::text, ERRCODE = 'check_violation';
    END IF;

    RETURN NULL;
END
$$;

DROP TRIGGER IF EXISTS trg_details_check ON pool_details;
CREATE CONSTRAINT TRIGGER trg_details_check
AFTER INSERT OR UPDATE OR DELETE ON pool_details
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION trg_details_check_deferred();

-- ---------- 提交时校验: 混池侧 (总量增删改) ---------------------------------

CREATE OR REPLACE FUNCTION trg_pools_check_deferred() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_pool   text;
    v_total  bigint;
    v_sum    bigint;
    v_detail jsonb;
BEGIN
    v_pool := COALESCE(NEW.pool_no, OLD.pool_no);

    SELECT p.total_ul, COALESCE(d.s, 0)
      INTO v_total, v_sum
      FROM pools p
      LEFT JOIN (SELECT pool_no, sum(amount_ul) AS s
                   FROM pool_details GROUP BY pool_no) d
        ON d.pool_no = p.pool_no
     WHERE p.pool_no = v_pool;

    IF FOUND AND v_total IS DISTINCT FROM v_sum THEN
        v_detail := jsonb_build_object(
            'type', 'pool_mismatch', 'object_kind', 'pool', 'object', v_pool,
            'pool_total', v_total, 'detail_sum', v_sum,
            'message', format('混池 %s: 总量 %s μL 与明细合计 %s μL 不一致',
                              v_pool, v_total, v_sum));
        RAISE EXCEPTION '混样组成规则校验未通过'
            USING DETAIL = v_detail::text, ERRCODE = 'check_violation';
    END IF;

    RETURN NULL;
END
$$;

DROP TRIGGER IF EXISTS trg_pools_check ON pools;
CREATE CONSTRAINT TRIGGER trg_pools_check
AFTER INSERT OR UPDATE OR DELETE ON pools
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION trg_pools_check_deferred();

-- ---------- 提交时校验: 样本侧 (原量增删改) ---------------------------------

CREATE OR REPLACE FUNCTION trg_samples_check_deferred() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_sample text;
    v_amount bigint;
    v_used   bigint;
    v_detail jsonb;
BEGIN
    v_sample := COALESCE(NEW.sample_no, OLD.sample_no);

    SELECT s.amount_ul, COALESCE(d.u, 0)
      INTO v_amount, v_used
      FROM samples s
      LEFT JOIN (SELECT sample_no, sum(amount_ul) AS u
                   FROM pool_details GROUP BY sample_no) d
        ON d.sample_no = s.sample_no
     WHERE s.sample_no = v_sample;

    IF FOUND AND v_used > v_amount THEN
        v_detail := jsonb_build_object(
            'type', 'sample_overuse', 'object_kind', 'sample', 'object', v_sample,
            'sample_amount', v_amount, 'used', v_used,
            'message', format('样本 %s: 累计用量 %s μL 超过原量 %s μL',
                              v_sample, v_used, v_amount));
        RAISE EXCEPTION '混样组成规则校验未通过'
            USING DETAIL = v_detail::text, ERRCODE = 'check_violation';
    END IF;

    RETURN NULL;
END
$$;

DROP TRIGGER IF EXISTS trg_samples_check ON samples;
CREATE CONSTRAINT TRIGGER trg_samples_check
AFTER INSERT OR UPDATE OR DELETE ON samples
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION trg_samples_check_deferred();
