# -*- coding: utf-8 -*-
"""
Argus 助手（手机版网页应用）

功能：
  - 查价格：从 Argus 官方接口拉取煤炭价格（API 2、API 4 等 20 项），带涨跌
  - 搜新闻：尝试 Argus 官方新闻接口（未开通时给出提示和网页入口）
  - AI 助手：跳转 Argus 官方 Ask Argus（网页版 / 手机 App）

启动：
  python argus_assistant.py
  浏览器打开 http://127.0.0.1:8768

访问无需密码；服务仅监听本机地址。
"""

import csv
import json
import os
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from argus_coal_fetch import fetch_custom, load_codes, prev_date_str
from argus_reports import get_price_pages, get_report, list_reports, render_price_page
from argus_ws_test import (SCRIPT_DIR, TNS, ArgusError, authenticate,
                           load_config, soap_call)

PORT = int(os.environ.get("ARGUS_PORT", "8768"))
WEB_DIR = SCRIPT_DIR / "web"
CATALOG_FILE = SCRIPT_DIR / "price_catalog.csv"
WORKSPACE_FILE = SCRIPT_DIR / "workspace.json"
CONFIG_FILE = Path(os.environ.get("ARGUS_CONFIG", str(SCRIPT_DIR / "config.ini")))
CACHE_TTL = 30 * 60  # 价格缓存 30 分钟
CACHE_VER = 2
CACHE_DIR = SCRIPT_DIR / "argus_output" / "煤价_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ---------------- 启动时准备 ----------------
WEB_DIR.mkdir(exist_ok=True)

CODES = load_codes(SCRIPT_DIR / "codes.csv")


