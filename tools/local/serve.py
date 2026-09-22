"""本地一键数据服务（零新增依赖，仅标准库 + 现有 common）。

架构
----
- parquet 文件是唯一真相；本服务启动时把常用事件型表 + 行情表预加载为
  内存 DataFrame（查询缓存），供快速只读查询。
- 定时拉取（fetch.py）落盘后，调用 POST /refresh 或等缓存 TTL 过期即热刷新。
- 仅监听 127.0.0.1（本机访问）。

用法
----
    python -m tools.local.serve --data-root /path/to/data --port 8765

    # 数据新鲜度查询 + 各表行数
    curl -s http://127.0.0.1:8765/health
    # 龙虎榜任意历史日期（净买额/上榜原因/席位）
    curl -s 'http://127.0.0.1:8765/api/events?table=lhb_detail&date=2026-03-10'
    # 涨停梯队/晋级率
    curl -s 'http://127.0.0.1:8765/api/events?table=zt_ladder&date=2026-09-21'
    # 个股日K
    curl -s 'http://127.0.0.1:8765/api/kline?asset=stock&fq=hfq&code=600000&last_n=20'
    # 拉取完成后热刷新缓存
    curl -s -X POST http://127.0.0.1:8765/refresh

响应：JSON。data 为记录数组（datetime -> ISO 字符串，NaN -> null），
meta 携带请求/过滤信息。行数超过 --max-rows 截断并标注 truncated=True。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BEIJING = _dt.timezone(_dt.timedelta(hours=8))

# 默认预加载的事件型表（schema.EVENT_TABLES 键）。zt_daily 全量很大，
# 默认只缓存近 N 日（--events-days），其余表全量。
_EVENT_TABLES = ("lhb_detail", "lhb_seat", "zt_pool", "zt_ladder", "zt_daily")


def _iso(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.isoformat()
    return v


class DataStore:
    """文件(parquet)唯一真相 + 内存查询缓存。"""

    def __init__(self, root: str, *, events_days: int, kline_days: int,
                 kline_tables: tuple[str, ...]) -> None:
        self.root = root
        self.events_days = events_days
        self.kline_days = kline_days
        self.kline_tables = kline_tables
        self._events: dict[str, object] = {}      # 表名 -> DataFrame（全量或近 N 日）
        self._kline: dict[tuple, object] = {}     # (asset,fq,code) -> DataFrame(近 N 日)
        self._meta: dict[str, object] = {}
        self._lock = threading.RLock()
        self.loaded_at: str | None = None
        self.load_duration_s = 0.0
        self.last_error: str | None = None

    # -- 加载 -----------------------------------------------------------
    def load(self, logger=print) -> None:
        t0 = time.time()
        from common.store.events import load_events
        from common.store.reader import load, load_meta
        errs: list[str] = []
        with self._lock:
            ev: dict[str, object] = {}
            for name in _EVENT_TABLES:
                try:
                    kw: dict = {"root": self.root, "strict": False}
                    if name == "zt_daily" and self.events_days:
                        end = _dt.date.today().isoformat()
                        start = (_dt.date.today() - _dt.timedelta(days=self.events_days)).isoformat()
                        kw.update(start=start, end=end)
                    df = load_events(name, **kw)
                    ev[name] = df
                    print(f"[serve] events/{name}: {len(df):,} 行")
                except Exception as e:  # noqa: BLE001
                    errs.append(f"events/{name}: {e}")
                    ev[name] = None
            self._events = ev

            kl: dict[tuple, object] = {}
            for asset, fq in self.kline_tables:
                try:
                    if asset == "index":
                        # 指数目录 market/index/<group>/<code>/（或 <code>/），扫描 code 逐个加载。
                        # 指数不分复权，底层目录是 raw；缓存 key 仍用配置 fq 便于查询
                        # （用户传 fq=hfq 也命中）。
                        import glob as _glob
                        base = os.path.join(self.root, "market", "index")
                        codes = sorted({os.path.basename(p) for p in _glob.glob(os.path.join(base, "*", "*"))
                                        if os.path.isdir(p)} |
                                       {os.path.basename(p) for p in _glob.glob(os.path.join(base, "*"))
                                        if os.path.isdir(p)})
                        for code in codes:
                            try:
                                # 指数不分复权，底层目录是 raw；缓存 key 仍用配置 fq
                                d = load(asset="index", code=code, fq="raw",
                                         last_n=self.kline_days,
                                         root=self.root, strict=False)
                                if d is not None and len(d):
                                    kl[(asset, fq, code)] = d.reset_index(drop=True)
                            except Exception:  # noqa: BLE001
                                pass
                        print(f"[serve] kline index: {len([k for k in kl if k[0]=='index'])} 标的")
                    else:
                        d = load(asset=asset, fq=fq, last_n=self.kline_days,
                                 root=self.root, strict=False)
                        if d is not None and len(d):
                            for code, g in d.groupby("code"):
                                kl[(asset, fq, str(code))] = g.reset_index(drop=True)
                        print(f"[serve] kline {asset}/{fq}: {len(d):,} 行 / "
                              f"{d['code'].nunique()} 标的")
                except Exception as e:  # noqa: BLE001
                    errs.append(f"kline {asset}/{fq}: {e}")
            self._kline = kl

            for name in ("universe",):
                try:
                    self._meta[name] = load_meta(name, root=self.root, pd=None)
                except Exception as e:  # noqa: BLE001
                    errs.append(f"meta/{name}: {e}")

            self.loaded_at = _dt.datetime.now(BEIJING).isoformat(timespec="seconds")
            self.load_duration_s = round(time.time() - t0, 2)
            self.last_error = "; ".join(errs) if errs else None
            if self.last_error:
                print(f"[serve] 部分加载失败：{self.last_error}")

    # -- 查询 -----------------------------------------------------------
    def events(self, table: str, *, dates=None, start=None, end=None, codes=None,
               columns=None, max_rows: int):
        with self._lock:
            df = self._events.get(table)
        if df is None or len(df) == 0:
            return None, 0, f"表 {table} 无数据（未加载/空）"
        df = df.copy()
        if dates:
            from common.store.events import _to_date
            want = {_to_date(x) for x in dates}
            df = df[df["date"].dt.date.isin(want)]
        if start:
            from common.store.events import _to_date
            df = df[df["date"].dt.date >= _to_date(start)]
        if end:
            from common.store.events import _to_date
            df = df[df["date"].dt.date <= _to_date(end)]
        if codes:
            from common.store.reader import normalize_code
            want_c = {normalize_code(c) for c in codes}
            if "code" in df.columns:
                df = df[df["code"].isin(want_c)]
        if columns:
            have = [c for c in columns if c in df.columns]
            if "date" not in have:
                have = ["date"] + have
            df = df[have]
        total = len(df)
        truncated = total > max_rows
        return df.head(max_rows), total, None if not truncated else f"截断到 {max_rows} 行（共 {total}）"

    def kline(self, asset: str, fq: str, code: str, *, start=None, end=None,
              last_n=None, max_rows: int):
        with self._lock:
            df = self._kline.get((asset, fq, code))
        if df is None or len(df) == 0:
            return None, 0, f"无 {asset}/{fq}/{code} 缓存（服务启动时未预加载该标的）"
        df = df.copy()
        if start:
            df = df[df["date"] >= start]
        if end:
            df = df[df["date"] <= end]
        if last_n and len(df) > last_n:
            df = df.tail(last_n)
        total = len(df)
        truncated = total > max_rows
        return df.tail(max_rows), total, None if not truncated else f"截断到 {max_rows} 行（共 {total}）"

    def meta(self, name: str):
        return self._meta.get(name)

    def health(self) -> dict:
        with self._lock:
            ev = {k: (None if v is None else len(v)) for k, v in self._events.items()}
            kl = len(self._kline)
        return {
            "ok": True,
            "root": self.root,
            "loaded_at": self.loaded_at,
            "load_duration_s": self.load_duration_s,
            "last_error": self.last_error,
            "events_rows": ev,
            "kline_cached_assets": kl,
            "server_time": _dt.datetime.now(BEIJING).isoformat(timespec="seconds"),
        }


class Handler(BaseHTTPRequestHandler):
    store: DataStore          # class attr，由 serve 注入
    max_rows: int = 20000     # 覆盖 zt_daily 单日全市场快照（~5500 行）

    # -- helpers --------------------------------------------------------
    def _send(self, status: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=_iso).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bad(self, msg: str) -> None:
        self._send(400, {"ok": False, "error": msg})

    def _frames_to_json(self, df, total: int, truncated: bool, meta: dict) -> dict:
        records = df.to_dict(orient="records") if (df is not None and len(df)) else []
        return {"ok": True, "rows": len(records), "total": total,
                "truncated": truncated, "meta": meta, "data": records}

    def log_message(self, fmt, *args):  # 静默访问日志
        return

    # -- 路由 -----------------------------------------------------------
    def do_GET(self) -> None:
        try:
            u = urlparse(self.path)
            q = parse_qs(u.query)
            path = u.path.rstrip("/") or "/"
            if path == "/health":
                self._send(200, self.store.health())
            elif path == "/api/events":
                self._events(q)
            elif path == "/api/kline":
                self._kline(q)
            elif path == "/api/meta":
                self._meta(q)
            else:
                self._bad(f"未知路径 {path}；可用 /health /api/events /api/kline /api/meta /refresh")
        except Exception as e:  # noqa: BLE001
            self._bad(f"处理异常：{e}")

    def do_POST(self) -> None:
        u = urlparse(self.path)
        if u.path.rstrip("/") == "/refresh":
            t0 = time.time()
            self.store.load()
            self._send(200, {"ok": True, "refreshed": True,
                             "duration_s": round(time.time() - t0, 2),
                             **{k: v for k, v in self.store.health().items()
                                if k in ("loaded_at", "last_error")}})
        else:
            self._bad("仅支持 POST /refresh")

    # -- API 实现 -------------------------------------------------------
    def _events(self, q: dict) -> None:
        table = (q.get("table") or [""])[0]
        if table not in _EVENT_TABLES:
            self._bad(f"table 必须是 {'/'.join(_EVENT_TABLES)}")
        dates = q.get("date") or None
        start = (q.get("start") or [None])[0]
        end = (q.get("end") or [None])[0]
        codes = q.get("code") or None
        cols = q.get("columns") or None
        if cols:
            cols = [c for c in cols[0].split(",") if c]
        df, total, warn = self.store.events(
            table, dates=dates, start=start, end=end, codes=codes,
            columns=cols, max_rows=self.max_rows)
        if df is None:
            self._bad(warn or f"表 {table} 无数据")
            return
        meta = {"table": table, "start": start, "end": end,
                "dates": dates, "codes": codes, "warn": warn}
        self._send(200, self._frames_to_json(df, total, warn is not None, meta))

    def _kline(self, q: dict) -> None:
        asset = (q.get("asset") or [""])[0]
        fq = (q.get("fq") or ["hfq"])[0]
        code = (q.get("code") or [""])[0]
        if not asset or not code:
            self._bad("需要 asset（stock/etf/index）与 code（短代码）")
        start = (q.get("start") or [None])[0]
        end = (q.get("end") or [None])[0]
        last_n = (q.get("last_n") or [None])[0]
        last_n = int(last_n) if last_n else None
        df, total, warn = self.store.kline(asset, fq, code, start=start,
                                           end=end, last_n=last_n,
                                           max_rows=self.max_rows)
        if df is None:
            self._bad(warn or "无数据")
            return
        meta = {"asset": asset, "fq": fq, "code": code, "start": start,
                "end": end, "last_n": last_n, "warn": warn}
        self._send(200, self._frames_to_json(df, total, warn is not None, meta))

    def _meta(self, q: dict) -> None:
        name = (q.get("name") or [""])[0]
        df = self.store.meta(name)
        if df is None:
            self._bad(f"meta {name!r} 未加载")
            return
        self._send(200, self._frames_to_json(df, len(df), False, {"name": name}))


def serve(*, data_root: str, host: str, port: int, events_days: int,
          kline_days: int, kline_assets: tuple[str, ...], max_rows: int) -> int:
    store = DataStore(data_root, events_days=events_days, kline_days=kline_days,
                      kline_tables=kline_assets)
    print(f"[serve] 预加载数据 {data_root} …")
    store.load()
    Handler.store = store
    Handler.max_rows = max_rows
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"[serve] ✓ 数据服务已启动 http://{host}:{port}/health")
    print(f"        事件型表缓存 {store.events_days} 日（zt_daily） | 行情缓存 {store.kline_days} 日")
    print("        POST /refresh 在 fetch.py 拉取完成后热刷新缓存")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[serve] 停止")
    finally:
        httpd.server_close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="本地数据服务（parquet 文件唯一真相 + 内存查询缓存）")
    ap.add_argument("--data-root", default=os.environ.get("QH_DATA_ROOT", "data"))
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--events-days", type=int, default=60,
                    help="zt_daily 缓存近 N 日（其余事件表全量）")
    ap.add_argument("--kline-days", type=int, default=250,
                    help="行情缓存近 N 个交易日")
    ap.add_argument("--kline-assets", default="stock:hfq,etf:hfq",
                    help="预加载行情 asset:fq 列表，逗号分隔")
    ap.add_argument("--max-rows", type=int, default=20000,
                    help="单次响应最大行数（zt_daily 单日为全市场快照 ~5500 行）")
    args = ap.parse_args(argv)

    kline_tables: list[tuple[str, str]] = []
    for pair in args.kline_assets.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" in pair:
            a, f = pair.split(":", 1)
        else:
            a, f = pair, "hfq"
        kline_tables.append((a.strip(), f.strip()))

    return serve(data_root=args.data_root, host=args.host, port=args.port,
                 events_days=args.events_days, kline_days=args.kline_days,
                 kline_assets=tuple(kline_tables), max_rows=args.max_rows)


if __name__ == "__main__":
    raise SystemExit(main())
