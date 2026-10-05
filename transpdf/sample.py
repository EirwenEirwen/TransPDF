"""生成用于测试的学术风格样例 PDF（英文论文 + 少量中文行 + 公式符号行）。"""
import os

import pymupdf


def _sys_cjk_font():
    windir = os.environ.get("WINDIR", r"C:\Windows")
    for name in ("msyh.ttc", "simhei.ttf", "simsun.ttc"):
        p = os.path.join(windir, "Fonts", name)
        if os.path.exists(p):
            return p
    return None


ABSTRACT = ("Deep learning has transformed the way machines process natural language. "
            "In this survey, we review recent progress on neural machine translation with "
            "particular emphasis on document-level translation, terminology consistency, "
            "and layout-preserving generation. We discuss the trade-offs between autoregressive "
            "decoding quality and inference cost on consumer hardware, and we identify open "
            "problems in evaluating translations of scientific text.")

INTRO1 = ("Scientific papers are written in a highly specialized register: sentences are long, "
          "terminology is dense, and mathematical notation is interleaved with prose. Translating "
          "such documents therefore requires more than sentence-level adequacy; it demands domain "
          "awareness and stable rendering of symbols, citations, and cross-references.")

INTRO2 = ("Existing translation services rely on cloud infrastructure, which raises two practical "
          "concerns for researchers: unpublished manuscripts may leave the local machine, and "
          "network access is not always available. An offline pipeline that runs a large language "
          "model locally addresses both concerns while keeping quality competitive.")

METHOD = ("Our pipeline extracts text blocks with their geometry and style from the PDF, classifies "
          "each block as translatable, mathematical, or auxiliary, and then replaces the original "
          "text in place. A redaction step removes the source glyphs, and the translated Chinese "
          "text is written back into the same region with an embedded CJK font.")

CONCLUSION = ("We conclude that layout-preserving offline translation of academic papers is feasible "
              "on a single consumer workstation when an efficient mixture-of-experts model is used. "
              "Future work includes table reconstruction and bilingual side-by-side output.")

REFERENCES = [
    "[1] Vaswani A, et al. Attention is all you need. NeurIPS, 2017.",
    "[2] Wu Y, et al. Google's neural machine translation system. arXiv:1609.08144.",
    "[3] Kudo T, Richardson J. SentencePiece: A simple and language independent subword tokenizer. EMNLP, 2018.",
]

COL_LEFT = ("Two-column layouts are common in conference proceedings. The extraction step must "
            "therefore respect the column structure, otherwise translated paragraphs would be "
            "merged across columns. Block-level geometry keeps each paragraph inside its own "
            "column box, which is why the write-back stage can preserve readability without "
            "full document reflow.")

COL_RIGHT = ("Formulas pose a different challenge. Glyphs from math fonts must never be sent to the "
             "translation model, because symbols such as summation or gradient operators would be "
             "rewritten into meaningless Chinese characters. Our classifier therefore skips any "
             "line whose font set or symbol density indicates mathematical content.")


