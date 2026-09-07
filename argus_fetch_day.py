# -*- coding: utf-8 -*-
"""按日期拉取 Argus 接口全部更新记录（分页），保存原始 XML + CSV"""

import argparse
import csv
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from argus_ws_test import (SCRIPT_DIR, TNS, ArgusError, authenticate,
                           load_config, soap_call)


def parse_v_repo(root):
    rows = []
    for elem in root.iter():
        if not isinstance(elem.tag, str):
            continue
        if elem.tag.rsplit("}", 1)[-1] == "diffgram":
            for table in elem:
                for row in table:
                    if not isinstance(row.tag, str):
                        continue
                    cells = {}
                    for c in row:
                        if isinstance(c.tag, str):
                            cells[c.tag.rsplit("}", 1)[-1]] = (c.text or "").strip()
                    if "QUOTE_ID" in cells:
                        rows.append(cells)
    return rows


def fetch_updated(token, start, end, start_id, max_rows=10000):
    body = f"""<GetUpdatedPricesInDateTimeRange xmlns="{TNS}">
      <authToken>{token}</authToken>
      <fromDateTime>{start}T00:00:00</fromDateTime>
      <toDateTime>{end}T23:59:59</toDateTime>
      <startId>{start_id}</startId>
    </GetUpdatedPricesInDateTimeRange>"""
    return soap_call("GetUpdatedPricesInDateTimeRange", body)


def main():
    parser = argparse.ArgumentParser(description="拉取某天 Argus 价格更新")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"),
                        help="日期，格式 2026-08-07")
    parser.add_argument("--config", default=str(SCRIPT_DIR / "config.ini"))
    args = parser.parse_args()

    try:
        username, password = load_config(Path(args.config))
        _, token = authenticate(username, password)
        print("登录成功，开始分页拉取", args.date, "的数据 ...")

        all_rows = []
        start_id = 0
        page = 0
        while True:
            page += 1
            root = fetch_updated(token, args.date, args.date, start_id)
            rows = parse_v_repo(root)
            if not rows:
                break
            all_rows.extend(rows)
            max_id = max(int(r.get("REPOSITORY_ID", 0) or 0) for r in rows)
            print(f"第{page}页: {len(rows)} 行, 最大ID {max_id}")
            if len(rows) < 500:
                break
            if max_id <= start_id:
                break
            start_id = max_id

        print("合计:", len(all_rows), "行")
        out_dir = SCRIPT_DIR / "argus_output" / "days"
        out_dir.mkdir(parents=True, exist_ok=True)
        csv_path = out_dir / f"argus_{args.date}.csv"
        headers = []
        for r in all_rows:
            for k in r:
                if k not in headers:
                    headers.append(k)
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
            w.writeheader()
            w.writerows(all_rows)
        print("已保存:", csv_path)
    except ArgusError as e:
        print("出错了：", e)
        sys.exit(1)
    except Exception as e:
        print("未预期错误：", repr(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
