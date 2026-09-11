# state/ · CI 中间态账本（方案 §3）

"CI 说了什么、依据什么算了什么"的账本，跟代码一起 review/blame/rollback。
分两类存储（混在一起就是短线域 lgbm_scores 10MB/天、ETF 域 data/ 归档 7MB 的两个坑）：

| 类别 | 判据 | 存哪 | 本目录 |
|---|---|---|---|
| 决策依据 | 不可重算/重算会变/出争议要复核 | **入 git** | `payload/`(carry-forward 权威快照) · `publish/`(HSK 幂等指纹) · `data/hsk-resource.json`(D3 资源留档) |
| 可复现派生物 | 给定 data/+sha 能逐位重算 | 不入 git（Actions cache/artifacts） | `*/factors/` `*/backtest/full/`（gitignore） |

- `payload/{domain}.{variant}.json` —— 统一信封，build-publish 注入合并页；**入 git 是 carry-forward 的前提**（§4.5：任一域挂掉，读上次成功信封顶上 + 状态条标红）。
- `publish/merged.json` —— HSK 发布指纹（幂等：指纹未变则 NOOP）。
- `data/hsk-resource.json` —— D3：首次成功创建的资源 ID，之后只 update 这一个、失败绝不新建。
- `data/runlog/` —— 数据链路台账（抓取行数/请求数/重试/熔断/覆盖率/缺口）。
