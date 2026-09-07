# -*- coding: utf-8 -*-
"""
Argus 日报模块：读取 D:/ArgusPublications 里的 PDF 日报，
按标题切分成"一条条新闻"，提取文字并翻译成中文（带缓存）。
"""

import json
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pdfplumber

SCRIPT_DIR = Path(__file__).resolve().parent
REPORTS_ROOT = Path(os.environ.get("ARGUS_REPORT_ROOT", "D:/ArgusPublications"))
TEXT_DIR = SCRIPT_DIR / "argus_output" / "reports_text"
CACHE_DIR = SCRIPT_DIR / "argus_output" / "reports_cache"
PRICE_DIR = SCRIPT_DIR / "argus_output" / "price_pages"
TEXT_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)
PRICE_DIR.mkdir(parents=True, exist_ok=True)

KINDS = {
    "cdi": {"folder": "Coal Daily International", "name": "Coal Daily International（国际煤炭日报）"},
    "ici": {"folder": "Indonesian Coal Index", "name": "Indonesian Coal Index（印尼煤价指数）"},
    "mck": {"folder": "McCloskey Coal Price Index", "name": "McCloskey Coal Price Index（煤炭价格指数）"},
}

FOOTER_PATTERNS = (
    re.compile(r"^Copyright .*Argus Media group", re.I),
    re.compile(r"^Licensed to:", re.I),
    re.compile(r"^Page \d+ of \d+$", re.I),
    re.compile(r"^Argus Media group Page \d+ of \d+$", re.I),
)
SECTION_HEADER = re.compile(
    r"^(NEWS AND ANALYSIS|COMMENTARY|CONTENTS|DATA[& ]|FORWARD PRICES|"
    r"DAILY PRICE ASSESSMENTS|GLOBAL WEEKLY|COAL MARKET NEWS|INDICES|"
    r"PRICES|MARKET ROUND-UP|WEEKLY|MONTHLY|QUARTERLY)[\sA-Z0-9&/|.$£€()-]*$"
)

# 纯价格表格行：数字/符号为主，不翻译
TABLE_LINE = re.compile(
    r"^[\d\s,.\-+%$€£¥/()'']+$"
)

NUMERIC_ONLY = re.compile(r"^[\d\s,.\-+%$€£¥/()'']+$")

# 表格残留/栏目头等垃圾行
TABLE_JUNK = re.compile(
    r"^(EnergyBasisTimingPortPrice|BasisTimingPortPrice|PricePrevious|"
    r"Weekly and monthly averages of daily assessments|"
    r"Daily price assessments|Timing|Buy|Sell|Average|Previous|Midpoint|"
    r"Index|Change|Low|High|Close|Open|Bid|Offer)[\sA-Z0-9&|./()'']*$",
    re.I,
)

PAGE_HEADER = re.compile(
    r"^(Argus\s*)?Coal Daily International\s*Issue\s*\d+-\d+.*$", re.I
)
CONTENTS_HEADER = re.compile(r"^(CONTENTS|CONTENTSDATA)", re.I)
PAGE_MARK = "@@PAGE@@"
STORY_NOISE = re.compile(
    r"\s+(?:El Nino clouds|South Korea eyes date|Netherlands coal-fired|"
    r"Kazakhstan moves|North Macedonia signs|Coal market prices, news and analysis|"
    r"\$t Daily price assessments|Weekly and monthly averages of daily assessments|"
    r"Wednesday \d{1,2} [A-Za-z]+ \d{4}|Argus (?:Richards Bay|cif ARA)|"
    r"International coal assessments|Available on the Argus Publications App|"
    r"(?:Europe|South Africa)\s+6,000\s+kcal|Price\s+Energy\s+Basis\s+Timing|"
    r"MA\d+(?:-day)?|\bhhh\b|\bIssue\s+\d+-\d+\b|"
    r"API\s+2\s+year[- ]ahead|fob\s+Indo|\bBy\s+[A-Z][a-z]+\s+[A-Z][a-z]+\b).*?$",
    re.I,
)


def _in_boxes(c, boxes, margin=3):
    """字符是否落在表格或图片区域内（用于剔除价格表/图表文字）"""
    x0, top, x1, bottom = c["x0"], c["top"], c["x1"], c["bottom"]
    for bx in boxes:
        if (x0 >= bx[0] - margin and x1 <= bx[2] + margin
                and top >= bx[1] - margin and bottom <= bx[3] + margin):
            return True
    return False


