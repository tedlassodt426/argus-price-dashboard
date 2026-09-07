# -*- coding: utf-8 -*-
"""
日报 OCR 识别模块
=================
三种引擎：
  - umi（默认，推荐）：Umi-OCR（Windows 离线 OCR 软件，自带 PaddleOCR 引擎，
    质量好、速度快，通过本机 HTTP 接口调用；需要先启动 Umi-OCR）
  - rapid（本地轻量备用）：RapidOCR（PP-OCRv6，ONNX，CPU 可跑，不需要显卡）
  - unlimited（远程 GPU）：baidu/Unlimited-OCR 服务（OpenAI 兼容接口）

两种模式（仅本地 rapid 引擎适用）：
  - full：整份日报每页都走 OCR（识别最稳，首次较慢）
  - auto：有文字层的页面用 PDF 精确文字，识别不出的页面自动切 OCR

配置（config.ini 的 [OCR] 段，环境变量优先）：
  [OCR]
  backend      = umi                    # umi / rapid / unlimited / 留空=关闭
  mode         = full                   # full / auto
  dpi          = 120                    # 渲染分辨率（100 会糊，150 更慢）
  concurrency  = 2                      # 本地并行 OCR 引擎数
  umi_url      = http://127.0.0.1:1224  # Umi-OCR HTTP 服务地址
  umi_language = models/config_en.txt   # Umi-OCR 英文模型
  umi_limit_side_len = 2880             # Umi-OCR 图像边长上限（越大越精细）
  url          = http://127.0.0.1:10000 # unlimited 引擎的服务地址
  model        = Unlimited-OCR
  timeout      = 900

不配置 backend 时 OCR 关闭，日报走本地 PDF 文字提取。
"""

import base64
import json
import os
import re
import statistics
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pdfplumber

SCRIPT_DIR = Path(__file__).resolve().parent


def _load_config():
    """读取 config.ini 的 [OCR] 段（不碰账号密码段）"""
    cfg = {}
    ini = Path(os.environ.get("ARGUS_CONFIG", str(SCRIPT_DIR / "config.ini")))
    if not ini.exists():
        return cfg
    try:
        in_section = False
        for line in ini.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            low = line.lower()
            if low.startswith("[") and low.endswith("]"):
                in_section = low == "[ocr]"
                continue
            if in_section and "=" in line \
                    and not line.startswith("#") and not line.startswith(";"):
                k, _, v = line.partition("=")
                cfg[k.strip().lower()] = v.strip()
    except Exception:
        pass
    return cfg


_CFG = _load_config()

BACKEND = os.environ.get("ARGUS_OCR_BACKEND", "").strip().lower() \
    or _CFG.get("backend", "").strip().lower()
MODE = os.environ.get("ARGUS_OCR_MODE", "").strip().lower() \
    or _CFG.get("mode", "full").strip().lower()
OCR_DPI = int(os.environ.get("ARGUS_OCR_DPI", "")
              or _CFG.get("dpi", "120") or "120")
OCR_CONCURRENCY = int(os.environ.get("ARGUS_OCR_CONCURRENCY", "")
                      or _CFG.get("concurrency", "2") or "2")

UMI_URL = os.environ.get("ARGUS_UMI_URL", "").strip().rstrip("/") \
    or _CFG.get("umi_url", "http://127.0.0.1:1224")
UMI_LANGUAGE = os.environ.get("ARGUS_UMI_LANGUAGE", "").strip() \
    or _CFG.get("umi_language", "models/config_en.txt")
UMI_LIMIT = int(os.environ.get("ARGUS_UMI_LIMIT", "")
                or _CFG.get("umi_limit_side_len", "2880") or "2880")

OCR_URL = os.environ.get("ARGUS_OCR_URL", "").strip().rstrip("/") \
    or _CFG.get("url", "")
OCR_MODEL = os.environ.get("ARGUS_OCR_MODEL", "").strip() \
    or _CFG.get("model", "Unlimited-OCR")
OCR_TIMEOUT = int(os.environ.get("ARGUS_OCR_TIMEOUT", "")
                  or _CFG.get("timeout", "900") or "900")

