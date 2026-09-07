# -*- coding: utf-8 -*-
"""
Argus 煤炭价格拉取工具（快速版）

原理：用 Argus 官方 GetCustomReport 接口只拉 codes.csv 里列出的价格，
      一次请求约 1 秒，而不是像旧版那样拉全量 4000 多条再筛选。

用法：
  python argus_coal_fetch.py --date 2026-08-07
  python argus_coal_fetch.py --no-change
"""

import argparse
import csv
import json
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

from argus_ws_test import (SCRIPT_DIR, TNS, ArgusError, authenticate,
                           load_config, soap_call)


def load_codes(path):
    """读取 codes.csv（含快速接口需要的全部参数）"""
    codes = []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            codes.append({
                "code": r["CODE_ID"].strip(),
                "pt": r["PRICETYPE_ID"].strip(),
                "ts": r["TIMESTAMP_ID"].strip(),
                "name": r["名称"].strip(),
                "freq": r["类型"].strip(),
                "quote_id": r["QUOTE_ID"].strip(),
                "item_id": r["PRICEREPORTITEMID"].strip(),
                "fwd": r["FORWARD_PERIOD"].strip(),
                "year": r["FORWARD_YEAR"].strip(),
                "unit1": r["UNIT_ID1"].strip(),
                "unit2": r["UNIT_ID2"].strip(),
            })
    return codes


def fetch_custom(token, date, codes):
    """只拉指定价格（GetCustomReport），返回 {code: value}"""
    item_xml = "".join(f"""<PriceReportItem>
        <QuoteId>{c['quote_id']}</QuoteId>
        <PriceTypeId>{c['pt']}</PriceTypeId>
        <PriceReportItemID>{c['item_id']}</PriceReportItemID>
        <ForwardPeriod>{c['fwd']}</ForwardPeriod>
        <ForwardYear>{c['year']}</ForwardYear>
        <CurrencyUnit>{c['unit1']}</CurrencyUnit>
        <MeasureUnit>{c['unit2']}</MeasureUnit>
        <TimestampId>{c['ts']}</TimestampId>
      </PriceReportItem>""" for c in codes)
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
            if d.get("CODE_ID") and d.get("VALUE") is not None:
                values[d["CODE_ID"]] = d.get("VALUE", "").strip()
    return values


def prev_date_str(date_str):
    return (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")


def main():
    parser = argparse.ArgumentParser(description="Argus 煤炭价格拉取（快速）")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"),
                        help="日期，格式 2026-08-07（默认今天）")
    parser.add_argument("--no-change", action="store_true", help="不对比前一日")
    parser.add_argument("--config", default=str(SCRIPT_DIR / "config.ini"))
    args = parser.parse_args()

    try:
        username, password = load_config(Path(args.config))
        codes = load_codes(SCRIPT_DIR / "codes.csv")
        print("登录 Argus ...")
        _, token = authenticate(username, password)

        t0 = time.time()
        values = fetch_custom(token, args.date, codes)
        print(f"拉取 {args.date} 完成（{time.time()-t0:.1f} 秒）")

        prev_values = {}
        if not args.no_change:
            pdate = prev_date_str(args.date)
            prev_values = fetch_custom(token, pdate, codes)

        results = []
        for c in codes:
            value = values.get(c["code"])
            prev = None
            if not args.no_change and c["freq"] == "日" and value:
                prev = prev_values.get(c["code"])
            change = ""
            if value and prev not in (None, ""):
                try:
                    change = f"{float(value) - float(prev):+.2f}"
                except ValueError:
                    change = ""
            results.append({
                "日期": args.date,
                "名称": c["name"],
                "价格(美元/吨)": value if value else "",
                "前一日": prev if prev not in (None, "") else "",
                "涨跌": change,
                "频率": c["freq"],
            })

        out_dir = SCRIPT_DIR / "argus_output" / "煤价"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"煤价_{args.date}.csv"
        try:
            with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=["日期", "名称", "价格(美元/吨)", "前一日", "涨跌", "频率"])
                w.writeheader()
                w.writerows(results)
            print("已保存到：", out_path)
        except PermissionError:
            print("提示：结果文件正被 Excel 打开，无法覆盖保存。")
            print("请关闭 Excel 里的 煤价_2026-08-07.csv 后重试；"
                  "或先看下面的结果（一样完整）。")

        print("")
        print("================ 结果 ================")
        for r in results:
            print(f'{r["名称"]:<28} {r["价格(美元/吨)"]:>8}  {r["涨跌"]}')
        print("")
    except ArgusError as e:
        print("出错了：", e)
        sys.exit(1)
    except Exception as e:
        print("未预期错误：", repr(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
