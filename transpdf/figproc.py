"""PDF 内嵌图片文字翻译（图表 / 插图 / 漫画页）。

外部引擎：manga-translator-ui（uv 管理，CLI 模式驱动），负责文字检测、OCR、
消字与渲染；翻译走本程序现有引擎（本地模型/在线接口均可）。

流程：提取 PDF 内嵌图片 -> 工具检测+OCR -> 可译性过滤 -> 批量翻译 ->
译文写回检测 JSON（不译区域剔除，等价 keep_original）-> 工具渲染成中文化
图片 -> replace_image 替换回 PDF -> 输出逐图对照表（渲染有风险时的保底交付）。

实测依据（manga-translate skill 2026-10-07）：
- 论文图 OCR 必须用 paddleocr（48px 是日文模型，英文会幻觉日文）
- 检测调参 detection_size 3072 + box_threshold 0.25 + text_threshold 0.4
- translator 配置必须 original（none 会清空 regions）
- 渲染 inpainter=lama_large + renderer=default + direction=horizontal
"""
import json
import os
import re
import subprocess
import tempfile

import pymupdf

from . import engines

# 图片过滤阈值：小于此显示尺寸的内嵌图（logo/图标/装饰线）不处理
_MIN_IMG_W = 160
_MIN_IMG_H = 100

DETECT_CONFIG = {
    "cli": {"save_text": True, "use_gpu": False},
    "translator": {"translator": "original", "target_lang": "CHS"},
    "detector": {"detector": "default", "detection_size": 3072,
                 "box_threshold": 0.25, "text_threshold": 0.4},
    "ocr": {"ocr": "paddleocr"},
    "inpainter": {"inpainter": "none"},
    "render": {"renderer": "none"},
}

RENDER_CONFIG = {
    "cli": {"save_text": True, "use_gpu": False},
    "translator": {"translator": "original", "target_lang": "CHS"},
    "detector": {"detector": "default", "detection_size": 3072,
                 "box_threshold": 0.25, "text_threshold": 0.4},
    "ocr": {"ocr": "paddleocr"},
    "inpainter": {"inpainter": "lama_large"},
    "render": {"renderer": "default", "font_family": "msyh.ttc",
               "direction": "horizontal"},
}

FIGURE_PROMPT = """你是学术论文图表翻译专家，负责把图表（figure）中 OCR 出来的英文文字翻译成简体中文。
这些文字通常是图标题、坐标轴标签、图例条目、标注或表格文字，翻译要求：
1. 轴标签与图例用精炼的名词短语（如 Accuracy→准确率，Training steps→训练步数），不要翻译成完整句子。
2. 单位、变量名、数学符号、纯数值、型号（如 RTX 4090）、模型名/数据集名（如 BERT、ImageNet）、
   量化格式（FP16/INT8）等一律原样保留。
3. OCR 文本可能有个别错字（如 Throughbut 应为 Throughput），按上下文最合理的理解翻译。
4. 图表中的 Ours / Our method 惯例译「本文方法」；Baseline 译「基线」。
5. 图内空间紧凑，译文必须不长于原文的合理范围，宁短勿长。
6. 只输出译文本身，一行一条，与输入顺序一致，不要输出任何解释或编号。"""


class FigureToolError(Exception):
    pass


# --------------------------------------------------------------------------
# 工具定位与调用
# --------------------------------------------------------------------------

def find_figure_tool(cfg):
    """定位 manga-translator-ui 目录：设置指定 > 常见位置探测。"""
    cands = []
    ft = cfg.get("figure", {}).get("tool_dir")
    if ft:
        cands.append(ft)
    from .config import app_dir
    cands += [
        os.path.join(app_dir(), "figuretools", "manga-translator-ui"),
        os.path.join(app_dir(), "figuretools"),
        r"D:\tools\manga-translator-ui",
        r"C:\tools\manga-translator-ui",
    ]
    for d in cands:
        if d and os.path.isfile(os.path.join(d, "pyproject.toml")) and \
                os.path.isdir(os.path.join(d, "manga_translator")):
            return os.path.normpath(d)
    return ""


