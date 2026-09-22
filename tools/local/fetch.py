"""本地定时拉取编排（对齐 GitHub Actions data-*.yml 链路，零新增依赖）。

用法
----
    # 立即跑一次全链路（个股/指数/ETF/龙虎榜/涨停复盘 + 自检）
    python -m tools.local.fetch --once --data-root /path/to/data

    # 只跑某一步（冒烟/补跑）
    python -m tools.local.fetch --once --only events --data-root /path/to/data

    # 常驻后台，按北京盘后时刻错峰触发（16:30 index / 16:40 stock /
    # 16:55 etf / 17:10 events；自动交易日闸门，非交易日静默跳过）
    python -m tools.local.fetch --daemon --data-root /path/to/data

    # 打印推荐 crontab 行（与 --daemon 二选一，交给系统 cron）
    python -m tools.local.fetch --cron --data-root /path/to/data

说明
----
- 数据以 parquet 文件形式落盘（复用 common/store 契约分区 + _incr 日分片），
  parquet 是唯一真相；内存只是数据服务（serve.py）内的查询缓存。
- 幂等：各 pipeline 自带幂等（同日重跑跳过/合并），重复触发安全。
- 依赖：仅 Python 标准库 + 本仓 common/tools，无需新增 pip 包。

时间表（与数据仓 .github/workflows/data-*.yml 对齐，北京时间）：
    16:30  index  ->  tools.data_pipeline.index
    16:40  stock  ->  tools.data_pipeline.stock_incr
    16:55  etf    ->  tools.data_pipeline.etf_incr
    17:10  events ->  tools.data_pipeline.lhb_incr + zt_pipeline
    17:35  verify ->  tools.data_pipeline.verify
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import subprocess
import sys
import time

BEIJING = _dt.timezone(_dt.timedelta(hours=8))

# 步骤：name -> (模块, 北京 HH:MM)。events 内部严格先 lhb 后 zt
# （zt 自算连板递推依赖当日 raw 已入库 + 历史尾部做追溯）。
PIPELINES: list[tuple[str, str, str]] = [
    ("index",  "tools.data_pipeline.index",      "16:30"),
    ("stock",  "tools.data_pipeline.stock_incr", "16:40"),
    ("etf",    "tools.data_pipeline.etf_incr",   "16:55"),
    ("events", "tools.data_pipeline.lhb_incr",   "17:10"),
    ("verify", "tools.data_pipeline.verify",     "17:35"),
]

# events 组内的内部顺序（lhb 先，zt 后）
_EVENTS_STEPS = [
    ("tools.data_pipeline.lhb_incr", "events.lhb"),
    ("tools.data_pipeline.zt_pipeline", "events.zt"),
]


def _bnow() -> _dt.datetime:
    return _dt.datetime.now(BEIJING)


def _run_module(module: str, *, data_root: str, asof: str | None, offline: bool,
                extra: list[str] | None = None) -> int:
    """subprocess 跑一个 pipeline 模块；返回退出码。"""
    env = dict(os.environ)
    env["QH_DATA_ROOT"] = data_root
    if "PYTHONPATH" in env and env["PYTHONPATH"]:
        env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) \
            + os.pathsep + env["PYTHONPATH"]
    else:
        env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cmd = [sys.executable, "-m", module, "--data-root", data_root]
    # verify 无 --writer 参数；其余 pipeline 都有
    if module != "tools.data_pipeline.verify":
        cmd += ["--writer", "local-fetch@%s" % _bnow().strftime("%Y-%m-%d")]
    if asof:
        cmd += ["--asof", asof]
    if offline and module != "tools.data_pipeline.verify":
        cmd += ["--offline"]
        # 空数据根首次合成时，新鲜度门禁红降级为告警（--allow-stale 仅部分模块有）
        if module in ("tools.data_pipeline.index", "tools.data_pipeline.etf_incr"):
            cmd += ["--allow-stale"]
    if extra:
        cmd += extra
    print(f"\n[fetch] >>> {' '.join(cmd)}", flush=True)
    t0 = time.time()
    r = subprocess.run(cmd, env=env)
    print(f"[fetch] <<< {module} rc={r.returncode} ({time.time()-t0:.1f}s)", flush=True)
    return r.returncode


def run_once(*, data_root: str, asof: str | None, offline: bool,
             only: str | None = None) -> int:
    """立即跑一次全链路（或 --only 指定单步）。events 内部 lhb -> zt 顺序固定。"""
    f = asof or _bnow().date().isoformat()
    if offline and only == "events":
        print("[fetch] events 组不支持 --offline（lhb/zt 无合成模式，需真实网络数据），跳过")
        return 0
    if only:
        if only == "events":
            steps = [("events", m, tag) for m, tag in _EVENTS_STEPS]
        else:
            matches = [p for p in PIPELINES if p[0] == only]
            if not matches:
                print(f"[fetch] 未知步骤 {only!r}，可选：{', '.join(p[0] for p in PIPELINES)}")
                return 2
            steps = [(only, matches[0][1], only)]
    else:
        steps = [(name, module, name) for name, module, _t in PIPELINES]
        steps = [s for s in steps if s[0] != "events"] + [
            ("events", m, tag) for m, tag in _EVENTS_STEPS]

    failures = []
    for name, module, tag in steps:
        if offline and name == "events":
            print(f"[fetch] 跳过 events（--offline 无合成模式）")
            continue
        if not offline:
            from common.calendar import load_calendar
            try:
                cal = load_calendar(root=data_root)
                if not cal.is_trading_day(_dt.date.fromisoformat(f)):
                    print(f"[fetch] {f} 非交易日，跳过 {name}（日历 {cal.min_date}~{cal.max_date}）")
                    continue
            except Exception as e:  # noqa: BLE001
                print(f"[fetch] 交易日历加载失败（放行）：{e}")
        rc = _run_module(module, data_root=data_root, asof=asof, offline=offline)
        if rc != 0:
            failures.append(tag)
            print(f"[fetch] ✗ {tag} 失败 rc={rc}", flush=True)
    if failures:
        print(f"\n[fetch] ✗ 全链路结束，失败步骤：{', '.join(failures)}")
        return 1
    print("\n[fetch] ✓ 全链路通过", flush=True)
    return 0


def run_daemon(*, data_root: str, offline: bool, interval: int = 30) -> int:
    """常驻：每分钟检查一次北京时间，到点触发对应 pipeline（幂等，安全重入）。"""
    print(f"[fetch] daemon 启动：data_root={data_root} offline={offline} "
          f"interval={interval}s（北京时间 {_bnow().strftime('%H:%M:%S')}）", flush=True)
    last_run: dict[str, str] = {}  # step -> 已触发的 asof，防止同分钟重复
    while True:
        now = _bnow()
        hhmm = now.strftime("%H:%M")
        today = now.date().isoformat()
        for name, module, hhmm_t in PIPELINES:
            if hhmm != hhmm_t:
                continue
            if last_run.get(name) == today:
                continue
            last_run[name] = today
            print(f"\n[fetch] ⏰ {today} {hhmm} 触发 {name}", flush=True)
            if name == "events":
                for m, tag in _EVENTS_STEPS:
                    rc = _run_module(m, data_root=data_root, asof=None, offline=offline)
                    if rc != 0:
                        print(f"[fetch] ✗ {tag} 失败 rc={rc}", flush=True)
            else:
                rc = _run_module(module, data_root=data_root, asof=None, offline=offline)
                if rc != 0:
                    print(f"[fetch] ✗ {name} 失败 rc={rc}", flush=True)
            print(f"[fetch] 下一轮等待 {interval}s…", flush=True)
        time.sleep(interval)

def print_crontab(*, data_root: str, offline: bool) -> int:
    """打印与 daemon 等价（略提前 1 分钟）的 crontab 行。"""
    lines = ["# quant-hub 本地定时拉取（与 GitHub Actions data-*.yml 对齐，北京时间；周一~周五）"]
    env = f"QH_DATA_ROOT={data_root}" + (" " + "LOCAL_FETCH_OFFLINE=1" if offline else "")
    log = os.path.join(data_root, "..", "state", "local-fetch.log")
    for name, module, hhmm in PIPELINES:
        h, m = hhmm.split(":")
        if name == "events":
            # events 组内 lhb 先、zt 后（zt 自算依赖 raw + 历史尾部）
            for m2, tag in _EVENTS_STEPS:
                lines.append(
                    f"{int(m)-1} {h} * * 1-5 {env} python3 -m {m2} "
                    f"--data-root {data_root} --writer local-fetch >> {log} 2>&1  # {tag}")
            continue
        writer = "" if module == "tools.data_pipeline.verify" else " --writer local-fetch"
        lines.append(f"{int(m)-1} {h} * * 1-5 {env} python3 -m {module} "
                     f"--data-root {data_root}{writer} >> {log} 2>&1")
    print("\n".join(lines))
    print("\n# 用法：crontab -e 粘贴上述行（events 需 17:10 后东财公布；verify 17:35）")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="本地定时拉取编排（指数/个股/ETF/龙虎榜/涨停复盘）")
    ap.add_argument("--data-root", default=os.environ.get("QH_DATA_ROOT", "data"))
    ap.add_argument("--asof", default=None, help="数据基准日 YYYY-MM-DD（--once 用，默认今天）")
    ap.add_argument("--once", action="store_true", help="立即跑一次全链路")
    ap.add_argument("--daemon", action="store_true", help="常驻按北京盘后时刻触发")
    ap.add_argument("--cron", action="store_true", help="打印推荐 crontab 行（不执行）")
    ap.add_argument("--only", default=None, help="--once 时只跑单步：index/stock/etf/events/verify")
    ap.add_argument("--offline", action="store_true", help="合成数据，不联网（自测）")
    ap.add_argument("--interval", type=int, default=30, help="daemon 轮询间隔秒")
    args = ap.parse_args(argv)

    modes = sum(bool(x) for x in (args.once, args.daemon, args.cron))
    if modes != 1:
        ap.error("必须且只能指定 --once / --daemon / --cron 之一")
    if args.once:
        return run_once(data_root=args.data_root, asof=args.asof,
                        offline=args.offline, only=args.only)
    if args.daemon:
        return run_daemon(data_root=args.data_root, offline=args.offline,
                          interval=args.interval)
    return print_crontab(data_root=args.data_root, offline=args.offline)


if __name__ == "__main__":
    raise SystemExit(main())
