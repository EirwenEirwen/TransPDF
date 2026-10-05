# -*- coding: utf-8 -*-
"""TransPDF 改动验证：语法 -> 导入 -> 纯逻辑单测 -> 真实文件功能检查"""
import glob
import os
import py_compile
import socket
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _find_model():
    """定位 GGUF 模型：优先 models/，其次 release 包内，找不到返回空串。"""
    pats = [os.path.join(ROOT, "models", "*.gguf"),
            os.path.join(ROOT, "release", "*", "models", "*.gguf")]
    for pat in pats:
        hits = sorted(glob.glob(pat), key=os.path.getsize, reverse=True)
        if hits:
            return hits[0]
    return ""


MODEL = _find_model()
SAMPLE = os.path.join(ROOT, "test_out", "sample_paper.pdf")
_NEED_MODEL = bool(MODEL)


def _ensure_sample():
    """按需生成样例 PDF。

    test_out/ 不入库（构建产物），所以克隆仓库后它并不存在——不能依赖它，
    缺失时用 sample.make_sample 现生成一份到临时目录。
    """
    if os.path.isfile(SAMPLE):
        return SAMPLE
    from transpdf import sample as _sample
    out = os.path.join(_PYC_TMP, "sample_paper.pdf")
    _sample.make_sample(out)
    return out

import tempfile
_PYC_TMP = tempfile.mkdtemp(prefix="transpdf_pyc_")
FAIL = []
HARD = []          # 硬错误：脚本级/导入级/解析级


def check(name, fn):
    if name.startswith(("GGUF", "KV", "显存")) and not _NEED_MODEL:
        print(f"  [SKIP] {name} —— 未找到 GGUF 模型")
        return
    try:
        detail = fn()
        print(f"  [PASS] {name}" + (f" —— {detail}" if detail else ""))
    except AssertionError as e:
        FAIL.append(name)
        print(f"  [FAIL] {name} —— {e}")
    except Exception as e:
        HARD.append(name)
        print(f"  [ERROR] {name} —— {type(e).__name__}: {e}")
        traceback.print_exc()


print("=" * 72)
print("1) 语法编译全部源文件")
print("=" * 72)
targets = sorted(glob.glob(os.path.join(ROOT, "*.py"))
                 + glob.glob(os.path.join(ROOT, "transpdf", "*.py"))
                 + glob.glob(os.path.join(ROOT, "tools", "*.py")))
for p in targets:
    rel = os.path.relpath(p, ROOT)
    try:
        py_compile.compile(p, doraise=True, cfile=os.path.join(_PYC_TMP, rel.replace(os.sep, "_") + "c"))
        print(f"  [PASS] {rel}")
    except py_compile.PyCompileError as e:
        HARD.append(rel)
        print(f"  [ERROR] {rel} —— {e}")

print()
print("=" * 72)
print("2) 导入模块")
print("=" * 72)
sys.path.insert(0, ROOT)

hardware = localserver = engines = pdfproc = None


def _imp_hw():
    global hardware
    from transpdf import hardware as hw
    hardware = hw
    return f"capability 可调用={callable(hw.capability)}"


def _imp_ls():
    global localserver
    from transpdf import localserver as ls
    localserver = ls
    return f"实例记录文件={ls._LOCK_NAME}"


def _imp_en():
    global engines
    from transpdf import engines
    return f"引擎数={len(engines.ENGINE_LABELS)}"


def _imp_pp():
    global pdfproc
    from transpdf import pdfproc
    return "ok"


check("导入 transpdf.hardware", _imp_hw)
check("导入 transpdf.localserver", _imp_ls)
check("导入 transpdf.engines", _imp_en)
check("导入 transpdf.pdfproc", _imp_pp)


def _imp_gui():
    import tkinter  # noqa: F401
    from transpdf import gui
    return f"SettingsDialog 存在={hasattr(gui, 'SettingsDialog')}"


check("导入 transpdf.gui（需 tkinter）", _imp_gui)

print()
print("=" * 72)
print("3) 新增逻辑单测")
print("=" * 72)


def t_gguf():
    assert os.path.isfile(MODEL), f"模型不存在：{MODEL}"
    meta = hardware.gguf_meta(MODEL)
    assert meta.get("general.architecture") == "qwen3moe", f"arch={meta.get('general.architecture')}"
    assert meta.get("qwen3moe.block_count") == 48, meta
    assert meta.get("qwen3moe.attention.head_count_kv") == 4, meta
    return f"arch={meta['general.architecture']} 层数=48 kv_heads=4"