def _run_detect(tool_dir, img_path, out_dir, config_path, log, timeout):
    """跑 manga_translator local CLI 的检测+OCR（translator=original 不翻译）。"""
    cmd = ["uv", "run", "--no-sync", "python", "-m", "manga_translator",
           "local", "-i", img_path, "-o", out_dir,
           "--config", config_path, "--overwrite"]
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    try:
        r = subprocess.run(cmd, cwd=tool_dir, env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
    except FileNotFoundError:
        raise FigureToolError("未找到 uv 命令（manga-translator-ui 依赖它运行），"
                              "请安装 uv 或确认其在 PATH 中。")
    except subprocess.TimeoutExpired:
        raise FigureToolError(f"图片处理超时（{timeout}s），已中止。")
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "")[-400:]
        raise FigureToolError(f"manga-translator-ui 运行失败：{tail}")


def _run_render(tool_dir, img_dir, out_dir, config_path, log, timeout):
    """按已写回译文的 JSON 渲染（render_with_tool.py 走工具 Python API，
    CLI local 没有对应开关）。脚本随本程序分发。"""
    from .pdfproc import resource_dir
    script = os.path.join(resource_dir(), "tools", "render_with_tool.py")
    if not os.path.isfile(script):  # 源码运行时在项目根 tools/ 下
        script = os.path.join(os.path.dirname(resource_dir()), "tools",
                              "render_with_tool.py")
    if not os.path.isfile(script):
        raise FigureToolError("未找到渲染驱动脚本 render_with_tool.py")
    cmd = ["uv", "run", "--no-sync", "python", script,
           "--images", img_dir, "--config", config_path,
           "-o", out_dir, "--overwrite"]
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    try:
        r = subprocess.run(cmd, cwd=tool_dir, env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
    except FileNotFoundError:
        raise FigureToolError("未找到 uv 命令（manga-translator-ui 依赖它运行）。")
    except subprocess.TimeoutExpired:
        raise FigureToolError(f"图片渲染超时（{timeout}s），已中止。")
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "")[-400:]
        raise FigureToolError(f"渲染失败：{tail}")


def _tool_json_path(img_dir, stem):
    return os.path.join(img_dir, "manga_translator_work", "json",
                        stem + "_translations.json")


# --------------------------------------------------------------------------
# 图片提取与过滤
# --------------------------------------------------------------------------

def find_figure_images(doc, log=None):
    """扫描 PDF，返回可处理的内嵌图片 [{page_no, xref, w, h}]。

    过滤：原始像素或页面显示尺寸过小的（logo/图标）、整页复用同 xref 只计一次。
    """
    log = log or (lambda s: None)
    seen = {}
    for page in doc:
        for img in page.get_images(full=True):
            xref = img[0]
            if xref in seen:
                continue
            w_px, h_px = int(img[2]), int(img[3])
            if w_px < _MIN_IMG_W or h_px < _MIN_IMG_H:
                continue
            try:
                rects = page.get_image_rects(xref)
            except Exception:
                rects = []
            disp = rects[0] if rects else None
            if disp is not None and (disp.width < _MIN_IMG_W or disp.height < _MIN_IMG_H):
                continue
            if disp is not None and disp.get_area() < page.rect.get_area() * 0.005:
                continue
            seen[xref] = {"page_no": page.number, "xref": xref,
                          "w": w_px, "h": h_px}
    imgs = list(seen.values())
    if imgs:
        log(f"[图片] 发现 {len(imgs)} 张可处理内嵌图片")
    else:
        log("[图片] 未发现含文字嫌疑的内嵌图片（或均过小）")
    return imgs


