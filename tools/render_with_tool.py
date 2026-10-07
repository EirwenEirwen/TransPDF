# 来源：manga-translate skill，随 TransPDF 发布包分发使用
#!/usr/bin/env python3
"""第6步渲染驱动: 让 manga-translator-ui 按"已写回译文的 JSON"渲染成图。

译文由 build_output.py 写进 <图>_translations.json 各 region 的 translation 字段后,
用本脚本渲染(等价于工具编辑器的"从 JSON 渲染", CLI 没有对应开关所以走 Python API)。

必须在 manga-translator-ui 仓库根目录下用它的 venv 运行:
  cd /d/tools/manga-translator-ui && PYTHONUTF8=1 \
    uv run --no-sync python "$SKILL/scripts/render_with_tool.py" \
    --images <图片文件或目录> --config <渲染配置.json> -o <输出目录> [--overwrite]

渲染配置要点: inpainter 选真模型(如 lama_large, 负责擦原文), render.renderer 用默认,
render.font_family 用 fonts/ 里存在的中文字体(如 msyh.ttc), cli.use_gpu 按机器实际。
"""

import argparse
import asyncio
import json
import os
import sys


def main():
    ap = argparse.ArgumentParser(description="按已写回译文的 JSON 渲染成图")
    ap.add_argument("--images", required=True, help="图片文件或目录")
    ap.add_argument("--config", required=True, help="渲染配置 JSON(与 local 模式同格式)")
    ap.add_argument("-o", "--output", required=True, help="输出目录")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--skip-font-scaling", action="store_true",
                    help="保留检测时的字号(默认按中文重新智能排版)")
    args = ap.parse_args()

    repo = os.getcwd()
    if os.path.basename(repo) != "manga-translator-ui" and not os.path.isdir(os.path.join(repo, "manga_translator")):
        sys.exit("请在 manga-translator-ui 仓库根目录运行本脚本")

    sys.path.insert(0, repo)
    from manga_translator import Config, Context, MangaTranslator
    from manga_translator.utils.path_manager import find_json_path

    config_dict = json.load(open(args.config, encoding="utf-8"))
    translator_params = config_dict.get("cli", {}).copy()
    font_family = config_dict.get("render", {}).get("font_family")
    if font_family:
        translator_params["font_family"] = font_family
    translator = MangaTranslator(params=translator_params)

    explicit_keys = ("render", "upscale", "translator", "detector", "colorizer", "inpainter", "ocr")
    config_for_translate = {k: v for k, v in config_dict.items() if k in explicit_keys}
    for key in ("kernel_size", "mask_dilation_offset", "force_simple_sort"):
        if key in config_dict:
            config_for_translate[key] = config_dict[key]
    manga_config = Config(**config_for_translate)

    images = [args.images] if os.path.isfile(args.images) else [
        os.path.join(args.images, f) for f in sorted(os.listdir(args.images))
        if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
    os.makedirs(args.output, exist_ok=True)

    ok = fail = 0
    for img_path in images:
        img_path = os.path.abspath(img_path)
        name = os.path.basename(img_path)
        out_path = os.path.join(args.output, name)
        if os.path.exists(out_path) and not args.overwrite:
            print(f"[skip] {name} 已存在")
            continue
        json_path = find_json_path(img_path)
        if not json_path:
            print(f"[fail] {name}: 找不到对应 *_translations.json (先跑 build_output.py)")
            fail += 1
            continue

        async def run():
            import numpy as np
            from PIL import Image
            from manga_translator.utils.generic import dump_image
            regions, mask, mask_refined, skip_scaling, skip_repl, parse_failures = \
                translator._load_text_and_regions_from_file(img_path, manga_config)
            if regions is None:
                raise FileNotFoundError(f"JSON 加载失败: {json_path}")
            if parse_failures:
                raise ValueError(f"{parse_failures} 个 region 解析失败, 拒绝渲染以免丢区域")
            translator._prepare_loaded_regions(regions, use_text_as_translation=True)

            img_pil = Image.open(img_path)
            img_rgb = np.array(img_pil.convert("RGB"))
            ctx = Context()
            ctx.input = img_pil
            ctx.image_name = img_path
            ctx.img_rgb = img_rgb
            ctx.img_colorized = img_rgb
            ctx.upscaled = img_rgb
            ctx.mask_raw = None
            ctx.bubble_mask = None
            ctx.text_regions = regions
            ctx.config = manga_config
            ctx.from_lang = "auto"
            skip_scaling = bool(args.skip_font_scaling) or bool(skip_scaling)
            ctx.skip_font_scaling = skip_scaling
            if mask is not None and mask_refined:
                ctx.mask = mask
            else:
                ctx.mask = await translator._run_mask_refinement(manga_config, ctx)
            ctx.img_inpainted = await translator._run_inpainting(manga_config, ctx)
            ctx.img_rendered = await translator._run_text_rendering(
                manga_config, ctx, skip_font_scaling=skip_scaling, skip_text_replacements=False)
            ctx.result = dump_image(
                img_pil, ctx.img_rendered, getattr(ctx, "img_alpha", None),
                mask=ctx.mask, render_alpha=getattr(ctx, "img_render_alpha", None))
            save_info = {"output_folder": os.path.abspath(args.output), "format": None}
            translator._save_and_cleanup_context(ctx, save_info, manga_config, "SKILL-RENDER")
            return bool(ctx.success)

        try:
            asyncio.run(run())
            print(f"[ok] {name} -> {out_path}")
            ok += 1
        except Exception as e:
            print(f"[fail] {name}: {e}")
            fail += 1
    print(f"完成: 成功 {ok}, 失败 {fail}")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