def t_kv():
    kv = hardware.kv_cache_bytes(MODEL, 8192)
    expect = 48 * 4 * (128 + 128) * 2 * 8192
    assert kv == expect, f"KV={kv} 期望={expect}"
    return f"KV cache(8192)={kv/1024**3:.2f} GB"


def t_vram():
    size = os.path.getsize(MODEL)
    reserve = max(int(size * 0.08), 512 * 1024 ** 2)
    need = hardware.vram_need(MODEL, 8192)
    assert need == size + hardware.kv_cache_bytes(MODEL, 8192) + reserve, need
    # 必须显著大于纯权重体积——这正是旧实现漏掉的部分
    assert need > size * 1.1, f"need={need} size={size}"
    return f"显存总需={need/1024**3:.2f} GB（权重 {size/1024**3:.2f} GB）"


def t_mode():
    cases = [
        (["--n-cpu-moe", "999", "-ngl", "999", "--jinja"], "拆分"),
        (["-ngl", "999", "--jinja"], "GPU 全量"),
        (["-ngl", "0", "--jinja"], "纯 CPU"),
        (["-ngl", "0"], "纯 CPU"),
    ]
    out = []
    for args, want in cases:
        got = localserver._describe_mode(args)
        assert want in got, f"args={args} 期望含“{want}” 实际“{got}”"
        out.append(f"{' '.join(args)}->{got}")
    return " / ".join(out)


def t_spawn_cmd():
    captured = {}

    class FakeProc:
        pid = 4242
        def poll(self):
            return None

    real = localserver.subprocess.Popen
    localserver.subprocess.Popen = lambda cmd, **kw: (captured.setdefault("cmd", cmd), FakeProc())[1]
    fh = None
    try:
        _proc, fh = localserver._spawn("llama-server.exe", "m.gguf", 18080,
                                       {"ctx": 8192}, ["-ngl", "999", "--jinja"])
    finally:
        localserver.subprocess.Popen = real
        if fh is not None:
            fh.close()
    cmd = captured.get("cmd")
    assert cmd, "未捕获到命令行"
    assert "--parallel" in cmd, f"缺少 --parallel：{cmd}"
    assert cmd[cmd.index("--parallel") + 1] == "1", cmd
    assert cmd[cmd.index("-c") + 1] == "8192", cmd
    assert "--port" in cmd and cmd[cmd.index("--port") + 1] == "18080", cmd
    return " ".join(cmd)


def t_attempts():
    cfg = {"local": {"gpu": True, "model_path": MODEL, "ctx": 8192}}
    att = localserver._launch_attempts(cfg, MODEL)
    assert att, "启动组合为空"
    assert att[-1] == ["-ngl", "0"], f"末位应为纯 CPU 兜底：{att}"
    flat = [a for a in att]
    assert any("-ngl" in a for a in flat), att
    return f"{len(att)} 组：{flat}"


def t_port_pids():
    if os.name != "nt":
        return "跳过（非 Windows）"
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        pids = localserver._port_owner_pids(port)
        assert pids is not None, "netstat 解析失败（返回 None）"
        assert os.getpid() in pids, f"未识别出本进程 PID {os.getpid()}，实际 {pids}"
    finally:
        srv.close()
    return f"端口 {port} -> PID {sorted(pids)}"


def t_owns_port():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert localserver._owns_port(os.getpid(), port) is True
        assert localserver._owns_port(999999, port) is False, "错误 PID 应判为不拥有端口"
    finally:
        srv.close()
    return "归属判定正确"


def t_chunk():
    t = "机器学习" * 200           # 每字 3 字节
    parts = engines._chunk_utf8(t, 440)
    assert "".join(parts) == t, "切块后拼接与原文不一致（会丢字）"
    assert all(len(p.encode("utf-8")) <= 440 for p in parts), "存在超过字节上限的块"
    # 反证：旧实现按字符切，在非 ASCII 上必然超限
    old = [t[i:i + 440] for i in range(0, len(t), 440)]
    assert max(len(p.encode("utf-8")) for p in old) > 500, "旧实现未超限？测试前提不成立"
    return f"{len(parts)} 块，旧实现最大 {max(len(p.encode('utf-8')) for p in old)} 字节（>500 会被拒）"


