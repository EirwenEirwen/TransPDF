"""PDF 翻译处理核心。

流程：提取文本块 -> 识别需要翻译的外文块（跳过公式/网址/数字）->
并发调用翻译接口 -> 用红色抹除注记删除原文 -> 在原位置写回中文
（自动缩小字号以适配原区域，嵌入中文字体保证任何设备可读）。
"""
import html
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait

import pymupdf

from . import engines, localserver


# --------------------------------------------------------------------------
# 文本判定
# --------------------------------------------------------------------------

_HAN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")
_KANA = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
_HANGUL = re.compile(r"[\uac00-\ud7af]")
_LETTER = re.compile(r"[A-Za-z\u00c0-\u024f\u0370-\u03ff\u0400-\u04ff]")
_URL = re.compile(r"(https?://|www\.|ftp://|\S+@\S+\.\S+|doi:\s*\S+)", re.I)
_GREEK_OP = re.compile(r"[\u0370-\u03ff\u2010-\u2027\u2190-\u21ff\u2200-\u22ff\u2300-\u23ff\u25a0-\u25ff\u2a00-\u2aff]")
_MATH_FONT_PREFIX = (
    "cmmi", "cmsy", "cmex", "msam", "msbm", "mtmi", "mtsy", "mtextra",
    "eufm",
)
# 注：不把 Symbol/SymbolMT 当数学字体——排版软件常用它渲染引号、连字符等
# 正文标点，误判会把整段降级成逐行翻译。真公式行靠符号密度判定兜住。


def _looks_math(text, font_names):
    """整行像公式（数学字体 / 大量希腊字母与运算符）则不翻译。"""
    for f in font_names or []:
        fl = (f or "").lower()
        if any(fl.startswith(p) for p in _MATH_FONT_PREFIX):
            return True
    t = text.strip()
    if not t:
        return False
    marks = len(_GREEK_OP.findall(t))
    return marks / max(len(t), 1) > 0.3


def _char_stats(text):
    total = cjk = letters = 0
    for ch in text:
        if ch.isspace():
            continue
        total += 1
        if _HAN.match(ch):
            cjk += 1
        elif _LETTER.match(ch) or _KANA.match(ch) or _HANGUL.match(ch):
            letters += 1
    return total, cjk, letters


def is_foreign(text):
    """判断一段文本是否为需要翻译的外文。"""
    t = text.strip()
    if _URL.search(t):
        return False
    total, cjk, letters = _char_stats(t)
    if letters < 3 or total == 0:
        return False
    return cjk / total < 0.25


# --------------------------------------------------------------------------
# 字体定位
# --------------------------------------------------------------------------

def resource_dir():
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find_cjk_font(cfg=None):
    """返回可用的中文字体文件路径（必须是 TTF/ TTC 等 TrueType 轮廓，
    CFF 轮廓的 OTF 会被 MuPDF HTML 引擎渲染成乱码），找不到返回 None。"""
    if cfg and cfg.get("font_path") and os.path.exists(cfg["font_path"]):
        return cfg["font_path"]
    base = resource_dir()
    for name in ("SourceHanSansCN-Regular.ttf",):
        p = os.path.join(base, "fonts", name)
        if os.path.exists(p):
            return p
    exe_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else base
    for name in ("custom_font.ttf",):
        p = os.path.join(exe_dir, name)
        if os.path.exists(p):
            return p
    windir = os.environ.get("WINDIR", r"C:\Windows")
    fonts_dir = os.path.join(windir, "Fonts")
    for name in ("simhei.ttf", "msyh.ttc", "msyh.ttf", "Deng.ttf",
                 "simsun.ttc", "simfang.ttf", "simkai.ttf"):
        p = os.path.join(fonts_dir, name)
        if os.path.exists(p):
            return p
    return None


# --------------------------------------------------------------------------
# 文本块任务
# --------------------------------------------------------------------------

class Task:
    __slots__ = ("page_no", "kind", "rects", "insert_rect", "text",
                 "size", "color", "translation")

    def __init__(self, page_no, kind, rects, insert_rect, text, size, color):
        self.page_no = page_no
        self.kind = kind            # "block" 整段替换 / "lines" 仅替换部分行
        self.rects = rects          # 需要抹除的行矩形
        self.insert_rect = insert_rect
        self.text = text
        self.size = size
        self.color = color
        self.translation = None


