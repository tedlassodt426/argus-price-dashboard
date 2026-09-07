# -*- coding: utf-8 -*-
"""
Argus Web Service 接口测试小工具

作用：
  1. 用 config.ini 里的 Argus 账号密码调用官方接口登录（不需要额外密钥）
  2. 如果登录成功（LoginResult=0），再探测一次数据表更新时间，
     确认账号能拉取价格数据

用法：
  python argus_ws_test.py        测试登录和数据权限
"""

import configparser
import argparse
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
WS_URL = "https://www.argusmedia.com/ArgusWSVSTO/ArgusOnline.asmx"
SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"
TNS = "http://tempuri.org/"


class ArgusError(Exception):
    pass


def load_config(path: Path):
    if not path.exists():
        raise ArgusError(f"找不到配置文件 {path}，请把 config.ini 放在脚本同目录下。")
    cfg = configparser.ConfigParser()
    cfg.read(path, encoding="utf-8-sig")
    username = cfg.get("ARGUS", "username", fallback="").strip()
    password = cfg.get("ARGUS", "password", fallback="").strip()
    if not username or not password or username.startswith("你的"):
        raise ArgusError(
            "config.ini 里还没有填写账号密码。\n"
            "请用记事本打开 config.ini，把 username 和 password 换成你的 Argus 登录账号密码后保存。"
        )
    return username, password


def soap_call(operation: str, body_xml: str, timeout: int = 60):
    envelope = f"""<?xml version="1.0" encoding="utf-8"?>
<soap12:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                 xmlns:xsd="http://www.w3.org/2001/XMLSchema"
                 xmlns:soap12="{SOAP_NS}">
  <soap12:Body>
    {body_xml}
  </soap12:Body>
</soap12:Envelope>"""
    req = urllib.request.Request(
        WS_URL,
        data=envelope.encode("utf-8"),
        headers={
            "Content-Type": "application/soap+xml; charset=utf-8",
            "User-Agent": "Mozilla/5.0 (argus-ws-test/1.0)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return ET.fromstring(resp.read())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            raw = e.read().decode("utf-8", errors="replace")
            root = ET.fromstring(raw)
            for elem in root.iter():
                if isinstance(elem.tag, str) and elem.tag.endswith("Text"):
                    detail = (elem.text or "").strip()
                    break
        except Exception:
            pass
        msg = f"接口返回 HTTP {e.code}"
        if detail:
            msg += f"：{detail}"
        raise ArgusError(msg)
    except urllib.error.URLError as e:
        raise ArgusError(f"网络连接失败：{e.reason}")


def find_text(root, local_name: str, ns=TNS):
    """在响应里按标签名找文本值"""
    for elem in root.iter():
        tag = elem.tag
        if isinstance(tag, str) and tag.endswith("}" + local_name):
            return elem.text
    return None


def authenticate(username: str, password: str):
    body = f"""<Authenticate xmlns="{TNS}">
      <username>{username}</username>
      <password>{password}</password>
    </Authenticate>"""
    root = soap_call("Authenticate", body)
    login_result = find_text(root, "LoginResult")
    auth_token = find_text(root, "AuthToken")
    return login_result, auth_token


def get_tables_last_updated(auth_token: str):
    body = f"""<GetTablesLastUpdated xmlns="{TNS}">
      <authToken>{auth_token}</authToken>
    </GetTablesLastUpdated>"""
    root = soap_call("GetTablesLastUpdated", body)
    out_dir = SCRIPT_DIR / "argus_output"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "tables_last_updated.xml").write_text(
        ET.tostring(root, encoding="unicode"), encoding="utf-8")
    return root


def main():
    parser = argparse.ArgumentParser(description="Argus Web Service 接口测试")
    parser.add_argument("--config", default=str(SCRIPT_DIR / "config.ini"),
                        help="配置文件路径（默认脚本同目录的 config.ini）")
    args = parser.parse_args()
    cfg_path = Path(args.config)
    try:
        username, password = load_config(cfg_path)
        print("正在连接 Argus 官方接口登录 ...")
        login_result, auth_token = authenticate(username, password)
        print("")
        print("登录结果代码 LoginResult =", login_result)

        if login_result != "0":
            print("")
            print("登录失败：这个账号在 Argus 数据接口上没有被认可。")
            print("可能原因：")
            print("  1. 账号密码填错；")
            print("  2. 订阅未开通数据接口权限（FULL SERVICE 不一定包含数据接口，")
            print("     需要联系 Argus 客户经理确认/开通）。")
            sys.exit(1)

        print("登录成功！账号已取得数据接口令牌。")
        print("")
        print("正在探测数据权限（查询数据表更新时间）...")
        get_tables_last_updated(auth_token)
        print("数据权限探测通过：这个账号可以从接口拉取价格数据。")
        print("")
        print("下一步我可以帮你写正式的拉取脚本：按日期把煤炭价格（API2、API4 等）")
        print("从 Argus 接口取下来存成表格。")
    except ArgusError as e:
        print("")
        print("出错了：")
        print(str(e))
        sys.exit(1)
    except Exception as e:
        print("")
        print("程序出现未预期的错误：")
        print(repr(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