def t_glossary():
    cfg = {"glossary": {f"k{i}": f"v{i}" for i in range(450)}}
    gloss, dropped = engines._load_glossary(cfg)
    assert len(gloss) == engines._GLOSSARY_MAX, len(gloss)
    assert dropped == 450 - engines._GLOSSARY_MAX, dropped
    logs = []
    sp = engines.build_system_prompt(cfg, None, logs.append)
    assert any("术语表" in s for s in logs), f"未提示截断：{logs}"
    assert sp.count("- k") == engines._GLOSSARY_MAX, "提示词内条数与加载上限不一致"
    return f"载入 {len(gloss)} 条、丢弃 {dropped} 条并已提示"


def t_domain_terms():
    """领域术语表：结构、渲染、覆盖规模、体积护栏。

    terms 从字符串改为「子领域 -> [(英文, 中文)]」的结构化词表后，
    build_system_prompt 必须经 _render_terms 渲染；漏渲染会把整个 dict
    的 repr 拼进提示词（模型看到的是 {'机器学习与训练范式': [...]}），
    这条断言就是为拦住该失效模式而写的。
    """
    total = {}
    for did, prof in engines.DOMAIN_PROFILES.items():
        assert isinstance(prof.get("label"), str) and prof["label"], f"{did} 缺 label"
        assert isinstance(prof.get("keywords"), list) and prof["keywords"], f"{did} 缺 keywords"
        groups = prof.get("terms")
        assert isinstance(groups, dict), \
            f"{did}.terms 应为 dict，实际 {type(groups).__name__}（旧结构）"
        pairs = [(en, zh) for g in groups.values() for en, zh in g]
        assert len(pairs) >= 100, f"{did} 术语仅 {len(pairs)} 条，覆盖不足"
        for en, zh in pairs:
            assert en.strip() and zh.strip(), f"{did} 存在空词条"
            assert engines._HAN_RE.search(zh), f"{did} 词条缺中文译名：{en} -> {zh}"
        assert prof.get("notes"), f"{did} 缺 notes（行内代码/专名规则）"
        total[did] = len(pairs)

        block, used, dropped = engines._render_terms(prof)
        assert used == len(pairs) and dropped == 0, \
            f"{did} 渲染丢条：used={used} dropped={dropped}，上限 {engines._DOMAIN_TERMS_MAX_CHARS}"
        assert prof["label"] in block, f"{did} 渲染结果缺标题"
        assert "{" not in block and "':" not in block, f"{did} 渲染混入 dict repr"
        for en, zh in pairs[:5]:
            assert f"{en} {zh}" in block, f"{did} 渲染缺词条 {en}"

    # 走一遍真实入口：同时命中两个领域时不得告警、体积不得失控
    logs = []
    sp = engines.build_system_prompt({"glossary": {}}, ["cs", "game"], logs.append)
    for did in ("cs", "game"):
        assert engines.DOMAIN_PROFILES[did]["label"] in sp, f"系统提示词缺 {did} 术语块"
    assert "{" not in sp and "':" not in sp, "系统提示词混入 dict repr"
    assert not logs, f"正常规模不应告警：{logs}"
    assert len(sp) < 8000, f"双领域提示词 {len(sp)} 字符，吃占 ctx=8192 过多"

    # 体积护栏：压低上限后必须整组丢弃并如实返回条数，不允许静默膨胀
    prof = engines.DOMAIN_PROFILES["cs"]
    full, full_used, _ = engines._render_terms(prof)
    old = engines._DOMAIN_TERMS_MAX_CHARS
    try:
        engines._DOMAIN_TERMS_MAX_CHARS = 300
        block, used, dropped = engines._render_terms(prof)
    finally:
        engines._DOMAIN_TERMS_MAX_CHARS = old
    assert dropped > 0 and 0 < used < full_used, "压低上限后未按预期整组截断"
    assert len(block) < len(full), "截断后体积未下降"

    # 检测：合成文本必须命中对应领域
    cs_txt = ("the transformer attention mechanism with GPU throughput and latency "
              "benchmark of a neural network")
    assert "cs" in engines.detect_domains(cs_txt), "计算机关键词未命中"
    gm_txt = ("gameplay pathfinding matchmaking game engine level design NPC "
              "cooldown skill tree boss fight")
    assert "game" in engines.detect_domains(gm_txt), "游戏关键词未命中"
    assert engines.domain_terms_count("cs") == total["cs"], "domain_terms_count 与词表不一致"
    return "；".join(f"{d} {n} 条" for d, n in total.items())