NUMERIC_ONLY = re.compile(r"^[\d\s,.\-+%$€£¥/()'']+$")
PAGE_MARK = "@@PAGE@@"
META_MARK = "@@META@@"


def ocr_enabled():
    return BACKEND in ("umi", "rapid", "unlimited")


def ocr_engine_name():
    if BACKEND == "umi":
        return "Umi-OCR"
    if BACKEND == "unlimited":
        return "Unlimited-OCR"
    if BACKEND == "rapid":
        return "RapidOCR"
    return ""


# ---------------- 本地 RapidOCR 引擎 ----------------

_ENGINES = []
_ENGINE_LOCK = threading.Lock()


def _get_engines(n):
    """确保有 n 个 RapidOCR 引擎实例（onnxruntime 必须先于 rapidocr 加载）"""
    global _ENGINES
    with _ENGINE_LOCK:
        while len(_ENGINES) < n:
            import onnxruntime  # noqa: F401  关键：必须先加载 onnxruntime
            from rapidocr import RapidOCR
            _ENGINES.append(RapidOCR())
    return _ENGINES


def _in_boxes(c, boxes, margin=3):
    x0, top, x1, bottom = c["x0"], c["top"], c["x1"], c["bottom"]
    for bx in boxes:
        if (x0 >= bx[0] - margin and x1 <= bx[2] + margin
                and top >= bx[1] - margin and bottom <= bx[3] + margin):
            return True
    return False


def _point_in_boxes(x, y, boxes, margin=0):
    for bx in boxes:
        if bx[0] - margin <= x <= bx[2] + margin \
                and bx[1] - margin <= y <= bx[3] + margin:
            return True
    return False


def _junk_text(text):
    """OCR 行杂物过滤：页眉/页码/网址/全大写章节标题等"""
    if text == PAGE_MARK:
        return False
    compact = text.replace(" ", "")
    if len(compact) <= 1:
        return True
    if compact.isupper() and len(compact) > 3:
        return True
    if text.startswith(("www.", "http://", "https://", "Copyright", "©",
                        "Licensed to", "Thermal Coal + Freight")):
        return True
    if text.lower() in ("argus", "argusmedia.com"):
        return True
    if re.match(r"^Page\s+\d+\s+of\s+\d+$", text):
        return True
    if re.match(r"^Argus Coal Daily [A-Za-z]+ational$", text):
        return True
    if re.match(r"^Issue\s+\d+-\d+", text):
        return True
    return False


def _norm(text):
    """把弯引号/弯撇号统一成直引号，用于标题前缀匹配"""
    return (text.replace("\u2019", "'").replace("\u2018", "'")
            .replace("\u201c", '"').replace("\u201d", '"').lower())


def _page_boxes(page):
    """表格区域 + 图片区域（用于把价格表/图表从新闻 OCR 里剔除）"""
    boxes = [t.bbox for t in page.find_tables()]
    boxes += [(im["x0"], im["top"], im["x1"], im["bottom"]) for im in page.images]
    return boxes


def _page_image(page, dpi=OCR_DPI):
    """渲染页面为 numpy 数组（RGB）"""
    pil = page.to_image(resolution=dpi).original
    return np.array(pil.convert("RGB"))


def _pdf_headline_ys(pdf, page_idx, dpi=OCR_DPI):
    """用 PDF 文字层字号识别标题行，返回 [(col, y, text)]（OCR 像素坐标）"""
    from argus_reports import PAGE_HEADER
    page = pdf.pages[page_idx - 1]
    boxes = _page_boxes(page)
    mid = page.width / 2
    groups = {}
    for c in page.chars:
        if _in_boxes(c, boxes):
            continue
        key = round(c["top"] / 2) * 2
        groups.setdefault(key, []).append(c)
    ys = []
    for key, cs in groups.items():
        col_chars = {0: [], 1: []}
        for c in cs:
            col_chars[0 if c["x0"] < mid else 1].append(c)
        for col in (0, 1):
            ccs = col_chars[col]
            if not ccs:
                continue
            ccs.sort(key=lambda c: c["x0"])
            text = "".join(c["text"] for c in ccs).strip()
            size = max(c["size"] for c in ccs)
            if not text or size < 10.5 or size >= 12:
                continue
            if PAGE_HEADER.search(text):
                continue
            compact = text.replace(" ", "")
            if len(compact) <= 3 or compact.isupper():
                continue
            ys.append((col, (key + size / 2) * dpi / 72, text))
    return ys