def make_sample(path):
    cjk = _sys_cjk_font()
    doc = pymupdf.open()
    page = doc.new_page()  # A4

    page.insert_textbox(pymupdf.Rect(60, 50, 535, 100),
                        "Layout-Preserving Offline Translation of Academic Papers "
                        "with Local Large Language Models",
                        fontsize=15, fontname="tiro", align=1)
    page.insert_textbox(pymupdf.Rect(60, 100, 535, 120),
                        "Alice Smith, Bob Zhang, and Carol Lee - Journal of Applied NLP, 2025",
                        fontsize=9.5, fontname="tiro", align=1)

    y = 130
    page.insert_textbox(pymupdf.Rect(60, y, 535, y + 22), "Abstract", fontsize=12,
                        fontname="tibo")
    page.insert_textbox(pymupdf.Rect(60, y + 22, 535, y + 104), ABSTRACT,
                        fontsize=10, fontname="tiro")
    y += 112
    page.insert_textbox(pymupdf.Rect(60, y, 535, y + 22), "1. Introduction", fontsize=12,
                        fontname="tibo")
    page.insert_textbox(pymupdf.Rect(60, y + 22, 535, y + 100), INTRO1,
                        fontsize=10, fontname="tiro")
    y += 100
    page.insert_textbox(pymupdf.Rect(60, y, 535, y + 78), INTRO2,
                        fontsize=10, fontname="tiro")
    y += 84
    # 公式符号行（应被识别并跳过）
    if cjk:
        page.insert_textbox(pymupdf.Rect(60, y, 535, y + 18),
                            "∑ ∫ √ α β λ θ ≤ ≥ ∞ ∂ ∇ ∈ ∀ × ÷ ≈",
                            fontsize=10, fontfile=cjk, fontname="cjkfont")
    y += 24
    # URL 行（应被跳过）
    page.insert_textbox(pymupdf.Rect(60, y, 535, y + 18),
                        "Code and datasets: https://github.com/example/paper-trans",
                        fontsize=9, fontname="cour")
    y += 20
    # 中文行（应保持不变）
    if cjk:
        page.insert_textbox(pymupdf.Rect(60, y, 535, y + 18),
                            "这一行是中文内容，应当保持原样，不做翻译。",
                            fontsize=10, fontfile=cjk, fontname="cjkfont2")
    y += 24
    page.insert_textbox(pymupdf.Rect(60, y, 535, y + 22), "2. Method", fontsize=12,
                        fontname="tibo")
    page.insert_textbox(pymupdf.Rect(60, y + 22, 535, y + 104), METHOD,
                        fontsize=10, fontname="tiro")
    y += 106
    page.insert_textbox(pymupdf.Rect(60, y, 535, y + 22), "3. Conclusion", fontsize=12,
                        fontname="tibo")
    page.insert_textbox(pymupdf.Rect(60, y + 22, 535, y + 82), CONCLUSION,
                        fontsize=10, fontname="tiro")
    y += 84
    page.insert_textbox(pymupdf.Rect(60, y, 535, y + 22), "References", fontsize=12,
                        fontname="tibo")
    ry = y + 22
    for ref in REFERENCES:
        page.insert_textbox(pymupdf.Rect(60, ry, 535, ry + 26), ref,
                            fontsize=9, fontname="tiro")
        ry += 26

    # 第二页：双栏
    p2 = doc.new_page()
    p2.insert_textbox(pymupdf.Rect(60, 50, 535, 74), "4. Discussion on Layout",
                      fontsize=12, fontname="tibo", align=1)
    p2.insert_textbox(pymupdf.Rect(50, 90, 290, 400), COL_LEFT, fontsize=9.5, fontname="tiro")
    p2.insert_textbox(pymupdf.Rect(305, 90, 545, 400), COL_RIGHT, fontsize=9.5, fontname="tiro")
    p2.insert_textbox(pymupdf.Rect(50, 770, 545, 786), "2",
                      fontsize=9, fontname="tiro", align=1)

    doc.save(path)
    doc.close()
    return path



# --------------------------------------------------------------------------
# 计算机 / 游戏 AI 风格样例（用于领域自适应测试）
# --------------------------------------------------------------------------

CS_ABS = ("Real-time strategy (RTS) games such as StarCraft II expose a huge action space and "
          "partial observability, making deep reinforcement learning expensive to train. We propose "
          "an efficient multi-agent framework that combines a navigation-mesh based pathfinding "
          "layer with a micromanagement policy trained by self-play. On a dataset of 12,000 matches, "
          "our agent raises the win rate from 54.3% to 71.8% against built-in AI at an APM budget of "
          "180, while improving training throughput by 2.3x on a single GPU. An ablation study shows "
          "that action masking and reward shaping each contribute to the final Elo rating.")

CS_INTRO = ("Recent systems such as OpenAI Five and AlphaStar demonstrated that deep reinforcement "
            "learning can reach expert play in Dota 2 and StarCraft II. However, deploying such "
            "agents inside a modern game engine raises engineering issues: the training loop must "
            "call env.step(action) through a Python binding, respect action_mask constraints, and "
            "keep the tick rate stable at 60 Hz. With a learning rate of 3e-4 and a batch size of "
            "4096, fine-tuning on 8 GPUs still takes several days. This paper focuses on reducing "
            "that cost without sacrificing gameplay quality.")

CS_METHOD = ("Our policy network conditions on the observation space of unit features and uses a "
             "transformer encoder. Rollback netcode and lag compensation keep online evaluation fair "
             "under latency. The HUD shows the current build order; NPC units are controlled by the "
             "same policy. For matchmaking we use Elo rating with a matchmaking rating (MMR) decay. "
             "本行是中文，应当保持不变。See https://example.com/code for the code.")