def _join_lines(texts):
    """拼接多行文本：处理英文行尾连字符，其余用空格连接。"""
    out = ""
    for t in texts:
        t = t.strip()
        if not t:
            continue
        if not out:
            out = t
        elif out.endswith("-") and t and t[0].islower():
            out = out[:-1] + t
        else:
            out = out + " " + t
    return re.sub(r"\s+", " ", out).strip()


def extract_page_tasks(page, page_no):
    """提取一页中需要翻译的文本块任务。"""
    d = page.get_text("dict")
    tasks = []
    for b in d["blocks"]:
        if b.get("type") != 0:
            continue
        usable = []
        for ln in b.get("lines", []):
            if ln.get("dir", (1, 0))[0] < 0.9:  # 旋转/竖排文本，跳过保护
                continue
            spans = [s for s in ln.get("spans", []) if s.get("text", "").strip()]
            if not spans:
                continue
            text = "".join(s["text"] for s in spans)
            fonts = [s.get("font", "") for s in spans]
            size = max(s.get("size", 10.0) for s in spans)
            usable.append({"text": text, "bbox": ln["bbox"], "size": size,
                           "fonts": fonts, "color": spans[0].get("color", 0)})
        if not usable:
            continue

        block_text = _join_lines([u["text"] for u in usable])
        # 取文本最长行的主色作为译文颜色。只在 usable（可翻译行）里找，
        # 否则可能取到已被跳过的旋转/竖排行的颜色
        color = max(usable, key=lambda u: len(u["text"]))["color"]
        size = max(u["size"] for u in usable)

        if is_foreign(block_text):
            # 纯外文块：整段翻译（保证上下文连贯），公式样式的块整体跳过
            if not _looks_math(block_text, [f for u in usable for f in u["fonts"]]):
                rects = [pymupdf.Rect(u["bbox"]) for u in usable]
                insert = rects[0]
                for r in rects[1:]:
                    insert |= r
                if insert.width >= 4 and insert.height >= 3:
                    tasks.append(Task(page_no, "block", rects, insert,
                                      block_text, size, color))
            continue

        # 中外混排块：只翻译其中的纯外文行（避免连中文一起送翻）
        for u in usable:
            if is_foreign(u["text"]) and not _looks_math(u["text"], u["fonts"]):
                r = pymupdf.Rect(u["bbox"])
                if r.width >= 4 and r.height >= 3:
                    tasks.append(Task(page_no, "lines", [r], r, u["text"], u["size"], color))
    return tasks


# --------------------------------------------------------------------------
# 译文写回
# --------------------------------------------------------------------------

def _color_css(color_int):
    return "#{:06x}".format(color_int & 0xFFFFFF)


def _color_rgb(color_int):
    c = color_int & 0xFFFFFF
    return ((c >> 16 & 255) / 255.0, (c >> 8 & 255) / 255.0, (c & 255) / 255.0)


_font_cache = {}


def _get_font(path):
    """加载并缓存 pymupdf.Font（用于测宽与折行计算）。"""
    f = _font_cache.get(path)
    if f is None:
        f = pymupdf.Font(fontfile=path)
        _font_cache[path] = f
    return f


def _insert_zwsp(text):
    """在中日韩字符间插入零宽空格，帮助无空格文本换行。"""
    out = []
    prev_cjk = False
    for ch in text:
        cur_cjk = bool(_HAN.match(ch))
        if prev_cjk and (cur_cjk or _LETTER.match(ch)):
            out.append("\u200b")
        out.append(ch)
        prev_cjk = cur_cjk or bool(_LETTER.match(ch))
    return "".join(out)


def _wrap_text(text, font, size, width):
    """按显示宽度手工折行：拉丁单词保持完整，CJK 可在任意字符间断行。"""
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9.@'‑-]*|\s+|\S", text)
    space_w = font.text_length(" ", size)
    lines, cur, cur_w = [], "", 0.0
    for tok in tokens:
        if tok.isspace():
            if cur:
                cur += " "
                cur_w += space_w
            continue
        w = font.text_length(tok, size)
        if w > width and len(tok) > 1:  # 超宽长词按字符硬拆
            for ch in tok:
                cw = font.text_length(ch, size)
                if cur and cur_w + cw > width:
                    lines.append(cur.rstrip())
                    cur, cur_w = ch, cw
                else:
                    cur += ch
                    cur_w += cw
            continue
        if cur and cur_w + w > width:
            lines.append(cur.rstrip())
            cur, cur_w = tok, w
        else:
            cur += tok
            cur_w += w
    if cur.strip():
        lines.append(cur.rstrip())
    lines = lines or [""]
    # 中文避头尾：行首不能是句读/收尾标点，行尾不能是开引号开括号
    no_start = "、。，；：？！）】》」』”’…—·%"
    no_end = "（【《「『“‘"
    for i in range(1, len(lines)):
        moved = ""
        while lines[i] and lines[i][0] in no_start:
            moved += lines[i][0]
            lines[i] = lines[i][1:]
        if moved:
            lines[i - 1] += moved
        if lines[i - 1] and lines[i - 1][-1] in no_end and lines[i]:
            lines[i] = lines[i - 1][-1] + lines[i]
            lines[i - 1] = lines[i - 1][:-1]
    return [ln for ln in lines if ln != ""] or [""]