def _ocr_page(engine, pdf, page_idx, dpi=OCR_DPI):
    """OCR 单页，剔除表格/图表区域，返回 [(text, cy, box_h, score)]"""
    page = pdf.pages[page_idx - 1]
    boxes = _page_boxes(page)
    arr = _page_image(page, dpi)
    res = engine(arr)
    d = res if isinstance(res, dict) else res.__dict__
    txts = d.get("txts") or []
    box_list = d.get("boxes")
    if box_list is None:
        box_list = []
    scores = d.get("scores")
    if scores is None:
        scores = d.get("rec_scores") or []
    out = []
    for i, t in enumerate(txts):
        b = box_list[i]
        cx = (b[0][0] + b[2][0]) / 2
        cy = (b[0][1] + b[2][1]) / 2
        if _point_in_boxes(cx, cy, boxes):
            continue
        text = t.strip()
        if not text:
            continue
        if len(text) <= 6 and NUMERIC_ONLY.match(text):
            continue
        try:
            score = float(scores[i])
        except Exception:
            score = 0.0
        out.append((text, cy, b[2][1] - b[0][1], score, b))
    return out, arr.shape[1]


def _items_to_lines(items, page_w_px, headline_ys=None):
    """OCR 结果按左右栏排序、按标题位置/框高判定标题，返回 [(text, size)]"""
    if not items:
        return []
    headlines = headline_ys or []
    col_items = {0: [], 1: []}
    for it in items:
        text, cy, h, score, b = it
        cx = (b[0][0] + b[2][0]) / 2
        col = 0 if cx < page_w_px / 2 else 1
        col_items[col].append((cy, text, h, score))
    lines = []
    for col in (0, 1):
        col_items[col].sort(key=lambda x: x[0])
        for cy, text, h, score in col_items[col]:
            if score < 0.55:
                continue
            if _junk_text(text):
                continue
            split_head = None
            for c, _, ht in headlines:
                if c == col and len(ht) >= 12 \
                        and _norm(text).startswith(_norm(ht)):
                    split_head = ht
                    break
            is_head = split_head is not None or any(
                c == col and abs(cy - y) <= 10 for c, y, _ in headlines)
            if is_head:
                if split_head:
                    rest = text[len(split_head):].strip().lstrip(".,:; ")
                    lines.append((split_head, 10.5))
                    if rest and not _junk_text(rest):
                        lines.append((rest, 9.0))
                else:
                    lines.append((text, 10.5))
            elif not headlines and h >= max(26, _h_threshold(col_items)):
                lines.append((text, 10.5))
            else:
                lines.append((text, 9.0))
    return lines


def _h_threshold(col_items):
    hs = [h for _, _, h, _ in col_items[0] + col_items[1]]
    return statistics.median(hs) * 1.15 if hs else 26


# ---------------- Umi-OCR（Paddle）引擎 ----------------


def _umi_page_width_px(pdf, page_idx, dpi=OCR_DPI):
    return pdf.pages[page_idx - 1].width * dpi / 72


def _umi_ocr_page(pdf, page_idx, dpi=OCR_DPI):
    """调 Umi-OCR HTTP 接口识别单页，剔除表格/图表区域，返回 items"""
    import base64 as _b64
    import io as _io
    import requests
    page = pdf.pages[page_idx - 1]
    boxes = _page_boxes(page)
    pil = page.to_image(resolution=dpi).original
    buf = _io.BytesIO()
    pil.save(buf, format="PNG")
    b64 = _b64.b64encode(buf.getvalue()).decode("utf-8")
    payload = {
        "base64": b64,
        "options": {
            "ocr.language": UMI_LANGUAGE,
            "ocr.limit_side_len": UMI_LIMIT,
            "ocr.cls": False,
            "tbpu.parser": "multi_para",
            "data.format": "dict",
        },
    }
    resp = requests.post(f"{UMI_URL}/api/ocr", json=payload, timeout=180)
    resp.raise_for_status()
    d = resp.json()
    if d.get("code") == 101:
        return []
    if d.get("code") != 100:
        raise RuntimeError(f"Umi-OCR 返回 {d.get('code')}: {d.get('data')}")
    items = []
    for it in d.get("data") or []:
        text = (it.get("text") or "").strip()
        if not text:
            continue
        box = it.get("box") or []
        if len(box) < 4:
            continue
        cx = (box[0][0] + box[2][0]) / 2
        cy = (box[0][1] + box[2][1]) / 2
        if _point_in_boxes(cx, cy, boxes):
            continue
        try:
            score = float(it.get("score", 0))
        except Exception:
            score = 0.0
        items.append({
            "text": text,
            "box": box,
            "cy": cy,
            "h": box[2][1] - box[0][1],
            "score": score,
            "end": it.get("end", "\n"),
        })
    return items


