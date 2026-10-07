"""TransPDF 程序入口。

用法：
  TransPDF.exe                 打开图形界面
  TransPDF.exe --selftest      无界面自检（生成样例论文并离线翻译一遍）
  TransPDF.exe --input a.pdf [--output b.pdf]   命令行翻译
"""
import argparse
import os
import sys
import tempfile
import traceback

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 退出码：区分"环境不具备"与"流程执行失败"，否则自动化里没法判断该修环境还是查 bug
EXIT_OK = 0        # 成功
EXIT_ENV = 1       # 运行环境缺失：tkinter / 硬件 / 模型 / 引擎
EXIT_FAIL = 2      # 流程执行或结果校验失败


def _print_and_log(lines, s):
    lines.append(s)
    try:
        print(s, flush=True)
    except UnicodeEncodeError:  # GBK 控制台打不出部分字符
        print(s.encode("gbk", "replace").decode("gbk"), flush=True)


def run_selftest():
    """无界面自检：界面组件 -> 模型 -> 硬件 -> 样例论文翻译 -> 校验。返回退出码。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    report = []
    log = lambda s: _print_and_log(report, s)
    code = EXIT_OK
    try:
        log("== TransPDF 自检 ==")
        try:
            import tkinter  # noqa: F401
            log("[1/5] 界面组件 tkinter：OK")
        except Exception as e:
            log(f"[1/5] 界面组件 tkinter：缺失（{e}）")
            code = EXIT_ENV

        from transpdf.config import load_config
        from transpdf import hardware, localserver
        cfg = load_config()

        # 先定位模型：硬件判断要按该模型的真实体积 + KV cache 算，否则卡边时会误判
        model = localserver.resolve_model(cfg)
        if not model:
            log("[2/5] 翻译模型：未找到 GGUF（把模型放到 models 文件夹后重试）")
            log("自检到此为止（后续步骤需要模型）。")
            return EXIT_ENV
        log(f"[2/5] 翻译模型：{model}")

        ctx = int(cfg.get("local", {}).get("ctx") or 8192)
        ok, text, _ = hardware.capability(model, ctx)
        log(f"[3/5] 硬件检测：{'通过' if ok else '不通过'} —— {text}")
        if not ok:
            code = EXIT_ENV

        from transpdf.sample import make_sample
        from transpdf import pdfproc
        tmpdir = tempfile.mkdtemp(prefix="TransPDF_")
        sample = os.path.join(tmpdir, "sample_paper.pdf")
        make_sample(sample)
        out = os.path.join(tmpdir, "sample_paper_中文翻译.pdf")
        log(f"[4/5] 样例论文已生成：{sample}，开始离线翻译…")

        stats = pdfproc.translate_pdf(
            sample, out, cfg=cfg, log=log,
            progress=lambda f, m: None)
        log("[4/5] 翻译流程执行完毕。")

        import re

        import pymupdf
        doc = pymupdf.open(out)
        all_text = "".join(page.get_text() for page in doc)
        cjk = len(re.findall(r"[\u4e00-\u9fff]", all_text))
        pages = doc.page_count
        doc.close()

        # 判据用"文本块翻译成功率"而不是"中文字符数"：字符数可能来自少数成功块，
        # 反映不了"大部分内容其实没翻"。样例论文共 19 块，正常应 ≥18 块成功。
        ok_blocks = int(stats.get("blocks") or 0)
        all_blocks = int(stats.get("blocks_total") or 0)
        log(f"[5/5] 输出校验：{pages} 页，文本块 {ok_blocks}/{all_blocks} 已翻译，"
            f"中文字符 {cjk} 个。")
        if all_blocks and ok_blocks * 4 < all_blocks * 3:
            log(f"校验未通过：仅 {ok_blocks}/{all_blocks} 块完成翻译"
                f"（低于 75%，说明引擎多数请求未成功）。")
            code = EXIT_FAIL
        elif cjk < 100:
            log("校验未通过：译文中文字符过少。")
            code = EXIT_FAIL
        elif code == EXIT_OK:
            log(f"自检通过 [OK]  输出文件：{out}")
    except Exception as e:
        log(f"自检失败：{e}")
        log(traceback.format_exc())
        code = EXIT_FAIL
    finally:
        try:
            rp = os.path.join(tempfile.gettempdir(), "TransPDF_selftest.log")
            with open(rp, "w", encoding="utf-8") as f:
                f.write("\n".join(report))
            print(f"报告文件：{rp}", flush=True)
        except OSError:
            pass
    return code


def run_headless(src, dst=None, engine="local", do_figures=None):
    """命令行翻译。返回进程退出码。"""
    from transpdf.config import load_config
    from transpdf import pdfproc
    cfg = load_config()
    dst = dst or os.path.splitext(src)[0] + "_中文翻译.pdf"
    if do_figures is None:
        do_figures = bool(cfg.get("do_figures", False))

    def log(s):
        print(s, flush=True)

    def progress(f, m):
        print(f"[{int(f * 100):3d}%] {m}", flush=True)

    try:
        stats = pdfproc.translate_pdf(src, dst, cfg=cfg, log=log, progress=progress,
                                      engine=engine, do_figures=do_figures)
    except InterruptedError:
        print("已取消。", flush=True)
        return EXIT_FAIL
    except Exception as e:
        # 给用户一句人话，同时把 traceback 留到 stderr 便于排查
        print(f"翻译失败：{e}", flush=True)
        traceback.print_exc()
        return EXIT_FAIL
    print("完成：", dst, stats, flush=True)
    return EXIT_OK


def main():
    from transpdf import engines
    parser = argparse.ArgumentParser(description="TransPDF 论文翻译器")
    parser.add_argument("--selftest", action="store_true", help="运行无界面自检")
    parser.add_argument("--input", help="要翻译的 PDF 路径（命令行模式）")
    parser.add_argument("--output", help="输出 PDF 路径")
    parser.add_argument("--engine", default="local",
                        choices=list(engines.ENGINE_LABELS),
                        help="翻译引擎（默认 local 离线）")
    parser.add_argument("--figures", dest="figures", action="store_true",
                        default=None, help="同时翻译图片内文字（图表/插图）")
    parser.add_argument("--no-figures", dest="figures", action="store_false",
                        help="跳过图片内文字翻译")
    parser.add_argument("--version", action="store_true", help="显示版本")
    args = parser.parse_args()

    if args.version:
        from transpdf import __version__
        print("TransPDF", __version__)
        return EXIT_OK
    if args.selftest:
        return run_selftest()
    if args.input:
        return run_headless(args.input, args.output, args.engine, args.figures)

    from transpdf.gui import run_gui
    run_gui()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
