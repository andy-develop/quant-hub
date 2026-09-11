# 个性化因子选股 · 智能选股引擎

基于多因子模型的 A 股选股产品，数据每日自动更新，静态页面自动部署。

## 项目结构

```
├── .github/workflows/
│   └── daily-update.yml     # GitHub Actions 每日工作流
├── data/                     # 数据文件（Git 管理，每日增量更新）
│   ├── stocks.json           # 全 A 股列表 [code, name]
│   ├── prices.json           # 历史日线收盘价（增量追加）
│   └── factors.json          # 计算出的技术因子
├── scripts/
│   ├── fetch_stocks.py       # 拉取全 A 股列表
│   ├── fetch_prices.py       # 增量拉取日线收盘价
│   ├── compute_factors.py    # 计算动量/波动率等因子
│   └── build_html.py         # 模板 + 数据 → index.html
├── templates/
│   └── index_template.html   # HTML 模板（含占位符）
├── assets/
│   └── echarts.min.js        # ECharts 本地依赖
├── index.html                # 构建产物（每日生成）
└── requirements.txt
```

## 本地运行

```bash
# 1. 拉取股票列表（首次或偶尔运行）
python3 scripts/fetch_stocks.py

# 2. 增量拉取股价（每日运行，自动跳过已有数据）
python3 scripts/fetch_prices.py --workers 12

# 3. 计算因子
python3 scripts/compute_factors.py

# 4. 生成 index.html
python3 scripts/build_html.py

# 5. 本地预览（浏览器打开 index.html）
```

### 调试参数

```bash
# 只拉取前 100 只股票的价格（快速测试）
python3 scripts/fetch_prices.py --limit 100 --workers 4
```

## 数据说明

| 文件 | 更新频率 | 说明 |
|------|---------|------|
| stocks.json | 周级/按需 | 全 A 股代码+名称，约 5180 只 |
| prices.json | 每日 | 每只股票最近 120 个交易日收盘价，增量追加 |
| factors.json | 每日 | 20日动量、60日动量、20日年化波动率 |

### 增量更新策略

- `fetch_prices.py` 读取已有 `prices.json`，每只股票只拉取**最后交易日之后**的新数据
- 无新数据的股票不重复请求
- 数据不足 21 个交易日的股票在因子计算中自动跳过

### 因子映射

7 维画像中，以下维度使用真实计算值：

| 维度 | 因子 | 归一化方式 |
|------|------|-----------|
| 动量 | 20日收益率 | `50 + mom_20 × 160`（截断到 5-98） |
| 波动 | 20日年化波动率 | `95 - vol_20 × 120`（低波动得分高） |

其余 5 维（估值、质量、成长、资金、机构）当前为演示数据，可后续接入财务/资金流接口扩展。

## GitHub Actions 自动部署

### 触发方式

- **定时**：周一至周五北京时间 18:00（A股收盘后）自动运行
- **手动**：GitHub 仓库 Actions 页面点击 "Run workflow"

### 工作流步骤

1. 检出代码
2. 更新股票列表
3. 增量拉取股价（12 并发）
4. 计算技术因子
5. 生成 index.html
6. 提交数据文件回仓库（prices.json 等增量更新持久化）
7. 安装 hsk-cli
8. 推送 index.html 到文件托管

### 必需配置

在 GitHub 仓库 **Settings → Secrets and variables → Actions** 中添加：

| Secret | 说明 |
|--------|------|
| `HSK_API_KEY` | 花生壳文件托管 API Key（ph_key_xxx） |

### 首次部署

1. 将本项目推送到 GitHub 仓库
2. 配置 `HSK_API_KEY` secret
3. 手动触发一次 workflow（或等待定时触发）
4. 工作流完成后，在日志中找到 `public_url` 即为访问地址

## hsk-cli 配置

```bash
# 安装
npm install -g @aweray/hsk-cli
hsk-cli update

# 配置 API Key
mkdir -p ~/.hsk
echo '{"api_key":"你的key","scene":"file_hosting"}' > ~/.hsk/api_key.json

# 手动推送
hsk-cli +host index.html --entry-file index.html --format json
```

## 免责声明

本产品仅供策略验证与原型演示，所有因子数据基于公开行情计算，不构成投资建议。