def _umi_items_to_lines(items, page_w_px, headline_ys=None):
    """Umi-OCR 结果按左右栏排序、段落重组、标题判定，返回 [(text, size)]"""
    if not items:
        return []
    headlines = headline_ys or []
    col_items = {0: [], 1: []}
    for it in items:
        cx = (it["box"][0][0] + it["box"][2][0]) / 2
        col = 0 if cx < page_w_px / 2 else 1
        col_items[col].append(it)
    lines = []
    for col in (0, 1):
        col_items[col].sort(key=lambda x: x["cy"])
        para = ""
        for it in col_items[col]:
            if it["score"] < 0.55:
                continue
            text = it["text"]
            if _junk_text(text):
                continue
            split_head = None
            # OCR 有时把标题和正文合并成一行，按 PDF 标题文本前缀拆分（优先）
            for c, _, ht in headlines:
                if c == col and len(ht) >= 12 \
                        and _norm(text).startswith(_norm(ht)):
                    split_head = ht
                    break
            is_head = split_head is not None or any(
                c == col and abs(it["cy"] - y) <= 10 for c, y, _ in headlines)
            if is_head:
                if para.strip():
                    lines.append((para.strip(), 9.0))
                    para = ""
                if split_head:
                    lines.append((split_head, 10.5))
                    rest = text[len(split_head):].strip().lstrip(".,:; ")
                    if rest and not _junk_text(rest):
                        if it["end"] == "\n":
                            lines.append((rest, 9.0))
                        else:
                            para = rest
                else:
                    lines.append((text, 10.5))
                continue
            sep = it["end"] if it["end"] in (" ", "") else ""
            para += text + sep
            if it["end"] == "\n":
                if para.strip():
                    lines.append((para.strip(), 9.0))
                para = ""
        if para.strip():
            lines.append((para.strip(), 9.0))
    return lines