def _page_lines(page):
    """把一页文字按左右栏目分离，剔除表格/图片区域，返回 [(text, size)]"""
    boxes = [t.bbox for t in page.find_tables()]
    boxes += [(im["x0"], im["top"], im["x1"], im["bottom"]) for im in page.images]
    mid = page.width / 2
    groups = {}
    for c in page.chars:
        if _in_boxes(c, boxes):
            continue
        col = 0 if c["x0"] < mid else 1
        key = (col, round(c["top"] / 2) * 2)
        groups.setdefault(key, []).append(c)
    rows = []
    for (col, top), cs in groups.items():
        cs.sort(key=lambda c: c["x0"])
        text = "".join(c["text"] for c in cs).strip()
        text = text.lstrip("\u0084\u201e").strip()
        if not text:
            continue
        size = max(c["size"] for c in cs)
        rows.append((col, top, text, size))
    rows.sort(key=lambda r: (r[0], r[1]))
    return [(t, s) for _, _, t, s in rows]


def list_reports():
    """扫描 D:/ArgusPublications，返回 {date: [kind...]}（按日期倒序）"""
    by_date = {}
    for folder in (REPORTS_ROOT / m["folder"] for m in KINDS.values()):
        if not folder.exists():
            continue
        for f in folder.glob("*.pdf"):
            m = re.match(r"^(\d{8})(\w+)\.pdf$", f.name, re.I)
            if not m:
                continue
            file_kind = m.group(2).lower()
            if file_kind not in KINDS:
                continue
            date = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}"
            by_date.setdefault(date, []).append(file_kind)
    for d in by_date:
        by_date[d].sort(key=lambda k: list(KINDS).index(k))
    return dict(sorted(by_date.items(), reverse=True))


def find_file(date, kind):
    meta = KINDS.get(kind)
    if not meta:
        return None
    d = date.replace("-", "")
    f = REPORTS_ROOT / meta["folder"] / f"{d}{kind}.pdf"
    return f if f.exists() else None


def extract_lines(date, kind):
    """提取 PDF 文字行（含字号），按左右栏目分离并剔除表格/图表，返回 [(text, size)]，带缓存"""
    txt_path = TEXT_DIR / f"{kind}_{date}.v5.lines.tsv"
    if txt_path.exists():
        out = []
        for ln in txt_path.read_text(encoding="utf-8").splitlines():
            if "\t" in ln:
                t, s = ln.rsplit("\t", 1)
                try:
                    out.append((t, float(s)))
                except ValueError:
                    pass
        return out

    f = find_file(date, kind)
    if not f:
        return []

    lines = []
    with pdfplumber.open(str(f)) as pdf:
        for page in pdf.pages:
            lines.append((PAGE_MARK, 0))
            for text, size in _page_lines(page):
                if any(p.search(text) for p in FOOTER_PATTERNS):
                    continue
                if PAGE_HEADER.search(text):
                    continue
                if text == "Argus":
                    continue
                if TABLE_JUNK.match(text):
                    continue
                if text.startswith("This Workspace is curated by") \
                        or text.startswith("Thermal Coal + Freight"):
                    continue
                if len(text) <= 6 and NUMERIC_ONLY.match(text):
                    continue
                lines.append((text, size))

    txt_path.write_text(
        "\n".join(f"{t}\t{s}" for t, s in lines), encoding="utf-8")
    return lines


