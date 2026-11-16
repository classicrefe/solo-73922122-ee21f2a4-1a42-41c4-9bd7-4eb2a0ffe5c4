"""应用配置: 全部来自环境变量, 带本地开发默认值。"""
import os


class Settings:
    db_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql://lab:lab@localhost:5432/labdb",
    )
    # 连接池大小: 网页请求 + 少量并发订单
    pool_min_size: int = int(os.getenv("DB_POOL_MIN", "2"))
    pool_max_size: int = int(os.getenv("DB_POOL_MAX", "10"))
    # 死锁/序列化冲突时整单重试次数 (重试的是整个订单事务)
    max_retries: int = int(os.getenv("ORDER_MAX_RETRIES", "3"))


settings = Settings()