def _umi_available():
    import requests
    try:
        r = requests.get(f"{UMI_URL}/api/ocr/get_options", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def _read_cache(cache_path):
    """读取 OCR 行缓存，返回 (lines, ocr_used)"""
    if not cache_path.exists():
        return None, False
    try:
        lines = []
        ocr_used = False
        for ln in cache_path.read_text(encoding="utf-8").splitlines():
            if ln.startswith(META_MARK):
                ocr_used = ln.split("\t")[1] == "1"
                continue
            if "\t" in ln:
                t, s = ln.rsplit("\t", 1)
                try:
                    lines.append((t, float(s)))
                except ValueError:
                    pass
        return lines, ocr_used
    except Exception:
        return None, False


def _write_cache(cache_path, lines, ocr_used):
    try:
        body = "\n".join(f"{t}\t{s}" for t, s in lines)
        cache_path.write_text(
            f"{META_MARK}\t{1 if ocr_used else 0}\n{body}",
            encoding="utf-8")
    except Exception:
        pass


def ocr_report_lines(date, kind, force=False):
    """full 模式：整份日报每页 OCR（umi 优先，失败回退 rapid），返回 (lines, ocr_used)"""
    if BACKEND not in ("rapid", "umi"):
        return [], False
    from argus_reports import TEXT_DIR, find_file
    f = find_file(date, kind)
    if not f:
        return [], False
    cache_name = "v7.umi.lines.tsv" if BACKEND == "umi" else "v7.ocr.lines.tsv"
    cache = TEXT_DIR / f"{kind}_{date}.{cache_name}"
    if not force:
        cached = _read_cache(cache)
        if cached[0] is not None:
            return cached

    use_umi = BACKEND == "umi" and _umi_available()
    if BACKEND == "umi" and not use_umi:
        print("[argus_ocr] Umi-OCR 未启动，自动回退 RapidOCR")
    with pdfplumber.open(str(f)) as pdf:
        total = len(pdf.pages)
        engines = _get_engines(max(1, OCR_CONCURRENCY))
        results = {}

        def work(page_idx):
            ys = _pdf_headline_ys(pdf, page_idx)
            if use_umi:
                try:
                    items = _umi_ocr_page(pdf, page_idx)
                    w = _umi_page_width_px(pdf, page_idx)
                    return page_idx, _umi_items_to_lines(items, w, ys)
                except Exception as e:
                    print(f"[argus_ocr] 第 {page_idx} 页 Umi-OCR 失败: {e}，回退 RapidOCR")
            engine = engines[page_idx % len(engines)]
            items, w = _ocr_page(engine, pdf, page_idx)
            return page_idx, _items_to_lines(items, w, ys)

        workers = 1 if use_umi else max(1, OCR_CONCURRENCY)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(work, p): p for p in range(1, total + 1)}
            for fut in as_completed(futures):
                try:
                    p, page_lines = fut.result()
                    results[p] = page_lines
                except Exception as e:
                    print(f"[argus_ocr] 第 {futures[fut]} 页 OCR 失败: {e}")

    lines = []
    for p in sorted(results):
        lines.append((PAGE_MARK, 0))
        lines.extend(results[p])
    ocr_used = bool(lines)
    _write_cache(cache, lines, ocr_used)
    return lines, ocr_used


def auto_report_lines(date, kind, force=False):
    """auto 模式：文字层强的页面用 PDF 精确文字，弱的页面切 OCR（umi 优先）"""
    if BACKEND not in ("rapid", "umi"):
        return [], False
    from argus_reports import TEXT_DIR, find_file
    f = find_file(date, kind)
    if not f:
        return [], False
    cache_name = "v7.umi_auto.lines.tsv" if BACKEND == "umi" else "v7.auto.lines.tsv"
    cache = TEXT_DIR / f"{kind}_{date}.{cache_name}"
    if not force:
        cached = _read_cache(cache)
        if cached[0] is not None:
            return cached

    lines = []
    ocr_used = False
    use_umi = BACKEND == "umi" and _umi_available()
    engines = _get_engines(max(1, OCR_CONCURRENCY))
    with pdfplumber.open(str(f)) as pdf:
        for page_idx, page in enumerate(pdf.pages, start=1):
            lines.append((PAGE_MARK, 0))
            pdf_lines = _pdf_page_lines(page)
            strong = sum(len(t) for t, _ in pdf_lines) >= 300
            if strong:
                lines.extend(pdf_lines)
                continue
            try:
                ys = _pdf_headline_ys(pdf, page_idx)
                if use_umi:
                    items = _umi_ocr_page(pdf, page_idx)
                    w = _umi_page_width_px(pdf, page_idx)
                    ocr_lines = _umi_items_to_lines(items, w, ys)
                else:
                    items, w = _ocr_page(engines[0], pdf, page_idx)
                    ocr_lines = _items_to_lines(items, w, ys)
                if ocr_lines:
                    lines.extend(ocr_lines)
                    ocr_used = True
                    continue
            except Exception as e:
                print(f"[argus_ocr] 第 {page_idx} 页 OCR 失败: {e}")
            lines.extend(pdf_lines)
    _write_cache(cache, lines, ocr_used)
    return lines, ocr_used


def _pdf_page_lines(page):
    """和 argus_reports._page_lines 相同的按栏提取（避免循环导入）"""
    from argus_reports import PAGE_HEADER, TABLE_JUNK, _page_lines
    out = []
    for text, size in _page_lines(page):
        if PAGE_HEADER.search(text) or text == "Argus":
            continue
        if TABLE_JUNK.match(text):
            continue
        if len(text) <= 6 and NUMERIC_ONLY.match(text):
            continue
        out.append((text, size))
    return out