def write_task(page, task, font_path, log=None):
    """在页面上写回一条译文（假设原文已被抹除）。

    使用 insert_textbox + 手工折行（不用 HTML 排版引擎：Story 对部分
    中文字体的 ASCII 编码映射有缺陷），字体文件嵌入保证任何设备可读。
    """
    text = (task.translation or "").strip()
    if not text:
        return
    rect = pymupdf.Rect(task.insert_rect)
    rect.x0 -= 1.0
    rect.y0 -= 0.5
    rect.x1 += 1.5
    rect.y1 += 0.5
    size = min(max(task.size, 4.0), 42.0)
    rgb = _color_rgb(task.color)
    min_s = max(4.0, size * 0.5)

    if font_path:
        try:
            font = _get_font(font_path)
            while True:
                lines = _wrap_text(text, font, size, rect.width)
                rc = page.insert_textbox(rect, "\n".join(lines),
                                         fontname="tcf", fontfile=font_path,
                                         fontsize=size, color=rgb, align=0)
                if rc >= 0 or size <= min_s:
                    break
                size *= 0.93  # 放不下则缩小字号重排
            return
        except Exception as e:
            if log:
                log(f"[写回] 字体嵌入失败，改用内置 CJK 字体：{e}")
    # 兜底：内置 CJK 字体 + 零宽空格辅助换行
    body = _insert_zwsp(text)
    s = size
    while s >= min_s:
        rc = page.insert_textbox(rect, body, fontname="china-s",
                                 fontsize=s, color=rgb, align=0)
        if rc >= 0:
            return
        s *= 0.93
    page.insert_textbox(rect, body, fontname="china-s",
                        fontsize=min_s, color=rgb, align=0)


def _redact_page(page, tasks):
    for t in tasks:
        for r in t.rects:
            try:
                page.add_redact_annot(r, fill=False)
            except Exception:
                pass
    try:
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    except TypeError:
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def _batch_tasks(tasks, budget=2200, max_items=40):
    """机器翻译类接口按字符预算分批（它们按批量请求计费/限速）。"""
    batch, size = [], 0
    for t in tasks:
        n = len(t.text)
        if batch and (size + n > budget or len(batch) >= max_items):
            yield batch
            batch, size = [], 0
        batch.append(t)
        size += n
    if batch:
        yield batch


