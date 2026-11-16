-- ============================================================================
-- 混池浓度还原 - 数据库结构、版本指纹与失效机制
--
-- 核心概念:
--   * composition_versions  混池"总量+明细"的版本指纹。测值录入时绑定当时版本;
--                          指纹内容相同则复用同一 version_id, 但失效状态不随之恢复。
--   * pool_measurements    各混池当前确认的浓度测值 (非负有理数, 分子/分母精确存储),
--                          绑定 version_id。组成更正后旧测值自动置 superseded,
--                          必须重新确认; 改回原组成也不恢复旧测值/旧结果。
--   * restoration_results  还原计算的完整结论 (唯一/非唯一/无解/失败),
--                          published 才允许供数; valid=false 永久失效且不恢复。
--   * restoration_result_pools  结果与混池的多对多关联, 供触发器精准失效
--                          (只失效相关池的结果, 无关池不受影响)。
--
-- 失效保证 (独立 READ COMMITTED SQL 连接同样生效):
--   pools / pool_details 上的行级 AFTER 触发器在真实发生数据变化时
--   (无变化的同值 UPDATE 不算更正), 把相关混池的 confirmed 测值置为
--   superseded, 并把引用这些混池的有效结果置为 valid=false。
--   触发器随 schema 持久化, 应用重启后依然有效; 外层事务回滚则失效一并回滚。
-- ============================================================================

-- ---------- 混池组成版本指纹 -------------------------------------------------

CREATE TABLE IF NOT EXISTS composition_versions (
    version_id  bigserial PRIMARY KEY,
    pool_no     text NOT NULL
        REFERENCES pools(pool_no) ON UPDATE CASCADE ON DELETE RESTRICT,
    total_ul    bigint NOT NULL CHECK (total_ul >= 0),
    detail_json jsonb NOT NULL,
    -- 规范化签名: total=<总量>;<样本号>:<用量>;... 按样本号排序
    detail_sig  text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- 同一混池同一组成只保留一个指纹行 (内容相同即可复用, 但不影响失效历史)
CREATE UNIQUE INDEX IF NOT EXISTS uq_composition_versions_sig
    ON composition_versions(pool_no, detail_sig);

-- ---------- 混池浓度测值 -----------------------------------------------------

CREATE TABLE IF NOT EXISTS pool_measurements (
    measurement_id bigserial PRIMARY KEY,
    pool_no        text NOT NULL
        REFERENCES pools(pool_no) ON UPDATE CASCADE ON DELETE RESTRICT,
    -- 非负有理数 c = conc_num / conc_den, 精确存储, 不使用浮点
    conc_num       bigint NOT NULL CHECK (conc_num >= 0),
    conc_den       bigint NOT NULL CHECK (conc_den > 0),
    version_id     bigint NOT NULL
        REFERENCES composition_versions(version_id),
    -- confirmed=当前有效且绑定当前组成; superseded=被更正/重新确认替换, 仅留档
    status         text NOT NULL DEFAULT 'confirmed'
        CHECK (status IN ('confirmed', 'superseded')),
    created_at     timestamptz NOT NULL DEFAULT now(),
    superseded_at  timestamptz
);

CREATE INDEX IF NOT EXISTS idx_measurements_pool_status
    ON pool_measurements(pool_no, status);

-- 一个混池至多一条 confirmed 测值 (部分唯一索引)
CREATE UNIQUE INDEX IF NOT EXISTS uq_measurements_confirmed
    ON pool_measurements(pool_no) WHERE status = 'confirmed';

-- ---------- 还原结果 ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS restoration_results (
    result_id      bigserial PRIMARY KEY,
    -- unique=唯一解; non_unique=区间非退化; infeasible=无解; failed=计算失败(不发布)
    status         text NOT NULL
        CHECK (status IN ('unique', 'non_unique', 'infeasible', 'failed')),
    pool_count     integer NOT NULL CHECK (pool_count BETWEEN 1 AND 8),
    -- 失败任务可能在确定样本集合前就终止, 因此允许 0; 成功结果必为 1..8
    sample_count   integer NOT NULL CHECK (sample_count BETWEEN 0 AND 8),
    -- 计算时的完整快照: 混池组成、测值、样本顺序、各样本可行上下界
    snapshot_json  jsonb NOT NULL,
    bounds_json    jsonb NOT NULL,
    error_message  text,
    published      boolean NOT NULL DEFAULT false,
    valid          boolean NOT NULL DEFAULT true,
    invalid_reason text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    published_at   timestamptz,
    invalidated_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_results_valid_published
    ON restoration_results(valid, published);

-- 成功 (非 failed) 结果必须涉及 1..8 个样本; failed 行允许 0
ALTER TABLE restoration_results
    DROP CONSTRAINT IF EXISTS restoration_results_sample_count_check;
ALTER TABLE restoration_results
    ADD CONSTRAINT restoration_results_sample_count_check
    CHECK (sample_count BETWEEN 0 AND 8);
ALTER TABLE restoration_results
    DROP CONSTRAINT IF EXISTS restoration_results_published_samples_check;
ALTER TABLE restoration_results
    ADD CONSTRAINT restoration_results_published_samples_check
    CHECK (status = 'failed' OR sample_count BETWEEN 1 AND 8);

CREATE TABLE IF NOT EXISTS restoration_result_pools (
    result_id bigint NOT NULL
        REFERENCES restoration_results(result_id) ON DELETE CASCADE,
    pool_no   text NOT NULL
        REFERENCES pools(pool_no) ON UPDATE CASCADE ON DELETE RESTRICT,
    PRIMARY KEY (result_id, pool_no)
);

CREATE INDEX IF NOT EXISTS idx_result_pools_pool
    ON restoration_result_pools(pool_no);

-- ---------- 还原计算任务 (异步计算, 状态持久化以支持重启保留结论) -------------

CREATE TABLE IF NOT EXISTS restoration_jobs (
    job_id        bigserial PRIMARY KEY,
    -- pending=排队; running=计算中; succeeded=已发布完整结果; failed=失败(未发布)
    status        text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
    request_json  jsonb NOT NULL,
    result_id     bigint REFERENCES restoration_results(result_id),
    error_message text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    started_at    timestamptz,
    finished_at   timestamptz
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON restoration_jobs(status);

-- ---------- 组成指纹函数 (应用与人工 SQL 均可调用) ---------------------------

CREATE OR REPLACE FUNCTION pool_composition_fingerprint(p_pool text)
RETURNS jsonb
LANGUAGE sql
STABLE
AS $$
    SELECT jsonb_build_object(
        'pool_no', p.pool_no,
        'total_ul', p.total_ul,
        'details', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                       'sample_no', d.sample_no,
                       'amount_ul', d.amount_ul
                   ) ORDER BY d.sample_no)
              FROM pool_details d
             WHERE d.pool_no = p.pool_no), '[]'::jsonb),
        'detail_sig',
            'total=' || p.total_ul || ';' || COALESCE((
                SELECT string_agg(d.sample_no || ':' || d.amount_ul, ';'
                                  ORDER BY d.sample_no)
                  FROM pool_details d
                 WHERE d.pool_no = p.pool_no), '')
    )
      FROM pools p
     WHERE p.pool_no = p_pool;
