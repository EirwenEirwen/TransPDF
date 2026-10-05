"""组装发布包。

发布包结构：
  TransPDF发布包/
    TransPDF.exe          程序本体（单文件）
    runtime/              llama.cpp 推理引擎（只保留 llama-server 运行所需文件）
    models/               放置高质档 GGUF 模型（可选 --model 自动复制）
    使用说明.txt

用法:
  python tools/assemble_release.py --llama <llama.cpp目录>
  python tools/assemble_release.py --llama <llama.cpp目录> --model <模型.gguf>
  python tools/assemble_release.py --llama ... --no-compact   保留 runtime 全部文件
"""
import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# llama-server 运行必需的文件（其余 CLI 工具/测试组件全部裁掉）
RUNTIME_KEEP = {
    "llama-server.exe",
    "llama-server-impl.dll",
    "llama-common.dll",
    "llama.dll",
    "mtmd.dll",
    "ggml.dll",
    "ggml-base.dll",
    "ggml-vulkan.dll",
    "libomp.dll",
    "LICENSE-LLVM-OpenMP",
}

NOTICE = """TransPDF 论文离线翻译器 —— 使用说明
========================================

一、快速开始
  1. 下载模型（本包不含模型权重）
     模型约 12.4GB，超出 GitHub 单文件上限，需自行下载：
       - 在 Hugging Face 或 ModelScope 搜索：Qwen3-30B-A3B-Instruct-2507-GGUF
       - 下载其中的 Q3_K_S 量化版，文件名形如
         Qwen3-30B-A3B-Instruct-2507-Q3_K_S.gguf（约 12.4GB）
       - 把该 .gguf 文件放入本目录的 models 文件夹
     务必选 Instruct 版本；不要用 Base 版或 Thinking/Reasoning 版，翻译质量会明显变差。
  2. 双击 TransPDF.exe
  3. 选择 PDF 文件，点击「开始翻译」
  4. 翻译完成后点「打开译文」。译文与原文件同目录，文件名以"_中文翻译"结尾。

二、首次使用提示
  - 首次双击若出现 Windows SmartScreen 蓝色警告（本程序未做代码签名），
    点「更多信息」→「仍要运行」即可。
  - 解压路径不要含中文与空格，避免个别环境下路径解析问题。
  - 程序会自动检测内存与显卡；显存 12GB 以上可全速运行，纯内存运行较慢。
  - 首次翻译需要把模型加载进显存/内存，需要几十秒到几分钟，之后连续翻译无需重复加载。
  - 默认使用本地模型，翻译全部在本机完成，不联网，论文内容不会离开电脑。
  - 也可以改用在线接口翻译（主界面「翻译引擎」下拉 + 「高级设置 → 在线接口」配置）：
    在线大模型 / 百度翻译 / DeepL / 免费在线接口；「自动」模式会在本地不可用时切换在线。

三、常见问题
  - 提示"未找到模型"：确认 models 文件夹内有 .gguf 文件，或在「高级设置」中指定模型目录。
  - 提示"未找到 llama.cpp"：确认 runtime 文件夹内有 llama-server.exe，或在「高级设置」指定。
  - 扫描件（图片型 PDF）无法翻译：本工具只处理有文字层的 PDF。
  - 加密的 PDF 请先解除密码。
  - 个别短标题（如 References）可能保留英文原文：模型对极短输入偶尔会不翻译，
    程序会保留原文而不是写入错误内容。

四、目录说明
  runtime/   llama.cpp 推理引擎（llama-server.exe 与动态库）
  models/    GGUF 模型权重（需自行下载放入）
  config.json 程序配置（自动生成）

五、许可
  本程序源码以 MIT 许可发布；随包的 llama.cpp 运行时与思源黑体字库
  遵循各自的原始许可（见 runtime/LICENSE-LLVM-OpenMP 等文件）。
"""


def compact_runtime(dst_runtime):
    """裁剪 runtime：删掉 CLI 工具与测试组件，只留 llama-server 运行所需。

    保留 ggml-cpu-*.dll 全部架构后端（约 22MB）——llama.cpp 会在运行时按 CPU
    指令集自动挑选，删掉会让老机器退回通用实现、推理明显变慢。
    """
    removed = 0
    for name in os.listdir(dst_runtime):
        p = os.path.join(dst_runtime, name)
        if os.path.isfile(p) and name not in RUNTIME_KEEP \
                and not name.startswith("ggml-cpu-"):
            os.remove(p)
            removed += 1
    return removed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llama", required=True, help="llama.cpp 目录（含 llama-server.exe）")
    ap.add_argument("--model", help="可选：要复制进 models 的 GGUF 文件")
    ap.add_argument("--out", help="发布包输出目录（默认：项目根/release/TransPDF发布包）")
    ap.add_argument("--no-compact", action="store_true", help="保留 runtime 全部文件")
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    exe = os.path.join(root, "dist", "TransPDF.exe")
    if not os.path.isfile(exe):
        sys.exit("先运行 PyInstaller 构建 dist/TransPDF.exe（见 build.bat）")

    # 默认输出到项目自己的 release/ 下，不再硬编码本机 D 盘路径
    out = args.out or os.path.join(root, "release", "TransPDF发布包")
    os.makedirs(os.path.join(out, "models"), exist_ok=True)
    print("复制程序本体…")
    shutil.copy2(exe, os.path.join(out, "TransPDF.exe"))

    dst_runtime = os.path.join(out, "runtime")
    if os.path.isdir(dst_runtime):
        shutil.rmtree(dst_runtime)
    print("复制 llama.cpp 运行时…")
    shutil.copytree(args.llama, dst_runtime,
                    ignore=shutil.ignore_patterns("*.log", "bin", "build", "scripts"))
    if not args.no_compact:
        n = compact_runtime(dst_runtime)
        print(f"已裁剪 runtime 冗余文件 {n} 个（仅保留 llama-server 运行所需）")

    if args.model:
        print("复制模型…")
        shutil.copy2(args.model, os.path.join(out, "models", os.path.basename(args.model)))

    with open(os.path.join(out, "使用说明.txt"), "w", encoding="utf-8") as f:
        f.write(NOTICE)

    total = 0
    for r, _, files in os.walk(out):
        for fn in files:
            total += os.path.getsize(os.path.join(r, fn))
    print(f"发布包已完成：{out}")
    print(f"总体积：{total / 1024 ** 3:.2f} GB")


if __name__ == "__main__":
    main()