def t_pdfproc():
    import pymupdf
    path = _ensure_sample()
    doc = pymupdf.open(path)
    tasks = pdfproc.extract_page_tasks(doc[0], 0)
    doc.close()
    assert tasks, "首页未提取到任何翻译任务"
    for t in tasks:
        assert isinstance(t.color, int) and t.color >= 0, f"颜色异常：{t.color!r}"
        assert t.text.strip(), "存在空文本任务"
    return f"首页提取 {len(tasks)} 条任务，颜色均有效"


def t_src_regressions():
    gui_src = open(os.path.join(ROOT, "transpdf", "gui.py"), encoding="utf-8").read()
    assert 'engines.test_connection("local"' in gui_src, "P0-1 未修复（未传引擎 id）"
    assert "engines.test_connection(self.cfg)" not in gui_src, "P0-1 仍有旧写法"
    # 测试按钮必须异步：local 分支会走 15 秒长超时探针，主线程直调会冻界面
    assert "def _test_done(" in gui_src, "测试按钮未改为异步（长超时探针会冻界面）"
    assert 'engines.test_connection("local", cfg)' in gui_src, "测试按钮应传 deepcopy 的 cfg"
    assert 'engines.test_connection("local", self.cfg)' not in gui_src, \
        "测试按钮仍在 Tk 主线程直接调用 engines.test_connection"
    assert "copy.deepcopy(master.cfg)" in gui_src, "P2-1 未修复"
    assert "self.saved = True" in gui_src, "设置对话框仍无法区分保存/关闭"
    pp = open(os.path.join(ROOT, "transpdf", "pdfproc.py"), encoding="utf-8").read()
    assert "skipped_rot" not in pp, "P3-7 死变量仍在"
    mn = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    assert "last = [0]" not in mn, "P3-2 死变量仍在"
    assert "EXIT_ENV" in mn and "EXIT_FAIL" in mn, "P3-3 退出码未区分"
    ar = open(os.path.join(ROOT, "tools", "assemble_release.py"), encoding="utf-8").read()
    assert r"D:/TransPDF/release" not in ar, "P3-8 仍硬编码本机路径"
    en = open(os.path.join(ROOT, "transpdf", "engines.py"), encoding="utf-8").read()
    assert "def _test_local(cfg):\n    if localserver" in en, "P3-4 冗余导入仍在"
    ls = open(os.path.join(ROOT, "transpdf", "localserver.py"), encoding="utf-8").read()
    assert "def _is_our_orphan(" in ls, "归属判据缺失：会退化成乱杀外部服务"
    assert "不会自动结束" in ls, "缺少「不擅自结束别人进程」的保护"
    assert "def _read_lock(" in ls, "缺少实例记录读取"
    # 探针超时策略：复用必须宽容、就绪必须短（硬写数字，防止被改回）
    assert "_HEALTH_TIMEOUT = 15.0" in ls, "复用探测超时未放宽到 15s"
    assert "_READY_TIMEOUT = 5.0" in ls, "就绪探测超时未固定为 5s"
    return "全部关键改动已确认落到源码"


check("GGUF 元数据解析", t_gguf)
check("KV cache 估算", t_kv)
check("显存需求估算（含 KV + 缓冲）", t_vram)
check("运行模式标签（旧实现会误报）", t_mode)
check("启动命令行（--parallel 1）", t_spawn_cmd)
check("启动降级组合", t_attempts)
check("netstat 端口归属解析", t_port_pids)
check("端口归属判定", t_owns_port)
check("MyMemory 字节分块", t_chunk)
check("术语表上限与提示", t_glossary)
check("领域术语表（结构/渲染/覆盖/护栏）", t_domain_terms)
check("extract_page_tasks 真实 PDF 功能", t_pdfproc)
check("源码级回归断言", t_src_regressions)

print()
print("=" * 72)
print(f"结果：失败 {len(FAIL)} 项，硬错误 {len(HARD)} 项")
if FAIL:
    print("  失败：", FAIL)
if HARD:
    print("  硬错误：", HARD)
print("=" * 72)
sys.exit(1 if (FAIL or HARD) else 0)