$$;

-- ---------- 失效逻辑: 按混池作废测值与结果 -----------------------------------

-- 把指定混池的当前测值作废 (组成更正时调用)
CREATE OR REPLACE FUNCTION invalidate_measurements_of(p_pool text)
RETURNS void
LANGUAGE sql
AS $$
    UPDATE pool_measurements
       SET status = 'superseded', superseded_at = now()
     WHERE pool_no = p_pool
       AND status = 'confirmed';
$$;

-- 把引用指定混池的有效还原结果永久作废 (组成更正/测值重新确认时调用)
CREATE OR REPLACE FUNCTION invalidate_results_of(p_pool text, p_reason text)
RETURNS void
LANGUAGE sql
AS $$
    UPDATE restoration_results r
       SET valid = false,
           invalidated_at = now(),
           invalid_reason = COALESCE(
               r.invalid_reason,
               p_reason || ':' || p_pool
           )
     WHERE r.valid = true
       AND r.result_id IN (
            SELECT rp.result_id
              FROM restoration_result_pools rp
             WHERE rp.pool_no = p_pool
       );
$$;

-- ---------- 触发器: pools 总量(或编号)真实变化 -> 失效 ------------------------

CREATE OR REPLACE FUNCTION trg_restore_pools_invalidate() RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_pool text;
BEGIN
    v_pool := COALESCE(NEW.pool_no, OLD.pool_no);
    IF TG_OP = 'UPDATE'
       AND NEW.pool_no IS NOT DISTINCT FROM OLD.pool_no
       AND NEW.total_ul IS NOT DISTINCT FROM OLD.total_ul THEN
        -- 同值 UPDATE 不构成更正, 不作废任何数据
        RETURN NULL;
    END IF;
    PERFORM invalidate_measurements_of(v_pool);
    PERFORM invalidate_results_of(v_pool, 'composition_correction');
    RETURN NULL;
END
$$;

DROP TRIGGER IF EXISTS trg_restore_pools_invalidate ON pools;
CREATE TRIGGER trg_restore_pools_invalidate
AFTER UPDATE OR DELETE ON pools
FOR EACH ROW
EXECUTE FUNCTION trg_restore_pools_invalidate();

-- ---------- 触发器: pool_details 真实变化 -> 失效 ----------------------------

CREATE OR REPLACE FUNCTION trg_restore_details_invalidate() RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_pool text;
BEGIN
    v_pool := COALESCE(NEW.pool_no, OLD.pool_no);
    IF TG_OP = 'UPDATE'
       AND NEW.pool_no IS NOT DISTINCT FROM OLD.pool_no
       AND NEW.sample_no IS NOT DISTINCT FROM OLD.sample_no
       AND NEW.amount_ul IS NOT DISTINCT FROM OLD.amount_ul THEN
        RETURN NULL;
    END IF;
    PERFORM invalidate_measurements_of(v_pool);
    PERFORM invalidate_results_of(v_pool, 'composition_correction');
    RETURN NULL;
END
$$;

DROP TRIGGER IF EXISTS trg_restore_details_invalidate ON pool_details;
CREATE TRIGGER trg_restore_details_invalidate
AFTER INSERT OR UPDATE OR DELETE ON pool_details
FOR EACH ROW
EXECUTE FUNCTION trg_restore_details_invalidate();