def build_stories(lines):
    """按标题把内容切成一条条新闻"""
    stories = []
    preamble = []
    current = None
    in_contents = False
    for text, size in lines:
        if text == PAGE_MARK:
            in_contents = False
            continue
        # 报告名/副标题/logo 字号更大（>=12），直接跳过
        if size >= 12:
            continue
        if in_contents:
            if size >= 10.5 or SECTION_HEADER.match(text):
                in_contents = False
            else:
                continue
        if CONTENTS_HEADER.search(text):
            in_contents = True
            continue
        is_header = size >= 10.5
        compact = text.replace(" ", "")
        all_caps = len(compact) > 3 and compact.isupper()
        if is_header:
            if all_caps or len(text) > 130 or SECTION_HEADER.match(text):
                continue  # 章节标题/页眉，跳过
            # 标题在 PDF 中可能折成下一行（indexes / assurance review 等），
            # 只有在上一条尚未出现正文时才并回标题。
            if current is not None and text[0].islower() and not current["body_en"]:
                current["headline_en"] += " " + text
                continue
            # 正文中的小标题/续行并入正文
            if current is not None and text[0].islower():
                current["body_en"].append(text)
                continue
            # 上一条还没有正文：视为折行标题，合并进上一条
            if stories and not stories[-1]["body_en"] and len(text) <= 90:
                stories[-1]["headline_en"] += " " + text
                current = stories[-1]
            else:
                current = {"headline_en": text, "body_en": []}
                stories.append(current)
        else:
            if TABLE_JUNK.match(text):
                continue
            if text.startswith("This Workspace is curated by") \
                    or text.startswith("Thermal Coal + Freight"):
                continue
            if len(text) <= 6 and NUMERIC_ONLY.match(text):
                continue
            if current is not None:
                current["body_en"].append(text)
            else:
                preamble.append(text)
    cleaned = []
    for s in stories:
        body = " ".join(s["body_en"]).strip()
        # PDF 文本层经常把目录、价格表和图表标签插入新闻正文，
        # 截断这些噪声，避免污染新闻阅读和机器翻译。
        body = STORY_NOISE.sub("", body)
        body = re.sub(r"\s+", " ", body).strip()
        s["body_en"] = body[:2600].rstrip()
        # 过滤仅由机构简称/表格残片形成的伪新闻卡片。
        title_words = re.findall(r"[A-Za-z]{2,}", s["headline_en"])
        if len(s["body_en"]) >= 80 and len(title_words) >= 3:
            cleaned.append(s)
    return cleaned, " ".join(preamble).strip()


def split_paragraphs(text):
    """把长文本切成段落（按句子断行，每段不超过约1500字符）"""
    if not text:
        return []
    sentences = re.split(r"(?<=[.!?])\s+", text)
    paras = []
    buf = ""
    for s in sentences:
        if len(buf) + len(s) > 1400 and buf:
            paras.append(buf.strip())
            buf = s
        else:
            buf += " " + s if buf else s
    if buf.strip():
        paras.append(buf.strip())
    return paras


