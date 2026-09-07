# -*- coding: utf-8 -*-
"""
从参考日全量数据生成"价格目录"（price_catalog.csv）

用法：python build_catalog.py
输入：argus_output/days/argus_YYYY-MM-DD.csv（用 argus_fetch_day.py 生成）
输出：price_catalog.csv（所有可拉取价格 + 能识别出的中文名）
"""

import csv
import json
import sys
from collections import OrderedDict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# 已确认的价格名称（编号, 价格类型, 时间戳 -> 名称）
KNOWN = {
    ("2608", "8", "6"): "API 2 现货（6,000 NAR cif ARA 2个月）",
    ("2579", "8", "6"): "API 4 现货（6,000 NAR fob 理查兹湾 2个月）",
    ("24497", "8", "6"): "ARA 5,700 NAR cif（2个月）",
    ("24496", "8", "6"): "理查兹湾 5,700 NAR fob（2个月）",
    ("27216", "8", "0"): "ARA 6,000 kcal cif（周均价）",
    ("27214", "8", "0"): "理查兹湾 6,000 kcal fob（周均价）",
    ("2586", "8", "6"): "纽卡斯尔 6,000 kcal fob（周均价）",
    ("9852", "8", "6"): "纽卡斯尔 5,500 kcal fob（周均价）",
    ("9290", "8", "21"): "汉普顿路 6,000 kcal fob（周均价）",
    ("8803", "8", "6"): "华南 CFR 5,500 kcal（周均价）",
    ("6642", "8", "6"): "秦皇岛内贸 fob（周均价）",
    ("12678", "8", "6"): "理查兹湾 5,500 kcal fob（周均价）",
    ("27070", "8", "6"): "理查兹湾 4,800 kcal fob（周均价）",
    ("2588", "8", "21"): "Puerto Bolivar fob（周均价）",
    ("2582", "8", "6"): "Vostochny 6,000 kcal fob（周均价）",
    ("20204", "8", "6"): "Vostochny 5,500 kcal fob（周均价）",
    ("20205", "8", "6"): "Black Sea fob（周均价）",
    ("2581", "8", "6"): "波罗的海港口 fob（周均价）",
    ("9657", "8", "6"): "Turkey mini bulk cif（周均价）",
    ("11139", "8", "6"): "Turkey supra cif（周均价）",
    ("2140", "4", "0"): "API 2 指数（周，cif ARA 6,000）",
    ("2141", "4", "0"): "API 4 指数（周，fob 理查兹湾 6,000）",
    ("7773", "4", "0"): "API 2 指数（日，cif ARA 6,000）",
    ("7774", "4", "0"): "API 4 指数（日，fob 理查兹湾 6,000）",
    ("12924", "4", "0"): "API 3 指数（周，fob 理查兹湾 5,500）",
    ("10632", "4", "0"): "API 5 指数（周，fob 纽卡斯尔 5,500）",
    ("3076", "4", "0"): "API 6 指数（周，fob 纽卡斯尔 6,000）",
    ("10633", "4", "0"): "API 8 指数（周，cfr 华南 5,500）",
    ("12111", "4", "0"): "API 10 指数（周，fob Puerto Bolivar 6,000）",
    ("13892", "4", "0"): "API 12 指数（周，cfr 印度 5,500）",
    ("4251", "4", "0"): "ICI 1（印尼 6,500 GAR，周）",
    ("4252", "4", "0"): "ICI 2（印尼 5,800 GAR，周）",
    ("4253", "4", "0"): "ICI 3（印尼 5,000 GAR，周）",
    ("5483", "4", "0"): "ICI 4（印尼 4,200 GAR，周）",
    ("9543", "4", "0"): "ICI 5（印尼 3,400 GAR，周）",
    ("2587", "8", "6"): "Argus ICI 1 日度（印尼 6,500 GAR）",
    ("3090", "8", "6"): "Argus ICI 2 日度（印尼 5,800 GAR）",
    ("4112", "8", "6"): "Argus ICI 3 日度（印尼 5,000 GAR）",
    ("5485", "8", "6"): "Argus ICI 4 日度（印尼 4,200 GAR）",
    ("9546", "8", "6"): "Argus ICI 5 日度（印尼 3,400 GAR）",
}

COAL_CODES = set(k[0] for k in KNOWN)


def main():
    days_dir = SCRIPT_DIR / "argus_output" / "days"
    files = sorted(days_dir.glob("argus_*.csv"))
    if not files:
        print("没有找到 argus_output/days/argus_*.csv，请先运行：")
        print("  python argus_fetch_day.py --date 2026-08-07")
        sys.exit(1)
    src = files[-1]
    date = src.stem.replace("argus_", "")
    print("参考日期：", date, "（", src.name, "）")

    rows = list(csv.DictReader(open(src, encoding="utf-8-sig")))
    # 只取当天新发布的记录，去掉修正
    fresh = [
        r for r in rows
        if (r.get("PUBLICATION_DATE") or "").startswith(date)
        and r.get("CORRECTION", "") not in ("C", "D")
    ]
    print("当天有效记录：", len(fresh))

    # 按 代码+价格类型+时间戳 去重（保留连续月份=1 的样例行）
    catalog = OrderedDict()
    for r in fresh:
        # 只收录美元/吨价格（单位组合 3,44），其他单位暂不入目录
        if (r.get("UNIT_ID1", "").strip(), r.get("UNIT_ID2", "").strip()) != ("3", "44"):
            continue
        key = (r.get("CODE_ID", "").strip(), r.get("PRICETYPE_ID", "").strip(),
               r.get("TIMESTAMP_ID", "").strip())
        if key in catalog:
            continue
        cf = r.get("CONTINUOUS_FORWARD", "0").strip()
        if cf not in ("", "0", "1"):
            continue
        catalog[key] = r

    out_path = SCRIPT_DIR / "price_catalog.csv"
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["KEY", "CODE_ID", "PRICETYPE_ID", "TIMESTAMP_ID", "名称", "类型",
                    "类别", "QUOTE_ID", "PRICEREPORTITEMID", "FORWARD_PERIOD",
                    "FORWARD_YEAR", "UNIT_ID1", "UNIT_ID2"])
        for (code, pt, ts), r in catalog.items():
            name = KNOWN.get((code, pt, ts))
            if name:
                freq = "日" if ts == "6" else "周"
                cat = "煤炭"
            else:
                name = f"代码 {code}（PT{pt}/TS{ts}）"
                freq = "日" if ts == "6" else ("周" if ts in ("0", "21") else "日")
                cat = "煤炭" if code in COAL_CODES else "其他"
            w.writerow([
                f"{code}|{pt}|{ts}", code, pt, ts, name, freq, cat,
                r.get("QUOTE_ID", ""), r.get("PRICEREPORTITEMID", "0"),
                r.get("FORWARD_PERIOD", ""), r.get("FORWARD_YEAR", ""),
                r.get("UNIT_ID1", ""), r.get("UNIT_ID2", ""),
            ])
    n = len(catalog)
    known_n = sum(1 for k in catalog if k in KNOWN)
    print(f"价格目录已生成：{out_path}（共 {n} 项，其中已识别名称 {known_n} 项）")
    print("已识别名称的价格会标注为「煤炭」；其他为「其他」，可自行在 CSV 里改名。")


if __name__ == "__main__":
    main()