def _export_images(doc, imgs, workdir):
    """把内嵌图片统一导出为 PNG（Pixmap 重编码，规避特殊色彩空间）。"""
    paths = []
    for it in imgs:
        xref = it["xref"]
        try:
            pix = pymupdf.Pixmap(doc, xref)
            if pix.colorspace and pix.colorspace.n > 3:
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
            p = os.path.join(workdir, f"fig_x{xref}.png")
            pix.save(p)
            pix = None
            paths.append((it, p))
        except Exception:
            continue
    return paths


# --------------------------------------------------------------------------
# 可译性过滤与译文处理
# --------------------------------------------------------------------------

_NUM_OR_SYM = re.compile(r"^[\d\s.,:%/°×÷+\-–—=<>≤≥±()（）\[\]]+$")


def _translatable(text):
    """图表文字是否需要翻译：纯数值/符号串、单字符、已是中文的跳过。"""
    t = text.strip()
    if not t or len(t) <= 1:
        return False
    if _NUM_OR_SYM.match(t):
        return False
    if re.match(r"^(https?://|www\.)", t, re.I):
        return False
    from .pdfproc import is_foreign
    return is_foreign(t)


def _short_enough(src, dst):
    """图内空间紧：译文过长（超原文 1.6 倍且超 4 字）视为不合格。"""
    return len(dst) <= max(len(src) * 1.6, len(src) + 4)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def translate_figures(doc, cfg, log=None, progress=None, cancel=None,
                      engine="local", domains=None):
    """翻译 PDF 内嵌图片中的外文文字并替换回文档。

    返回统计 dict；工具缺失/无图片时安静跳过（不阻断文本翻译主流程）。
    """
    log = log or (lambda s: None)
    progress = progress or (lambda f, m: None)

    tool_dir = find_figure_tool(cfg)
    if not tool_dir:
        log("[图片] 未找到 manga-translator-ui（图片文字翻译需要该工具），本次跳过图片翻译。"
            "可在「高级设置 → 图片翻译」中指定工具目录。")
        return {"figures": 0, "regions": 0, "rendered": 0, "skipped": True}

    imgs = find_figure_images(doc, log)
    if not imgs:
        return {"figures": 0, "regions": 0, "rendered": 0, "skipped": False}

    workdir = tempfile.mkdtemp(prefix="TransPDF_fig_")
    exported = _export_images(doc, imgs, workdir)
    if not exported:
        log("[图片] 图片导出失败，跳过图片翻译。")
        return {"figures": 0, "regions": 0, "rendered": 0, "skipped": False}

    cfg_detect = os.path.join(workdir, "detect.json")
    with open(cfg_detect, "w", encoding="utf-8") as f:
        json.dump(DETECT_CONFIG, f)

    # ---- 阶段1：逐图检测+OCR ----
    table_rows = []          # (页码, 图, [(原文, 译文, 是否渲染)])
    n = len(exported)
    for i, (it, img_path) in enumerate(exported):
        if cancel and cancel.is_set():
            raise InterruptedError("已取消")
        progress(0.85 + 0.05 * (i + 1) / n, f"图片识别 第{i + 1}/{n} 张…")
        stem = os.path.splitext(os.path.basename(img_path))[0]
        try:
            _run_detect(tool_dir, img_path, os.path.join(workdir, "detect_out"),
                        cfg_detect, log, timeout=900)
        except FigureToolError as e:
            log(f"[图片] 第{it['page_no'] + 1}页图片识别失败，跳过该图：{e}")
            continue
        jp = _tool_json_path(workdir, stem)
        if not os.path.isfile(jp):
            log(f"[图片] 第{it['page_no'] + 1}页未产出检测 JSON，跳过。")
            continue
        with open(jp, "r", encoding="utf-8") as f:
            raw = json.load(f)
        inner = raw.get(img_path) or next(iter(raw.values()), {})
        regions = inner.get("regions") or []
        table_rows.append((it, img_path, jp, regions))

    # ---- 阶段2：汇总可译文本批量翻译 ----
    jobs = []  # (img_entry, json_path, region)
    for it, img_path, jp, regions in table_rows:
        for r in regions:
            text = (r.get("text") or "").strip()
            if _translatable(text):
                jobs.append((it, jp, r, text))
    if not jobs:
        log("[图片] 图片中未发现需要翻译的外文文字。")
        return {"figures": len(table_rows), "regions": 0, "rendered": 0,
                "skipped": False}

    log(f"[图片] 共 {len(jobs)} 条图内文字待译，开始翻译…")
    if domains is None:
        domains = cfg.get("_domains")
    results = engines.translate_texts(
        [t for _, _, _, t in jobs], cfg, log, domains=domains, engine=engine,
        prompt=FIGURE_PROMPT)

    # 分组写回：translation==原文 或未过长度检查的 region 从渲染 JSON 剔除
    rendered_files = []
    table = []  # 对照表 (页码, 原文, 译文, rendered?)
    by_json = {}
    for (it, jp, r, src), dst in zip(jobs, results):
        dst = (dst or "").strip()
        ok = dst and dst != src and _plausible_fig(src, dst) and _short_enough(src, dst)
        table.append((it["page_no"], src, dst if ok else "（保留原文）", ok))
        if ok:
            r["translation"] = dst
            by_json.setdefault(jp, []).append(r)

    # ---- 阶段3：渲染并替换回 PDF ----
    cfg_render = os.path.join(workdir, "render.json")
    with open(cfg_render, "w", encoding="utf-8") as f:
        json.dump(RENDER_CONFIG, f)
    xref_by_json = {}
    for (it, jp, r, src) in jobs:          # 记录 JSON -> xref 映射
        xref_by_json.setdefault(jp, it["xref"])
    for i, (jp, keep_regions) in enumerate(by_json.items()):
        if cancel and cancel.is_set():
            raise InterruptedError("已取消")
        progress(0.90 + 0.08 * (i + 1) / len(by_json),
                 f"图片渲染 第{i + 1}/{len(by_json)} 张…")
        # 重写 JSON：只保留要渲染的区域（keep_original 剔除策略）
        with open(jp, "r", encoding="utf-8") as f:
            raw = json.load(f)
        key = next(iter(raw))
        raw[key]["regions"] = keep_regions
        with open(jp, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
        try:
            _run_render(tool_dir, workdir, os.path.join(workdir, "render_out"),
                        cfg_render, log, timeout=1200)
        except FigureToolError as e:
            log(f"[图片] 渲染失败（保留原图，译文见对照表）：{e}")
            continue
        # 找到渲染产物并替换回 PDF
        xref = xref_by_json.get(jp)
        rendered = os.path.join(workdir, "render_out",
                                os.path.basename(jp).replace(
                                    "_translations.json", ".png"))
        if os.path.isfile(rendered):
            try:
                page_no = _page_of_xref(doc, xref)
                doc[page_no].replace_image(xref, filename=rendered)
                rendered_files.append(xref)
            except Exception as e:
                log(f"[图片] 替换回 PDF 失败（xref={xref}）：{e}")

    progress(0.98, "图片翻译完成。")
    return {"figures": len(table_rows), "regions": len(jobs),
            "rendered": len(rendered_files), "table": table,
            "skipped": False}


def _plausible_fig(src, dst):
    """图内文字的合理性检查（比正文宽松：短标签也要过）。"""
    return engines._plausible(src, dst)


def _page_of_xref(doc, xref):
    """xref 首次出现的页码（replace_image 需要 Page 对象）。"""
    for page in doc:
        try:
            if any(im[0] == xref for im in page.get_images(full=True)):
                return page.number
        except Exception:
            continue
    return 0


def write_table_md(table, dst_path):
    """输出逐图对照表 markdown（渲染失败/未渲染项的保底交付）。"""
    lines = ["# 图片文字翻译对照表", "",
             "| 页码 | 原文 | 译文 | 是否已写回图内 |", "|---|---|---|---|"]
    for page_no, src, dst, rendered in table:
        lines.append(f"| {page_no + 1} | {src} | {dst} | "
                     f"{'是' if rendered else '否（保留原文）'} |")
    with open(dst_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return dst_path
