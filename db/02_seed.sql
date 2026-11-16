-- ============================================================================
-- 演示数据: 四个样本、三个混池, 初始状态完全满足全部规则。
--
--   样本 S001 原量 1000 μL, 已入池 300+200 = 500
--   样本 S002 原量  800 μL, 已入池 200     = 200
--   样本 S003 原量  500 μL, 已入池 150     = 150
--   样本 S004 原量  300 μL, 尚未入池       = 0
--
--   混池 P001 总量 500 = S001 300 + S002 200
--   混池 P002 总量 350 = S001 200 + S003 150
--   混池 P003 总量   0 (空池, 等待录入明细)
--
-- 注意: 约束触发器在提交时按最终状态校验。整个演示数据放在一个显式事务中,
--       允许中途暂时失衡 (空池 -> 明细 -> 总量), 提交时最终状态合法即可。
-- ============================================================================

BEGIN;

INSERT INTO samples (sample_no, amount_ul) VALUES
    ('S001', 1000),
    ('S002',  800),
    ('S003',  500),
    ('S004',  300)
ON CONFLICT DO NOTHING;

INSERT INTO pools (pool_no, total_ul) VALUES
    ('P001', 0),
    ('P002', 0),
    ('P003', 0)
ON CONFLICT DO NOTHING;

INSERT INTO pool_details (sample_no, pool_no, amount_ul) VALUES
    ('S001', 'P001', 300),
    ('S002', 'P001', 200),
    ('S001', 'P002', 200),
    ('S003', 'P002', 150)
ON CONFLICT DO NOTHING;

UPDATE pools SET total_ul = 500 WHERE pool_no = 'P001';
UPDATE pools SET total_ul = 350 WHERE pool_no = 'P002';

COMMIT;
