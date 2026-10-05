"""将 CFF 轮廓的 OTF 字体转换为 TrueType 轮廓的 TTF。

用途：MuPDF 的 HTML 排版引擎（insert_htmlbox）在嵌入 CFF 字体时
会出现字形索引错乱（提取文字正确但显示乱码），TTF 轮廓则正常。
因此发布包内置字体一律使用 TTF。

用法: python tools/otf2ttf.py <input.otf> <output.ttf>
"""
import sys
import time

from fontTools.ttLib import TTFont, newTable
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.pens.cu2quPen import Cu2QuPen

MAX_ERR = 1.0


def glyphs_to_quadratic(glyphs, max_err=MAX_ERR):
    quad = {}
    for gname in glyphs.keys():
        ttPen = TTGlyphPen(glyphs)
        cu2quPen = Cu2QuPen(ttPen, max_err, reverse_direction=True)
        glyphs[gname].draw(cu2quPen)
        quad[gname] = ttPen.glyph()
    return quad


def otf_to_ttf(ttFont):
    assert ttFont.sfntVersion == "OTTO" and "CFF " in ttFont
    glyphOrder = ttFont.getGlyphOrder()
    ttFont["loca"] = newTable("loca")
    ttFont["glyf"] = glyf = newTable("glyf")
    glyf.glyphOrder = glyphOrder
    glyf.glyphs = glyphs_to_quadratic(ttFont.getGlyphSet())
    del ttFont["CFF "]
    if "VORG" in ttFont:
        del ttFont["VORG"]
    glyf.compile(ttFont)

    hmtx = ttFont["hmtx"]
    for gname, glyph in glyf.glyphs.items():
        if hasattr(glyph, "xMin"):
            hmtx[gname] = (hmtx[gname][0], glyph.xMin)

    ttFont["maxp"] = maxp = newTable("maxp")
    maxp.tableVersion = 0x00010000
    maxp.maxZones = 1
    maxp.maxTwilightPoints = 0
    maxp.maxStorage = 0
    maxp.maxFunctionDefs = 0
    maxp.maxInstructionDefs = 0
    maxp.maxStackElements = 0
    maxp.maxSizeOfInstructions = 0
    maxp.maxComponentElements = max(
        (len(g.components) if hasattr(g, "components") else 0)
        for g in glyf.glyphs.values())
    maxp.compile(ttFont)

    post = ttFont["post"]
    post.formatType = 2.0
    post.extraNames = []
    post.mapping = {}
    post.glyphOrder = glyphOrder
    ttFont.sfntVersion = "\000\001\000\000"


def main():
    src, dst = sys.argv[1], sys.argv[2]
    t0 = time.time()
    font = TTFont(src)
    n = len(font.getGlyphOrder())
    print(f"转换 {src}（{n} 个字形）…")
    otf_to_ttf(font)
    font.save(dst)
    print(f"完成：{dst}（{time.time() - t0:.1f} 秒）")


if __name__ == "__main__":
    main()