CS_CONCL = ("We presented a compute-efficient pipeline for RTS micromanagement. Procedural content "
            "generation of maps and playtesting with human players are left for future work. We "
            "believe similar ideas transfer to first-person shooter (FPS) and MOBA titles such as "
            "League of Legends, where matchmaking and player retention are key concerns.")

CS_FIGCAPTION = ("Fig. 2. Evaluation results. (a) Win rate vs. training steps; (b) APM distribution "
                 "of our agent compared with built-in AI; (c) Elo rating over self-play games.")


def make_cs_game_sample(path):
    cjk = _sys_cjk_font()
    doc = pymupdf.open()

    p1 = doc.new_page()
    p1.insert_textbox(pymupdf.Rect(50, 40, 545, 90),
                      "Efficient Multi-Agent Reinforcement Learning for Real-Time Strategy Games: "
                      "Pathfinding, Micromanagement, and Matchmaking at Scale",
                      fontsize=14, fontname="tibo", align=1)
    p1.insert_textbox(pymupdf.Rect(50, 92, 545, 108),
                      "Alice Smith, Bob Zhang, Carol Lee - Proceedings of the ACM on Computer Games and AI, 2026",
                      fontsize=9, fontname="tiro", align=1)
    p1.insert_textbox(pymupdf.Rect(50, 116, 545, 132), "Abstract", fontsize=11.5,
                      fontname="tibo")
    p1.insert_textbox(pymupdf.Rect(50, 136, 545, 236), CS_ABS, fontsize=9.5, fontname="tiro")
    p1.insert_textbox(pymupdf.Rect(50, 240, 545, 254),
                      "Keywords: reinforcement learning, real-time strategy games, pathfinding, "
                      "matchmaking, game engine", fontsize=9, fontname="tibo")
    y = 262
    p1.insert_textbox(pymupdf.Rect(50, y, 545, y + 20), "1. Introduction", fontsize=11.5,
                      fontname="tibo")
    p1.insert_textbox(pymupdf.Rect(50, y + 22, 545, y + 128), CS_INTRO, fontsize=9.5,
                      fontname="tiro")
    y += 136
    if cjk:
        p1.insert_textbox(pymupdf.Rect(50, y, 545, y + 16),
                          "本段文字为中文内容，应当保持原样，不做翻译。",
                          fontsize=9.5, fontfile=cjk, fontname="cjkfont1")
    y += 22
    p1.insert_textbox(pymupdf.Rect(50, y, 545, y + 14),
                      "Project page: https://example.com/rts-agent  (code released under MIT)",
                      fontsize=8.5, fontname="cour")
    y += 20
    p1.insert_textbox(pymupdf.Rect(50, y, 545, y + 20), "2. Related Work", fontsize=11.5,
                      fontname="tibo")
    p1.insert_textbox(pymupdf.Rect(50, y + 22, 545, y + 108),
                      "Classical game AI relies on heuristic search such as A* pathfinding on "
                      "navigation meshes, while learning-based approaches model micromanagement as a "
                      "sequential decision problem. Engine-level concerns such as rollback netcode, "
                      "lag compensation and stable tick rate are usually orthogonal to the learning "
                      "pipeline but matter for fair online evaluation.", fontsize=9.5,
                      fontname="tiro")

    p2 = doc.new_page()
    p2.insert_textbox(pymupdf.Rect(50, 40, 545, 58), "3. Method and Evaluation",
                      fontsize=11.5, fontname="tibo")
    p2.insert_textbox(pymupdf.Rect(50, 62, 545, 150), CS_METHOD, fontsize=9.5, fontname="tiro")
    p2.insert_textbox(pymupdf.Rect(50, 156, 545, 230), CS_FIGCAPTION, fontsize=9,
                      fontname="tiro")
    p2.insert_textbox(pymupdf.Rect(50, 238, 545, 256), "4. Conclusion", fontsize=11.5,
                      fontname="tibo")
    p2.insert_textbox(pymupdf.Rect(50, 258, 545, 330), CS_CONCL, fontsize=9.5, fontname="tiro")
    p2.insert_textbox(pymupdf.Rect(50, 770, 545, 784), "2", fontsize=9, fontname="tiro", align=1)

    doc.save(path)
    doc.close()
    return path


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "sample_paper.pdf"
    make_sample(out)
    print("样例已生成:", out)
