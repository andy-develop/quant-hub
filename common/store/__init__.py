"""数据层唯一读入口（上层只 `from common.store import load`）。

上层开发者边界（CI grep 门禁强制）：

    ✅ from common.store import load
    ❌ pd.read_parquet('data/market/...')     # 绕过契约与修复件覆盖
    ❌ 任何写 data/ 的代码                     # 数据仓只对 CI bot 可写

新增一个策略 = domains/new/ + web/fragments/new.html + state/new/ + router 加一行；
底层零改动。新增一个因子 = domains/<d>/factors/xxx.py + state/<d>/factors/xxx.parquet。
"""

from .reader import (  # noqa: F401
    ContractViolation,
    FrozenPartitionError,
    StoreError,
    code_to_secid,
    code_to_symbol,
    contract_version,
    load,
    load_meta,
    normalize_code,
)
from .schema import (  # noqa: F401
    AGGREGATOR_VERSION,
    ASSETS,
    BROAD_INDEX_CODES,
    COLUMNS,
    CONTRACT_VERSION,
    DOMAINS,
    FQ,
    FREQ,
    INDEX_SPECIAL,
    INVARIANTS,
    OPTIONAL,
    PARTIAL_FLAG,
    PRIMARY_KEY,
    RETENTION,
    check_retention,
)

__all__ = [
    "load", "load_meta", "normalize_code", "code_to_secid", "code_to_symbol",
    "contract_version",
    "StoreError", "ContractViolation", "FrozenPartitionError",
    "CONTRACT_VERSION", "COLUMNS", "OPTIONAL", "INVARIANTS", "FQ", "FREQ",
    "PARTIAL_FLAG", "RETENTION", "ASSETS", "DOMAINS", "AGGREGATOR_VERSION",
    "BROAD_INDEX_CODES", "INDEX_SPECIAL", "PRIMARY_KEY", "check_retention",
]