def translate_one(text):
    """翻译一段英文为中文（Google 免费接口优先，MyMemory 备用）"""
    text = text.strip()
    if not text:
        return ""
    if TABLE_LINE.match(text):
        return text
    import urllib.parse
    import urllib.request
    q = urllib.parse.quote(text[:1800])
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=en"
           f"&tl=zh-CN&dt=t&q={q}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        parts = [seg[0] for seg in data[0] if seg and seg[0]]
        return "".join(parts).strip() or text
    except Exception:
        pass
    url2 = "https://api.mymemory.translated.net/get?q=" + q + "&langpair=en|zh-CN"
    try:
        with urllib.request.urlopen(url2, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        t = (data.get("responseData") or {}).get("translatedText")
        if t:
            return t.strip() or text
    except Exception:
        pass
    return text


def translate_paras(paras):
    """并发翻译段落列表"""
    out = [None] * len(paras)

    def worker(i, p):
        out[i] = translate_one(p)

    with ThreadPoolExecutor(max_workers=6) as ex:
        for i, p in enumerate(paras):
            ex.submit(worker, i, p)
    return [o or p for o, p in zip(out, paras)]


def get_report(date, kind, force=False):
    """返回 {name, date, kind, stories:[{headline_en,headline_zh,body_en,body_zh}]}"""
    meta = KINDS.get(kind)
    name = meta["name"] if meta else kind
    cache_file = CACHE_DIR / f"{kind}_{date}.json"
    try:
        from argus_ocr import get_report_lines, ocr_enabled, ocr_engine_name
    except Exception:
        get_report_lines = lambda *a, **k: (None, False)
        ocr_enabled = lambda: False
        ocr_engine_name = lambda: ""
    if not force and cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            if data.get("date") == date and data.get("kind") == kind \
                    and data.get("ver") == 11 \
                    and bool(data.get("ocr")) == ocr_enabled() \
                    and data.get("ocr_engine", "") == ocr_engine_name():
                return data
        except Exception:
            pass

    lines = None
    ocr_used = False
    if ocr_enabled():
        try:
            ocr_lines, ocr_used = get_report_lines(date, kind, force=force)
            if ocr_lines:
                lines = ocr_lines
        except Exception as e:
            print(f"[argus_reports] OCR 解析失败，回退本地提取: {e}")
            ocr_used = False
    if lines is None:
        lines = extract_lines(date, kind)
    if not lines:
        return {"ok": False, "error": "未找到该日报文件", "name": name,
                "date": date, "kind": kind, "stories": []}

    stories, preamble = build_stories(lines)
    # 翻译：每条新闻的标题 + 正文段落
    for s in stories:
        s["headline_zh"] = translate_one(s["headline_en"])
        paras = split_paragraphs(s["body_en"])
        s["body_zh"] = "\n".join(translate_paras(paras))
        s["body_paras"] = paras

    result = {
        "ok": True, "ver": 11, "name": name, "date": date, "kind": kind,
        "ocr": ocr_used,
        "ocr_engine": ocr_engine_name() if ocr_used else "",
        "stories": stories,
        "preamble_zh": translate_one(preamble) if preamble else "",
        "preamble_en": preamble,
    }
    try:
        cache_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return result


# ---------------- 价格表原图 ----------------


def get_price_pages(date, kind, force=False):
    """检测 PDF 中价格表页，返回 [{page, crop, box}]（page 为 1-based），带缓存"""
    f = find_file(date, kind)
    if not f:
        return []
    cache_file = PRICE_DIR / f"{kind}_{date}.pages.json"
    if not force and cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            if data.get("ver") == 1 and data.get("date") == date \
                    and data.get("kind") == kind:
                return data["pages"]
        except Exception:
            pass

    pages = []
    with pdfplumber.open(str(f)) as pdf:
        for i, page in enumerate(pdf.pages):
            words = page.extract_words()
            if not words:
                continue
            total = len(words)
            nonnum = sum(
                1 for w in words
                if not NUMERIC_ONLY.match(w["text"]) and len(w["text"]) > 1
            )
            tables = page.find_tables()
            tables = [
                t for t in tables
                if (t.bbox[2] - t.bbox[0]) * (t.bbox[3] - t.bbox[1]) >= 300
            ]
            tarea = 0.0
            box = None
            if tables:
                bx0 = min(t.bbox[0] for t in tables)
                btop = min(t.bbox[1] for t in tables)
                bx1 = max(t.bbox[2] for t in tables)
                bbot = max(t.bbox[3] for t in tables)
                tarea = sum(
                    (t.bbox[2] - t.bbox[0]) * (t.bbox[3] - t.bbox[1])
                    for t in tables)
                box = [round(bx0), round(btop), round(bx1), round(bbot)]
            ratio = nonnum / total
            area_ratio = tarea / (page.width * page.height)
            if len(tables) >= 5 and ratio < 0.75:
                pages.append({"page": i + 1, "crop": False, "box": None})
            elif area_ratio >= 0.04:
                pages.append({"page": i + 1, "crop": True, "box": box})
    try:
        cache_file.write_text(
            json.dumps({"ver": 1, "date": date, "kind": kind, "pages": pages}),
            encoding="utf-8")
    except Exception:
        pass
    return pages


def render_price_page(date, kind, page, crop=False, box=None):
    """把指定页（或页内表格区域）渲染成 PNG 原图（缓存），返回文件路径"""
    f = find_file(date, kind)
    if not f:
        raise FileNotFoundError(f"未找到 {date} {kind} 日报")
    out_dir = PRICE_DIR / f"{kind}_{date}"
    out_dir.mkdir(parents=True, exist_ok=True)
    if crop and box:
        bx = [float(v) for v in box]
        target = out_dir / (
            f"page_{int(page):02d}_crop_"
            f"{int(bx[0])}_{int(bx[1])}_{int(bx[2])}_{int(bx[3])}.png")
    else:
        target = out_dir / f"page_{int(page):02d}.png"
    if target.exists():
        return target
    with pdfplumber.open(str(f)) as pdf:
        if int(page) < 1 or int(page) > len(pdf.pages):
            raise ValueError(f"页码越界: {page}")
        pg = pdf.pages[int(page) - 1]
        if crop and box:
            bx = [float(v) for v in box]
            pg = pg.crop((bx[0] - 2, bx[1] - 2, bx[2] + 2, bx[3] + 2))
        im = pg.to_image(resolution=150)
        # 渲染到同盘 ASCII 临时路径，再移动到中文路径（Windows 渲染库兼容性）
        ascii_tmp = Path(os.environ.get("ARGUS_RENDER_TMP", str(REPORTS_ROOT / ".render_tmp")))
        ascii_tmp.mkdir(exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".png", dir=str(ascii_tmp))
        os.close(fd)
        im.save(tmp)
        os.replace(tmp, str(target))
    try:
        (ascii_tmp / os.path.basename(tmp)).unlink(missing_ok=True)
    except Exception:
        pass
    return target