def load_catalog():
    """读取价格目录（所有可勾选的价格）"""
    items = []
    if CATALOG_FILE.exists():
        with open(CATALOG_FILE, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                items.append({
                    "key": r["KEY"].strip(),
                    "code": r["CODE_ID"].strip(),
                    "pt": r["PRICETYPE_ID"].strip(),
                    "ts": r["TIMESTAMP_ID"].strip(),
                    "name": r["名称"].strip(),
                    "freq": r["类型"].strip(),
                    "cat": r["类别"].strip(),
                    "quote_id": r["QUOTE_ID"].strip(),
                    "item_id": r["PRICEREPORTITEMID"].strip(),
                    "fwd": r["FORWARD_PERIOD"].strip(),
                    "year": r["FORWARD_YEAR"].strip(),
                    "unit1": r["UNIT_ID1"].strip(),
                    "unit2": r["UNIT_ID2"].strip(),
                })
    items.append({
        "key": "m42", "code": "M42", "pt": "", "ts": "",
        "name": "M42（印尼 4,200 GAR FOB，周）",
        "freq": "周", "cat": "煤炭",
        "quote_id": "", "item_id": "", "fwd": "", "year": "",
        "unit1": "", "unit2": "",
    })
    return items


CATALOG = load_catalog()
CATALOG_BY_KEY = {c["key"]: c for c in CATALOG}

M42_SERIES_FILE = SCRIPT_DIR / "argus_output" / "mcc_m42_weekly_series.csv"
m42_series_cache = None
m42_series_mtime = 0.0
m42_series_lock = threading.Lock()


def load_m42_series():
    """读取本地 M42 周度序列，返回 {date: float}（date 为 ISO 格式）。"""
    global m42_series_cache, m42_series_mtime
    with m42_series_lock:
        try:
            mtime = M42_SERIES_FILE.stat().st_mtime if M42_SERIES_FILE.exists() else -1
        except OSError:
            mtime = -1
        if m42_series_cache is not None and mtime == m42_series_mtime:
            return m42_series_cache
        data = {}
        if M42_SERIES_FILE.exists():
            with open(M42_SERIES_FILE, encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    d = (r.get("date") or "").strip()
                    v = (r.get("m42_weekly") or "").strip()
                    if d and v:
                        try:
                            data[d] = float(v)
                        except ValueError:
                            pass
        m42_series_cache = data
        m42_series_mtime = mtime
        return data

price_cache = {}
price_lock = threading.Lock()
history_cache = {}
history_lock = threading.Lock()
yoy_cache = {}
yoy_lock = threading.Lock()


# ---------------- 业务函数 ----------------
def get_prices(date_str, include_change=True):
    key = (date_str, include_change)
    now = time.time()
    with price_lock:
        if key in price_cache and now - price_cache[key][0] < CACHE_TTL:
            return price_cache[key][1]

    # 磁盘缓存（重启后仍然秒开）
    cache_file = CACHE_DIR / f"{date_str}.json"
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if (cached.get("ver") == CACHE_VER
                    and now - cached.get("saved_at", 0) < CACHE_TTL):
                results = cached["rows"]
                payload = {
                    "date": cached.get("date", date_str),
                    "requested": date_str,
                    "fallback": cached.get("fallback_from"),
                    "rows": results,
                }
                with price_lock:
                    price_cache[key] = (time.time(), payload)
                return payload
        except Exception:
            pass

        username, password = load_config(CONFIG_FILE)
    _, token = authenticate(username, password)
    values = fetch_custom(token, date_str, CODES)

    # 如果当天没有数据（周末/休市），自动回退到最近有数据的交易日
    actual_date = date_str
    if not any(v not in (None, "") for v in values.values()):
        probe = date_str
        for _ in range(7):
            probe = prev_date_str(probe)
            v2 = fetch_custom(token, probe, CODES)
            if any(x not in (None, "") for x in v2.values()):
                values, actual_date = v2, probe
                break

    prev_values = {}
    if include_change:
        prev_date = prev_date_str(actual_date)
        prev_values = fetch_custom(token, prev_date, CODES)

    results = []
    for c in CODES:
        value = values.get(c["code"])
        prev = None
        if include_change and c["freq"] == "日" and value is not None:
            prev = prev_values.get(c["code"])
        change = ""
        if value is not None and prev not in (None, ""):
            try:
                change = f"{float(value) - float(prev):+.2f}"
            except ValueError:
                change = ""
        results.append({
            "name": c["name"],
            "value": value if value not in (None, "") else None,
            "prev": prev,
            "change": change,
            "freq": "日" if c["freq"] == "日" else "周",
        })

    try:
        cache_file.write_text(json.dumps(
            {"ver": CACHE_VER,
             "saved_at": time.time(),
             "date": actual_date,
             "fallback_from": date_str if actual_date != date_str else None,
             "rows": results}, ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass
    payload = {
        "date": actual_date,
        "requested": date_str,
        "fallback": date_str if actual_date != date_str else None,
        "rows": results,
    }
    with price_lock:
        price_cache[key] = (time.time(), payload)
    return payload


def fetch_custom_keyed(token, date, items):
    """只拉指定价格，返回 {key: value}（key = 代码|价格类型|时间戳）"""
    item_xml = "".join(f"""<PriceReportItem>
        <QuoteId>{c['quote_id']}</QuoteId>
        <PriceTypeId>{c['pt']}</PriceTypeId>
        <PriceReportItemID>{c['item_id']}</PriceReportItemID>
        <ForwardPeriod>{c['fwd']}</ForwardPeriod>
        <ForwardYear>{c['year']}</ForwardYear>
        <CurrencyUnit>{c['unit1']}</CurrencyUnit>
        <MeasureUnit>{c['unit2']}</MeasureUnit>
        <TimestampId>{c['ts']}</TimestampId>
      </PriceReportItem>""" for c in items)
    body = f"""<GetCustomReport xmlns="{TNS}">
      <authToken>{token}</authToken>
      <pars>
        <Periodicity>Daily</Periodicity>
        <StartDate>{date}</StartDate>
        <EndDate>{date}</EndDate>
        <ItemList>{item_xml}</ItemList>
      </pars>
    </GetCustomReport>"""
    root = soap_call("GetCustomReport", body)
    values = {}
    for elem in root.iter():
        if not isinstance(elem.tag, str):
            continue
        if elem.tag.rsplit("}", 1)[-1] == "V_REPO":
            d = {}
            for c in elem:
                if isinstance(c.tag, str):
                    d[c.tag.rsplit("}", 1)[-1]] = (c.text or "").strip()
            code = d.get("CODE_ID", "")
            pt = d.get("PRICETYPE_ID", "")
            ts = d.get("TIMESTAMP_ID", "")
            if code:
                values[f"{code}|{pt}|{ts}"] = d.get("VALUE", "")
    return values


def fetch_history(token, start, end, items):
    """拉取一段日期区间的历史，返回 {key: {date: value}}

    日度价格直接用目录参数；周度价格按每周五的周编号展开，
    所有条目合并到一次 GetCustomReport 请求里。
    """
    by_key = {}
    daily = [c for c in items if c["freq"] == "日"]
    weekly = [c for c in items if c["freq"] != "日"]

    # 展开所有条目（日度 1 条；周度按每周五一条，fwd=周编号）
    item_list = []
    if daily:
        item_list.extend(daily)
    if weekly:
        ds = datetime.strptime(start, "%Y-%m-%d").date()
        de = datetime.strptime(end, "%Y-%m-%d").date()
        friday = ds + timedelta(days=(4 - ds.weekday()) % 7)
        while friday <= de:
            for c in weekly:
                w = dict(c)
                w["fwd"] = str(friday.isocalendar()[1])
                w["year"] = str(friday.year)
                item_list.append(w)
            friday += timedelta(days=7)

    if not item_list:
        return by_key

    item_xml = "".join(f"""<PriceReportItem>
        <QuoteId>{c['quote_id']}</QuoteId>
        <PriceTypeId>{c['pt']}</PriceTypeId>
        <PriceReportItemID>{c['item_id']}</PriceReportItemID>
        <ForwardPeriod>{c['fwd']}</ForwardPeriod>
        <ForwardYear>{c['year']}</ForwardYear>
        <CurrencyUnit>{c['unit1']}</CurrencyUnit>
        <MeasureUnit>{c['unit2']}</MeasureUnit>
        <TimestampId>{c['ts']}</TimestampId>
      </PriceReportItem>""" for c in item_list)
    body = f"""<GetCustomReport xmlns="{TNS}">
      <authToken>{token}</authToken>
      <pars>
        <Periodicity>Daily</Periodicity>
        <StartDate>{start}</StartDate>
        <EndDate>{end}</EndDate>
        <ItemList>{item_xml}</ItemList>
      </pars>
    </GetCustomReport>"""
    root = soap_call("GetCustomReport", body)
    for elem in root.iter():
        if not isinstance(elem.tag, str):
            continue
        if elem.tag.rsplit("}", 1)[-1] == "V_REPO":
            d = {}
            for c in elem:
                if isinstance(c.tag, str):
                    d[c.tag.rsplit("}", 1)[-1]] = (c.text or "").strip()
            code, pt, ts = d.get("CODE_ID", ""), d.get("PRICETYPE_ID", ""), d.get("TIMESTAMP_ID", "")
            date = (d.get("PUBLICATION_DATE", "") or "")[:10]
            if code and date and d.get("VALUE", "") not in ("", None):
                by_key.setdefault(f"{code}|{pt}|{ts}", {})[date] = d.get("VALUE", "")
    return by_key


def board_items_for_date(date_str, items):
    """为价格台准备查询参数：周度价格使用目标日所在的最近发布周。"""
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
        friday = target - timedelta(days=(target.weekday() - 4) % 7)
    except ValueError:
        return items
    prepared = []
    for item in items:
        if item["freq"] == "日":
            prepared.append(item)
            continue
        current = dict(item)
        current["fwd"] = str(friday.isocalendar()[1])
        current["year"] = str(friday.year)
        prepared.append(current)
    return prepared


def fetch_board_values(token, date_str, items):
    """查询价格台：日度取目标日，周度取该周周五发布日。"""
    daily = [item for item in items if item["freq"] == "日"]
    weekly = [item for item in items if item["freq"] != "日"]
    values = {}
    if daily:
        values.update(fetch_custom_keyed(token, date_str, daily))
    if weekly:
        try:
            target = datetime.strptime(date_str, "%Y-%m-%d").date()
            friday = target - timedelta(days=(target.weekday() - 4) % 7)
            friday_str = friday.isoformat()
        except ValueError:
            friday_str = date_str
        values.update(fetch_custom_keyed(
            token, friday_str, board_items_for_date(date_str, weekly)))
    return values


def get_board(date_str, keys):
    """价格台：任意勾选价格的最新值+涨跌"""
    items = [CATALOG_BY_KEY[k] for k in keys if k in CATALOG_BY_KEY and k != "m42"]
    if not items and "m42" not in keys:
        return {"date": date_str, "rows": [], "fallback": None}
    username, password = load_config(CONFIG_FILE)
    _, token = authenticate(username, password)
    values = fetch_board_values(token, date_str, items)
    actual_date = date_str
    if not any(v not in (None, "") for v in values.values()):
        probe = date_str
        for _ in range(7):
            probe = prev_date_str(probe)
            v2 = fetch_board_values(token, probe, items)
            if any(x not in (None, "") for x in v2.values()):
                values, actual_date = v2, probe
                break
    prev_values = {}
    prev_date = prev_date_str(actual_date)
    prev_values = fetch_board_values(token, prev_date, items)
    rows = []
    for k in keys:
        if k == "m42":
            m42d = load_m42_series()
            dates = sorted(d for d in m42d if d <= date_str)
            if not dates:
                rows.append({"key": "m42", "name": "M42（印尼 4,200 GAR FOB，周）",
                             "freq": "周", "value": None, "prev": None, "change": ""})
                continue
            cur = dates[-1]
            prev = dates[-2] if len(dates) >= 2 else None
            v = m42d[cur]
            p = m42d[prev] if prev else None
            change = f"{v - p:+.2f}" if p is not None else ""
            rows.append({
                "key": "m42", "name": "M42（印尼 4,200 GAR FOB，周）", "freq": "周",
                "value": str(v), "prev": str(p) if p is not None else None,
                "change": change,
            })
            continue
        c = CATALOG_BY_KEY.get(k)
        if not c:
            continue
        value = values.get(k)
        prev = prev_values.get(k) if c["freq"] == "日" else None
        change = ""
        if value not in (None, "") and prev not in (None, ""):
            try:
                change = f"{float(value) - float(prev):+.2f}"
            except ValueError:
                change = ""
        rows.append({
            "key": k, "name": c["name"], "freq": c["freq"],
            "value": value if value not in (None, "") else None,
            "prev": prev, "change": change,
        })
    return {"date": actual_date, "rows": rows,
            "fallback": date_str if actual_date != date_str else None}


def get_yoy(key, date_str):
    """同比：取去年同日（日度）或去年同周（周度）的价格"""
    c = CATALOG_BY_KEY.get(key)
    if not c:
        return {"ok": False, "error": "未知价格"}
    cache_key = f"{key}|{date_str}"
    with yoy_lock:
        hit = yoy_cache.get(cache_key)
        if hit:
            return hit[1]
    try:
        if key == "m42":
            m42d = load_m42_series()
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
            ly = d - timedelta(days=364)
            friday = ly + timedelta(days=(4 - ly.weekday()) % 7)
            value = m42d.get(friday.isoformat())
            yoy_date = friday.isoformat()
            if value is None:
                cand = sorted(x for x in m42d if x <= friday.isoformat())
                if cand:
                    yoy_date = cand[-1]
                    value = m42d[yoy_date]
            payload = {"ok": True, "date": date_str, "yoy_date": yoy_date,
                       "value": value if value is not None else None}
            with yoy_lock:
                yoy_cache[cache_key] = (time.time(), payload)
            return payload
        username, password = load_config(CONFIG_FILE)
        _, token = authenticate(username, password)
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        if c["freq"] == "日":
            target = d - timedelta(days=365)
            value = None
            for _ in range(4):  # 若去年同日为周末，往前找
                vals = fetch_custom_keyed(token, target.isoformat(), [c])
                value = vals.get(key)
                if value not in (None, ""):
                    break
                target -= timedelta(days=1)
            payload = {"ok": True, "date": date_str, "yoy_date": target.isoformat(),
                       "value": value if value not in (None, "") else None}
        else:
            ly = d - timedelta(days=364)  # 去年同一周（同星期几）
            li = ly.isocalendar()
            w = dict(c)
            w["fwd"] = str(li[1])
            w["year"] = str(li[0])
            friday = ly + timedelta(days=(4 - ly.weekday()) % 7)
            vals = fetch_custom_keyed(token, friday.isoformat(), [w])
            value = vals.get(key)
            payload = {"ok": True, "date": date_str, "yoy_date": friday.isoformat(),
                       "value": value if value not in (None, "") else None}
        with yoy_lock:
            yoy_cache[cache_key] = (time.time(), payload)
        return payload
    except ArgusError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": repr(e)}


def calc_pricing(index, laycan, x, premium):
    """长协定价：指数均价 + premium = FOB 实际价。

    index: ici4 / m42 / avg
    laycan: 装货窗口起始日；取严格早于该日的最近 x 个周五发布的周指数。
    """
    laycan_date = datetime.strptime(laycan, "%Y-%m-%d").date()
    x = int(x)
    premium = float(premium)
    if x < 1 or x > 4:
        raise ValueError("x 需在 1-4 之间")
    if index not in ("ici4", "m42", "avg"):
        raise ValueError("index 需为 ici4/m42/avg")

    f = laycan_date - timedelta(days=1)
    while f.weekday() != 4:
        f -= timedelta(days=1)
    fridays = []
    for _ in range(x):
        fridays.append(f.isoformat())
        f -= timedelta(days=7)
    fridays = fridays[::-1]

    m42d = load_m42_series()
    m42_vals = {fd: m42d[fd] for fd in fridays if fd in m42d}

    ici_vals = {}
    if index in ("ici4", "avg"):
        ici4 = CATALOG_BY_KEY.get("5483|4|0")
        if not ici4:
            raise ValueError("目录中缺少 ICI 4 周度条目")
        username, password = load_config(CONFIG_FILE)
        _, token = authenticate(username, password)
        for fd in fridays:
            w = dict(ici4)
            fd_date = datetime.strptime(fd, "%Y-%m-%d").date()
            w["fwd"] = str(fd_date.isocalendar()[1])
            w["year"] = str(fd_date.year)
            vals = fetch_custom_keyed(token, fd, [w])
            v = vals.get("5483|4|0")
            if v not in (None, ""):
                try:
                    ici_vals[fd] = float(v)
                except ValueError:
                    pass

    def avg_of(series):
        return sum(series) / len(series) if series else None

    m42_base = avg_of(list(m42_vals.values()))
    ici_base = avg_of(list(ici_vals.values()))
    if index == "m42":
        if m42_base is None:
            raise ValueError(f"laycan {laycan} 之前不足 {x} 期 M42 数据")
        base = m42_base
    elif index == "ici4":
        if ici_base is None:
            raise ValueError(f"laycan {laycan} 之前不足 {x} 期 ICI-4 数据")
        base = ici_base
    else:
        missing = []
        if m42_base is None:
            missing.append("M42")
        if ici_base is None:
            missing.append("ICI-4")
        if missing:
            raise ValueError("缺少指数数据: " + "、".join(missing))
        base = (m42_base + ici_base) / 2

    return {
        "ok": True,
        "index": index,
        "laycan": laycan,
        "x": x,
        "premium": premium,
        "fridays": fridays,
        "m42": {
            "dates": list(m42_vals.keys()),
            "values": list(m42_vals.values()),
        },
        "ici4": {
            "dates": list(ici_vals.keys()),
            "values": list(ici_vals.values()),
        },
        "m42_base": m42_base,
        "ici4_base": ici_base,
        "base_index": round(base, 3),
        "fob_price": round(base + premium, 3),
    }


# ---------------- HTTP 服务 ----------------
class Handler(BaseHTTPRequestHandler):
    server_version = "ArgusAssistant/1.0"

    def log_message(self, fmt, *args):
        pass  # 静默日志，避免刷屏

    def _send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, html, status=200):
        data = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _require_login(self):
        # 本地价格台按用户要求不设访问密码。
        return False

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._send_html(INDEX_HTML)
            return

        if path == "/api/prices":
            if self._require_login():
                return self._send_json({"error": "login"}, 401)
            qs = urllib.parse.parse_qs(parsed.query)
            date_str = qs.get("date", [datetime.now().strftime("%Y-%m-%d")])[0]
            try:
                rows = get_prices(date_str, include_change=True)
                fallback = rows.get("_fallback_from")
                self._send_json({
                    "ok": True,
                    "date": rows["date"],
                    "requested": date_str,
                    "fallback": fallback,
                    "rows": rows["rows"],
                })
            except ArgusError as e:
                self._send_json({"ok": False, "error": str(e)})
            except Exception as e:
                self._send_json({"ok": False, "error": repr(e)})
            return

        if path == "/api/status":
            if self._require_login():
                return self._send_json({"error": "login"}, 401)
            self._send_json({
                "price_count": len(CODES),
                "catalog_count": len(CATALOG),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            return

        if path == "/api/catalog":
            if self._require_login():
                return self._send_json({"error": "login"}, 401)
            # 煤炭排前面，其余按名称排序
            items = sorted(CATALOG, key=lambda c: (0 if c["cat"] == "煤炭" else 1, c["name"]))
            self._send_json({"ok": True, "items": items})
            return

        if path == "/api/workspace":
            if self._require_login():
                return self._send_json({"error": "login"}, 401)
            data = {"selected": [], "diff": None}
            if WORKSPACE_FILE.exists():
                try:
                    data = json.loads(WORKSPACE_FILE.read_text(encoding="utf-8"))
                except Exception:
                    pass
            self._send_json(data)
            return

        if path == "/api/board":
            if self._require_login():
                return self._send_json({"error": "login"}, 401)
            qs = urllib.parse.parse_qs(parsed.query)
            date_str = qs.get("date", [datetime.now().strftime("%Y-%m-%d")])[0]
            keys = [k for k in qs.get("keys", [""])[0].split(",") if k]
            try:
                self._send_json(get_board(date_str, keys))
            except ArgusError as e:
                self._send_json({"ok": False, "error": str(e)})
            except Exception as e:
                self._send_json({"ok": False, "error": repr(e)})
            return

        if path == "/api/history":
            if self._require_login():
                return self._send_json({"error": "login"}, 401)
            qs = urllib.parse.parse_qs(parsed.query)
            keys = [k for k in qs.get("keys", [""])[0].split(",") if k]
            start = qs.get("start", [""])[0]
            end = qs.get("end", [""])[0]
            if not start or not end or not keys:
                return self._send_json({"ok": False, "error": "缺少参数 start/end/keys"})
            try:
                cache_key = "|".join(sorted(keys)) + f"|{start}|{end}"
                with history_lock:
                    hit = history_cache.get(cache_key)
                    if hit and time.time() - hit[0] < 600:
                        return self._send_json(hit[1])
                username, password = load_config(CONFIG_FILE)
                _, token = authenticate(username, password)
                items = [CATALOG_BY_KEY[k] for k in keys if k in CATALOG_BY_KEY and k != "m42"]
                by_key = fetch_history(token, start, end, items)
                if "m42" in keys:
                    m42d = load_m42_series()
                    by_key["m42"] = {
                        d: str(v) for d, v in m42d.items() if start <= d <= end
                    }
                by_key = {
                    key: {
                        report_date: value
                        for report_date, value in values.items()
                        if start <= report_date <= end
                    }
                    for key, values in by_key.items()
                }
                dates = sorted({d for m in by_key.values() for d in m})
                payload = {"ok": True, "dates": dates, "series": by_key}
                with history_lock:
                    history_cache[cache_key] = (time.time(), payload)
                self._send_json(payload)
            except ArgusError as e:
                self._send_json({"ok": False, "error": str(e)})
            except Exception as e:
                self._send_json({"ok": False, "error": repr(e)})
            return

        if path == "/api/reports":
            if self._require_login():
                return self._send_json({"error": "login"}, 401)
            try:
                reports = list_reports()
                self._send_json({"ok": True, "reports": reports})
            except Exception as e:
                self._send_json({"ok": False, "error": repr(e)})
            return

        if path == "/api/report":
            if self._require_login():
                return self._send_json({"error": "login"}, 401)
            qs = urllib.parse.parse_qs(parsed.query)
            date = qs.get("date", [""])[0]
            kind = qs.get("kind", [""])[0]
            if not date or not kind:
                return self._send_json({"ok": False, "error": "缺少 date/kind"})
            try:
                force = qs.get("force", ["0"])[0] in ("1", "true", "True")
                self._send_json(get_report(date, kind, force=force))
            except Exception as e:
                self._send_json({"ok": False, "error": repr(e)})
            return

        if path == "/api/price-pages":
            if self._require_login():
                return self._send_json({"error": "login"}, 401)
            qs = urllib.parse.parse_qs(parsed.query)
            date = qs.get("date", [""])[0]
            kind = qs.get("kind", [""])[0]
            if not date or not kind:
                return self._send_json({"ok": False, "error": "缺少 date/kind"})
            try:
                pages = get_price_pages(date, kind)
                self._send_json({"ok": True, "date": date, "kind": kind,
                                 "pages": pages})
            except Exception as e:
                self._send_json({"ok": False, "error": repr(e)})
            return

        if path == "/api/price-image":
            if self._require_login():
                return self._send_json({"error": "login"}, 401)
            qs = urllib.parse.parse_qs(parsed.query)
            date = qs.get("date", [""])[0]
            kind = qs.get("kind", [""])[0]
            page = qs.get("page", [""])[0]
            if not date or not kind or not page:
                return self._send_json({"ok": False, "error": "缺少 date/kind/page"})
            try:
                crop = qs.get("crop", ["0"])[0] in ("1", "true", "True")
                box = None
                if qs.get("box"):
                    try:
                        box = [float(x) for x in qs["box"][0].split(",")]
                    except Exception:
                        box = None
                fp = render_price_page(date, kind, int(page), crop=crop, box=box)
                data = fp.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._send_json({"ok": False, "error": repr(e)})
            return

        if path == "/api/yoy":
            if self._require_login():
                return self._send_json({"error": "login"}, 401)
            qs = urllib.parse.parse_qs(parsed.query)
            key = qs.get("key", [""])[0]
            date = qs.get("date", [""])[0]
            if not key or not date:
                return self._send_json({"ok": False, "error": "缺少 key/date"})
            self._send_json(get_yoy(key, date))
            return

        if path == "/api/calc":
            if self._require_login():
                return self._send_json({"error": "login"}, 401)
            qs = urllib.parse.parse_qs(parsed.query)
            index = qs.get("index", [""])[0]
            laycan = qs.get("laycan", [""])[0]
            x = qs.get("x", [""])[0]
            premium = qs.get("premium", ["0"])[0]
            if not index or not laycan or not x:
                return self._send_json({"ok": False, "error": "缺少 index/laycan/x"})
            try:
                self._send_json(calc_pricing(index, laycan, x, premium))
            except ArgusError as e:
                self._send_json({"ok": False, "error": str(e)})
            except Exception as e:
                self._send_json({"ok": False, "error": repr(e)})
            return

        if path == "/logout":
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie",
                             "argus_sess=; Max-Age=0; Path=/; HttpOnly")
            self.end_headers()
            return

        self._send_html("<h3>404</h3>", 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/workspace":
            if self._require_login():
                return self._send_json({"error": "login"}, 401)
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            try:
                data = json.loads(body or "{}")
                selected = [k for k in data.get("selected", []) if k in CATALOG_BY_KEY]
                diff = data.get("diff")
                if diff and not isinstance(diff, dict):
                    diff = None
                payload = {"selected": selected, "diff": diff}
                WORKSPACE_FILE.write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"ok": False, "error": repr(e)})
            return
        if parsed.path == "/login":
            self._send_json({"ok": True, "message": "本地价格台无需密码"})
            return
        self._send_json({"error": "not found"}, 404)


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Argus 助手 - 登录</title>
<style>
body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;margin:0;background:#f2e9d8;color:#463525;
display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#fbf5e8;border:1px solid #cfbf9d;border-radius:16px;padding:32px 24px;width:min(90vw,360px);text-align:center}
h1{font-size:22px;margin:0 0 6px}
p{color:#8a7355;font-size:13px;margin:0 0 20px}
input{width:100%;box-sizing:border-box;padding:14px;border-radius:10px;border:1px solid #cfbf9d;background:#fffaf0;
color:#463525;font-size:16px;text-align:center}
button{width:100%;margin-top:14px;padding:14px;border:none;border-radius:10px;background:#d05a26;color:#fff;
font-size:16px;font-weight:600}
#msg{color:#d64541;font-size:13px;margin-top:12px;min-height:18px}
.hint{margin-top:18px;font-size:12px;color:#8a7355}
</style>
</head>
<body>
<div class="card">
<h1>Argus 助手</h1>
<p>查煤炭价格 · 搜新闻 · AI 助手</p>
<input id="pw" type="password" placeholder="访问密码" autocomplete="current-password">
<button onclick="login()">进入</button>
<div id="msg"></div>
<div class="hint">密码在电脑上 argus_api 文件夹的 app_password.txt 里</div>
</div>
<script>
async function login(){
  const pw=document.getElementById('pw').value;
  const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({password:pw})});
  if(r.ok){location.href='/';}else{document.getElementById('msg').textContent='密码错误，请重试';}
}
document.getElementById('pw').addEventListener('keydown',e=>{if(e.key==='Enter')login();});
</script>
</body>
</html>"""


def load_index_html():
    idx = WEB_DIR / "index.html"
    if idx.exists():
        return idx.read_text(encoding="utf-8")
    return "<h3>缺少 web/index.html</h3>"


INDEX_HTML = load_index_html()


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("")
    print("Argus 助手已启动：")
    print("  本机访问：http://127.0.0.1:" + str(PORT))
    print("  手机访问：运行 启动手机版助手.bat 后使用生成的网址")
    print("  访问密码：无需密码")
    print("  按 Ctrl+C 停止")
    print("")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("已停止。")


if __name__ == "__main__":
    main()
