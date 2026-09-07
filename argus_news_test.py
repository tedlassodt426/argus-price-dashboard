# -*- coding: utf-8 -*-
"""测试 Argus 新闻接口"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from argus_ws_test import (SCRIPT_DIR, TNS, ArgusError, authenticate,
                           load_config, soap_call)

XSI = "http://www.w3.org/2001/XMLSchema-instance"


def main():
    try:
        username, password = load_config(SCRIPT_DIR / "config.ini")
        _, token = authenticate(username, password)
        print("登录成功，测试新闻接口 ...")

        # 1) GetNewsItemsByDateTime
        body = f"""<GetNewsItemsByDateTime xmlns="{TNS}"
                 xmlns:xsi="{XSI}">
          <authToken>{token}</authToken>
          <dateTimeFrom>2026-08-07T00:00:00</dateTimeFrom>
          <dateTimeTo>2026-08-07T23:59:59</dateTimeTo>
          <free xsi:nil="true" />
          <featured xsi:nil="true" />
          <languageId xsi:nil="true" />
          <newsTypeId xsi:nil="true" />
        </GetNewsItemsByDateTime>"""
        try:
            root = soap_call("GetNewsItemsByDateTime", body)
            out = SCRIPT_DIR / "argus_output" / "news_test.xml"
            out.write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")
            # 提取新闻条目
            items = []
            for elem in root.iter():
                if not isinstance(elem.tag, str):
                    continue
                if elem.tag.rsplit("}", 1)[-1] == "NewsEntity":
                    d = {}
                    for c in elem:
                        if isinstance(c.tag, str):
                            d[c.tag.rsplit("}", 1)[-1]] = (c.text or "").strip()
                    items.append(d)
            print(f"GetNewsItemsByDateTime 返回 {len(items)} 条新闻")
            for it in items[:10]:
                print("  ", it.get("NewsId"), "|", it.get("PublicationDate"), "|", it.get("Headline"))
            if items:
                # 取第一条看全文
                nid = items[0]["NewsId"]
                body2 = f"""<GetNewsStory xmlns="{TNS}">
                  <authToken>{token}</authToken>
                  <newsId>{nid}</newsId>
                </GetNewsStory>"""
                root2 = soap_call("GetNewsStory", body2)
                text = ""
                for elem in root2.iter():
                    if isinstance(elem.tag, str) and elem.tag.rsplit("}", 1)[-1] == "Text":
                        text = (elem.text or "")[:500]
                print("第一条新闻正文预览:", text)
        except ArgusError as e:
            print("GetNewsItemsByDateTime 失败:", e)

        # 2) GetNewsList 备用
        body3 = f"""<GetNewsList xmlns="{TNS}">
          <authToken>{token}</authToken>
          <dateFrom>2026-08-07T00:00:00</dateFrom>
          <dateTo>2026-08-07T23:59:59</dateTo>
          <categoryiesListOr>false</categoryiesListOr>
          <regionListOr>false</regionListOr>
        </GetNewsList>"""
        try:
            root3 = soap_call("GetNewsList", body3)
            ids = []
            for elem in root3.iter():
                if isinstance(elem.tag, str) and elem.tag.rsplit("}", 1)[-1] == "decimal":
                    ids.append((elem.text or "").strip())
            print("GetNewsList 返回新闻ID数量:", len(ids), "前5个:", ids[:5])
        except ArgusError as e:
            print("GetNewsList 失败:", e)
    except ArgusError as e:
        print("出错了：", e)
        sys.exit(1)
    except Exception as e:
        print("未预期错误：", repr(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