# ---------------- 远程 Unlimited-OCR 客户端 ----------------

DET_RE = re.compile(
    r"<\|det\|>([^<\s]+)(?:\s*\[[^\]]*\])?\s*<\|/det\|>(.*)", re.DOTALL)


def _render_page(pdf_path, page_idx, dpi=OCR_DPI):
    with pdfplumber.open(str(pdf_path)) as pdf:
        im = pdf.pages[page_idx - 1].to_image(resolution=dpi)
        fd, tmp = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            im.save(tmp)
            with open(tmp, "rb") as f:
                return f.read()
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _payload(image_datas, image_mode):
    content = [{"type": "text", "text": "document parsing."}]
    for data in image_datas:
        b64 = base64.b64encode(data).decode("utf-8")
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        })
    return {
        "model": OCR_MODEL,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0,
        "skip_special_tokens": False,
        "stream": False,
        "images_config": {"image_mode": image_mode},
    }


def _post(payload):
    import requests
    resp = requests.post(
        f"{OCR_URL}/v1/chat/completions",
        json=payload,
        timeout=OCR_TIMEOUT,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"OCR 服务返回 {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"OCR 服务响应异常: {json.dumps(data)[:300]}")


def parse_blocks(raw):
    """把 Unlimited-OCR 输出拆成版面块：[(type, text)]"""
    blocks = []
    cur_type = None
    cur = []
    for line in (raw or "").splitlines():
        line = line.rstrip()
        if not line:
            continue
        m = DET_RE.match(line)
        if m:
            if cur_type is not None and cur:
                blocks.append((cur_type, " ".join(cur).strip()))
            typ = m.group(1).strip()
            content = m.group(2).strip()
            cur_type = typ
            cur = [content] if content else []
        else:
            if cur_type is not None:
                cur.append(line.strip())
    if cur_type is not None and cur:
        blocks.append((cur_type, " ".join(cur).strip()))
    return [(t, c) for t, c in blocks if c]


def blocks_to_lines(blocks):
    title_types = ("title", "headline", "heading")
    body_types = ("text", "paragraph", "caption")
    lines = []
    for typ, text in blocks:
        t = text.strip()
        if not t:
            continue
        if typ in title_types:
            lines.append((t, 10.5))
        elif typ in body_types:
            lines.append((t, 9.0))
    return lines


def unlimited_report_lines(date, kind, force=False):
    """unlimited 模式：远程 GPU 服务整份解析，返回 (lines, ocr_used)"""
    if BACKEND != "unlimited" or not OCR_URL:
        return [], False
    from argus_reports import TEXT_DIR, find_file
    f = find_file(date, kind)
    if not f:
        return [], False
    cache = TEXT_DIR / f"{kind}_{date}.v7.unlimited.lines.tsv"
    if not force:
        cached = _read_cache(cache)
        if cached[0] is not None:
            return cached
    concurrency = max(1, OCR_CONCURRENCY)
    results = {}

    def work(page_idx):
        data = _render_page(str(f), page_idx)
        raw = _post(_payload([data], "gundam"))
        return page_idx, blocks_to_lines(parse_blocks(raw))

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(work, p): p for p in range(1, _page_count(f) + 1)}
        for fut in as_completed(futures):
            try:
                p, page_lines = fut.result()
                results[p] = page_lines
            except Exception as e:
                print(f"[argus_ocr] 第 {futures[fut]} 页解析失败: {e}")
    lines = []
    for p in sorted(results):
        lines.append((PAGE_MARK, 0))
        lines.extend(results[p])
    ocr_used = bool(lines)
    _write_cache(cache, lines, ocr_used)
    return lines, ocr_used


def _page_count(f):
    with pdfplumber.open(str(f)) as pdf:
        return len(pdf.pages)


def get_report_lines(date, kind, force=False):
    """按当前配置取日报行数据，返回 (lines, ocr_used)"""
    if BACKEND in ("rapid", "umi"):
        if MODE == "auto":
            return auto_report_lines(date, kind, force)
        return ocr_report_lines(date, kind, force)
    if BACKEND == "unlimited":
        return unlimited_report_lines(date, kind, force)
    return None, False