def translate_pdf(src, dst, cfg=None, log=None,
                  progress=None, cancel=None, engine="local"):
    """翻译 PDF 并输出到 dst。log/progress/cancel 为回调。

    engine：local（默认，离线）/ auto / llm / baidu / deepl / mymemory。"""
    cfg = cfg or {}
    log = log or (lambda s: None)
    progress = progress or (lambda f, m: None)
    try:
        doc = pymupdf.open(src)
    except Exception as e:
        raise RuntimeError(f"无法打开该文件，可能不是有效的 PDF：{e}") from e
    if doc.needs_pass:
        if doc.authenticate("") == 0:
            raise RuntimeError("该 PDF 已加密，请先解除密码保护后再翻译。")

    # 引擎预检：本地引擎的配置问题第一时间暴露
    if engine == "local":
        localserver.ensure_server(cfg, log)
    elif engine == "auto":
        if engines.local_available(cfg):
            localserver.ensure_server(cfg, log)
        else:
            log("[引擎] 本地模型不可用，将使用已配置的在线接口翻译。")
    n_pages = doc.page_count
    progress(0.0, f"正在提取文本（共 {n_pages} 页）…")
    pages_tasks = []
    for i in range(n_pages):
        if cancel and cancel.is_set():
            raise InterruptedError("已取消")
        try:
            pages_tasks.append(extract_page_tasks(doc[i], i))
        except Exception as e:
            log(f"[提取] 第 {i + 1} 页提取失败，跳过：{e}")
            pages_tasks.append([])
    all_tasks = [t for page in pages_tasks for t in page]
    total_chars = sum(len(t.text) for t in all_tasks)
    if not all_tasks:
        doc.save(dst, garbage=3, deflate=True)
        doc.close()
        return {"pages": n_pages, "blocks": 0, "chars": 0,
                "note": "未发现可翻译的外文文本（可能是扫描件或纯中文文档）。"}

    font_path = find_cjk_font(cfg)
    if font_path:
        log(f"[字体] 使用中文字体：{os.path.basename(font_path)}")
    else:
        log("[字体] 未找到中文字体文件，将使用内置 CJK 字体（部分阅读器可能显示异常）")

    # ---- 领域检测：按全文关键词自动注入领域术语规范 ----
    domains = engines.detect_domains(" ".join(t.text for t in all_tasks))
    if domains:
        labels = "、".join(
            f"{engines.DOMAIN_PROFILES[d]['label']}（{engines.domain_terms_count(d)} 条术语）"
            for d in domains
        )
        log(f"[领域] 检测到{labels}主题，已注入对应术语规范")
        cfg = dict(cfg)
        cfg["_domains"] = domains

    # ---- 翻译阶段：本地/大模型逐块请求，机器翻译类接口按字符预算分批 ----
    engines.reset_engine_failures()
    batch_engine = engine in ("baidu", "deepl", "mymemory")
    done_chars = [0]
    done_lock = threading.Lock()
    failures = []

    def _do_one(t):
        out = engines.translate_texts([t.text], cfg, log,
                                      domains=cfg.get("_domains"), engine=engine)[0]
        return [(t, out)]

    def _do_batch(batch):
        outs = engines.translate_texts([t.text for t in batch], cfg, log,
                                       domains=cfg.get("_domains"), engine=engine)
        return list(zip(batch, outs))

    def _report(nchars):
        with done_lock:
            done_chars[0] += nchars
            frac = 0.05 + 0.75 * done_chars[0] / max(total_chars, 1)
            progress(min(frac, 0.8), f"翻译中…（已完成 {done_chars[0]}/{total_chars} 字符）")

    units = list(_batch_tasks(all_tasks)) if batch_engine else [(t,) for t in all_tasks]
    progress(0.05, "翻译中…")
    pool = ThreadPoolExecutor(max_workers=3 if not batch_engine else 1)
    futs = {pool.submit(_do_one if not batch_engine else _do_batch, *u): u for u in units}
    try:
        # 短超时轮询代替 as_completed：取消标志能在一秒内被看到，
        # 而不是等某个最长 600 秒的请求结束才检查
        pending = set(futs)
        while pending:
            if cancel and cancel.is_set():
                raise InterruptedError("已取消")
            done, pending = wait(pending, timeout=0.5,
                                 return_when=FIRST_COMPLETED)
            for fut in done:
                try:
                    pairs = fut.result()
                except InterruptedError:
                    raise
                except Exception as e:
                    failures.append(str(e))
                    log(f"[翻译] 一批文本翻译失败，保留原文：{e}")
                    continue
                for t, out in pairs:
                    t.translation = out
                _report(sum(len(t.text) for t, _ in pairs))
    finally:
        # 取消时不能等 in-flight 请求（单块超时可达 600 秒）：丢弃未开始的任务，
        # 执行中的请求随守护线程自行结束，本函数立即返回
        cancelled = bool(cancel and cancel.is_set())
        pool.shutdown(wait=not cancelled, cancel_futures=cancelled)

    if failures and len(failures) >= len(units):
        raise RuntimeError("全部翻译请求均失败。" + "；".join(failures[:3]))

    # ---- 写回阶段 ----
    translated = sum(1 for t in all_tasks if t.translation)
    for i in range(n_pages):
        if cancel and cancel.is_set():
            raise InterruptedError("已取消")
        page_tasks = [t for t in pages_tasks[i] if t.translation]
        if not page_tasks:
            progress(0.8 + 0.2 * (i + 1) / n_pages, f"写回第 {i + 1}/{n_pages} 页…")
            continue
        _redact_page(doc[i], page_tasks)
        for t in page_tasks:
            write_task(doc[i], t, font_path, log)
        progress(0.8 + 0.2 * (i + 1) / n_pages, f"写回第 {i + 1}/{n_pages} 页…")

    tmp = dst + ".tmp"
    doc.save(tmp, garbage=3, deflate=True)
    doc.close()
    os.replace(tmp, dst)
    return {"pages": n_pages, "blocks": translated,
            "chars": total_chars, "note": ""}
