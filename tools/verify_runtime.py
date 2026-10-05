# -*- coding: utf-8 -*-
"""TransPDF localserver 运行时验证（第 2 层）。

只测"行为"，不测"文案"：
  1. 关窗中断：模型加载中调用 stop_server()，必须在数秒内退出（旧实现会等满 15 分钟）
  2. 快速路径：实例已就绪时 ensure_server 不做任何网络探测
  3. 归属判据：只回收自家孤儿；别人的服务探不通也绝不结束其进程
  4. 探针超时参数：复用探测必须宽容（≥15s），就绪探测必须短（≤5s，保证能快速取消）
  5. 端口被非 llama 程序占用时给出可读错误
  6. 实例记录文件写入/清除、日志尾部读取
  7. _probe / _health_ok 对真实 HTTP 服务的判定
  8. Job Object：句柄关闭后子进程被内核回收（"强杀不残留"的内核保障）
所有用例都在临时目录里跑，不碰程序目录与用户数据。
"""
import glob
import http.server
import os
import socket
import sys
import tempfile
import threading
import time
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
sys.path.insert(0, ROOT)

from transpdf import localserver as ls  # noqa: E402

if not MODEL:
    print("未找到 GGUF 模型（models/ 或 release/*/models/），跳过运行时验证。")
    sys.exit(0)

TMP = tempfile.mkdtemp(prefix="transpdf_rt_")
ls.app_dir = lambda: TMP          # 把锁文件/日志重定向到临时目录，绝不写程序目录

FAIL, HARD = [], []
LOGS = []


def check(name, fn):
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


def log(s):
    LOGS.append(s)


def reset():
    """把模块状态恢复到"从未启动过服务"。"""
    LOGS.clear()
    ls._clear_lock()
    with ls._state["lock"]:
        ls._state.update(proc=None, job=None, log_fh=None, base=None, ready=False)
    ls._state["cancel_start"].clear()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class FakeProc:
    """假 llama-server：永远存活、永不就绪，用来拖住启动等待循环。"""
    def __init__(self, pid=4242):
        self.pid = pid
        self.dead = False

    def poll(self):
        return 0 if self.dead else None

    def terminate(self):
        self.dead = True

    def kill(self):
        self.dead = True

    def wait(self, timeout=None):
        self.dead = True
        return 0


def make_cfg(port, gpu=False):
    return {"local": {"port": port, "ctx": 8192, "gpu": gpu,
                      "model_path": MODEL, "llama_dir": TMP}}


# --------------------------------------------------------------------------
print("=" * 72)
print("A) 关窗中断：旧实现在模型加载中关窗会等满 _LOAD_TIMEOUT")
print("=" * 72)


def t_cancel_interrupts_load():
    reset()
    port = free_port()
    fake = FakeProc()
    saved = {n: ls.__dict__[n] for n in
             ("_spawn", "_probe", "_health_ok", "find_llama_server", "_launch_attempts")}
    ls._spawn = lambda s, m, p, c, a: (fake, None)
    ls._probe = lambda base, timeout=None: False          # 永不就绪
    ls.find_llama_server = lambda d: r"C:\fake\llama-server.exe"
    ls._launch_attempts = lambda cfg, m: [["-ngl", "0"]]
    ls._health_ok = lambda *a, **k: False
    result = {}
    try:
        def worker():
            t0 = time.time()
            try:
                ls.ensure_server(make_cfg(port), log)
            except Exception as e:
                result["err"] = e
            result["dt"] = time.time() - t0

        th = threading.Thread(target=worker, daemon=True)
        th.start()
        time.sleep(2.0)
        assert th.is_alive(), "启动线程已结束，未进入等待循环（测试前提不成立）"
        t_stop = time.time()
        ls.stop_server()                                   # 等价于用户此刻关窗
        th.join(timeout=20)
        stop_dt = time.time() - t_stop
        assert not th.is_alive(), f"stop_server 后启动线程仍未退出（卡死 {stop_dt:.0f}s）"
        assert stop_dt < 8.0, f"中断耗时 {stop_dt:.1f}s，未达到立即退出的要求"
        assert ls._LOAD_TIMEOUT == 900, "超时上限被意外改动"
        assert "启动已取消" in str(result.get("err")), f"异常信息异常：{result.get('err')!r}"
        assert ls._state["proc"] is None, "取消后未清空进程记录"
        assert not os.path.exists(ls._lock_path()), "取消后实例记录文件仍在"
        return (f"加载等待被中断：{stop_dt:.1f}s 内退出"
                f"（旧实现需等满 {ls._LOAD_TIMEOUT}s）；已回收进程并清除记录")
    finally:
        for k, v in saved.items():
            setattr(ls, k, v)
        reset()


check("模型加载中关窗能立即中断（不卡死）", t_cancel_interrupts_load)


# --------------------------------------------------------------------------
print()
print("=" * 72)
print("B) 就绪快速路径：已就绪时不得再做网络探测")
print("=" * 72)


def t_fast_path():
    reset()
    port = free_port()
    fake = FakeProc(pid=777)
    with ls._state["lock"]:
        ls._state.update(proc=fake, base=f"http://127.0.0.1:{port}", ready=True)

    def boom(*a, **k):
        raise AssertionError("快速路径不该调用网络探测！")

    real_probe, real_health = ls._probe, ls._health_ok
    ls._probe, ls._health_ok = boom, boom
    try:
        t0 = time.time()
        base = ls.ensure_server(make_cfg(port), log)
        dt = time.time() - t0
        assert base == f"http://127.0.0.1:{port}", base
        assert dt < 0.2, f"耗时 {dt*1000:.0f}ms，说明仍在做网络探测"
        assert ls.is_running(make_cfg(port)) is True, "is_running 在就绪时未走快速路径"
        return f"{dt*1000:.1f}ms 返回，零网络探测"
    finally:
        ls._probe, ls._health_ok = real_probe, real_health
        reset()


check("ensure_server 就绪快速路径零探测", t_fast_path)


# --------------------------------------------------------------------------
print()
print("=" * 72)
print("C) 残留实例回收 / 探不通的外部服务不得被误杀 / 端口占用的错误提示")
print("=" * 72)


def _stub_owners(owners, name="llama-server.exe"):
    """把"端口占用者"伪造成 llama-server，并记录是否走到了加载流程。

    resolve_model 返回真实模型路径并记一笔 reached：这样"拒绝启动"的用例可以断言
    reached 为空（证明根本没往下走），回收用例则断言它非空（证明确实继续了）。
    find_llama_server 返回 None，作为下一步的确定性终点（抛"缺引擎"）。
    """
    reached = []
    killed = []
    saved = {n: ls.__dict__[n] for n in
             ("_health_ok", "_port_owner_pids", "_pid_names", "_kill_pid",
              "find_llama_server", "resolve_model")}
    ls._health_ok = lambda *a, **k: False                       # 探不通
    ls._port_owner_pids = lambda p: set(owners)
    ls._pid_names = lambda: {p: name for p in owners}
    ls._kill_pid = lambda p: (killed.append(p), True)[1]
    ls.find_llama_server = lambda d: None
    ls.resolve_model = lambda cfg: (reached.append(1), MODEL)[1]
    return saved, reached, killed


def t_reclaim_own_orphan():
    """本程序上次遗留的孤儿（记录文件 owned=True 且 PID 命中）→ 允许回收。"""
    reset()
    port = free_port()
    saved, reached, killed = _stub_owners([1234])
    ls._write_lock(1234, port, MODEL, owned=True)               # 证明确是本程序遗留
    try:
        try:
            ls.ensure_server(make_cfg(port), log)
            raise AssertionError("未在缺引擎时抛错")
        except Exception as e:
            assert type(e).__name__ == "TranslationErrorLocal", type(e).__name__
            assert "llama.cpp" in str(e), e
        assert killed == [1234], f"未回收自家孤儿：{killed}"
        assert reached, "回收后应继续走到加载流程"
        assert any("本程序" in s and "遗留" in s for s in LOGS), f"未说明归属：{LOGS}"
        return f"确认归属后回收自家孤儿 PID 1234 → 继续启动流程（{len(LOGS)} 条日志）"
    finally:
        for k, v in saved.items():
            setattr(ls, k, v)
        reset()


def t_dont_kill_external():
    """别人的 llama-server 探不通 → 绝不结束它的进程（它可能正忙）。"""
    reset()                                                     # 无记录文件 = 非本程序
    port = free_port()
    saved, reached, killed = _stub_owners([4321])
    try:
        try:
            ls.ensure_server(make_cfg(port), log)
            raise AssertionError("外部服务探不通时不应继续启动")
        except Exception as e:
            msg = str(e)
            assert type(e).__name__ == "TranslationErrorLocal", type(e).__name__
            assert "4321" in msg, f"未指出占用者 PID：{msg}"
            assert "健康检查" in msg, f"未说明判定依据：{msg}"
            assert "文本" in msg and "重试" in msg, f"未给出最可能的原因与后续动作：{msg}"
            assert "不会自动结束" in msg, f"未说明处置边界：{msg}"
        assert killed == [], f"误杀了外部服务：{killed}"
        assert reached == [], "探不通却仍走到加载流程（应直接拒绝）"
        return "拒绝结束外部进程，并提示「可能正在处理长文本，请稍候重试」"
    finally:
        for k, v in saved.items():
            setattr(ls, k, v)
        reset()


def t_dont_kill_adopted():
    """记录文件是 owned=False（复用的外部服务）→ 同样不得回收。"""
    reset()
    port = free_port()
    saved, reached, killed = _stub_owners([5678])
    ls._write_lock(5678, port, "", owned=False)                 # 记录明确写了"不是我们的"
    try:
        try:
            ls.ensure_server(make_cfg(port), log)
            raise AssertionError("不应继续启动")
        except Exception as e:
            assert type(e).__name__ == "TranslationErrorLocal", type(e).__name__
            assert "5678" in str(e), str(e)
        assert killed == [], f"误杀了复用的外部服务：{killed}"
        assert reached == [], "不应走到加载流程"
        return "owned=False 的记录被正确识别为「非本程序」，未回收"
    finally:
        for k, v in saved.items():
            setattr(ls, k, v)
        reset()


def t_orphan_judgement():
    """_is_our_orphan 的四种组合逐个验证（这是"敢不敢杀"的唯一判据）。"""
    cases = []
    reset()
    assert ls._is_our_orphan({1}) is False, "无记录文件时应判为不是我们的"
    cases.append("无记录->False")
    ls._write_lock(1, 18080, "m.gguf", owned=True)
    assert ls._is_our_orphan({1}) is True, "owned=True 且 PID 命中应判为我们的"
    cases.append("owned且PID命中->True")
    assert ls._is_our_orphan({2}) is False, "PID 对不上不应判为我们的"
    cases.append("PID不匹配->False")
    ls._write_lock(1, 18080, "m.gguf", owned=False)
    assert ls._is_our_orphan({1}) is False, "owned=False 不应判为我们的"
    cases.append("owned=False->False")
    with open(ls._lock_path(), "w", encoding="utf-8") as f:
        f.write("{ 这不是合法 JSON")
    assert ls._is_our_orphan({1}) is False, "记录损坏时应保守判为不是我们的"
    cases.append("记录损坏->False")
    reset()
    return " / ".join(cases)


def t_port_taken_by_other():
    reset()
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    real = (ls._health_ok, ls._pid_names)
    ls._health_ok = lambda *a, **k: False
    ls._pid_names = lambda: {os.getpid(): "python.exe"}
    try:
        try:
            ls.ensure_server(make_cfg(port), log)
            raise AssertionError("端口被占用时未抛错")
        except Exception as e:
            msg = str(e)
            assert type(e).__name__ == "TranslationErrorLocal", type(e).__name__
            assert "已被其它程序占用" in msg, msg
            assert "python.exe" in msg, f"未指出占用者：{msg}"
            return f"拒绝并给出可读提示：{msg[:46]}…"
    finally:
        ls._health_ok, ls._pid_names = real
        srv.close()
        reset()


check("自家孤儿实例（记录 owned=True）被回收", t_reclaim_own_orphan)
check("别人的服务探不通时不被误杀", t_dont_kill_external)
check("复用的外部服务（owned=False）不被回收", t_dont_kill_adopted)
check("_is_our_orphan 归属判据四组合", t_orphan_judgement)
check("端口被其它程序占用时明确报错", t_port_taken_by_other)


# --------------------------------------------------------------------------
print()
print("=" * 72)
print("D) 实例记录文件与日志尾部")
print("=" * 72)


def t_lockfile():
    reset()
    ls._write_lock(4321, 18080, r"C:\m\qwen3-30b-a3b.gguf", owned=True)
    import json
    with open(ls._lock_path(), encoding="utf-8") as f:
        d = json.load(f)
    assert d["pid"] == 4321 and d["port"] == 18080, d
    assert d["model"] == "qwen3-30b-a3b.gguf", d
    assert d["owned"] is True, d
    assert d["started"], "缺少启动时间"
    ls._clear_lock()
    assert not os.path.exists(ls._lock_path()), "清除失败"
    ls._clear_lock()                                        # 重复清除不得抛异常
    return "写入/读取/清除/重复清除均正常"


def t_tail_log():
    p = os.path.join(TMP, ls._SERVER_LOG)
    with open(p, "w", encoding="utf-8") as f:
        f.write("X" * 1000 + "\nCUDA error: out of memory\n")
    tail = ls._tail_log(300)
    assert tail.endswith("CUDA error: out of memory"), tail[-60:]
    assert len(tail) <= 300, len(tail)
    ls._SERVER_LOG_BAK = ls._SERVER_LOG
    ls._SERVER_LOG = "does-not-exist.log"
    try:
        assert ls._tail_log() == "", "文件不存在时应返回空串而不是抛异常"
    finally:
        ls._SERVER_LOG = ls._SERVER_LOG_BAK
        os.remove(p)
    return "取到真实失败原因，缺文件时安全降级"


check("实例记录文件（锁文件）读写", t_lockfile)
check("服务日志尾部读取（失败原因回传）", t_tail_log)


# --------------------------------------------------------------------------
print()
print("=" * 72)
print("E) 健康探测对真实 HTTP 服务的判定 / 超时策略")
print("=" * 72)


def t_timeout_policy():
    """超时参数是本次修复的核心，必须有护栏防止被改回去。"""
    import inspect
    hp = inspect.signature(ls._probe).parameters["timeout"].default
    hh = inspect.signature(ls._health_ok).parameters["timeout"].default
    # 复用探测（对方可能在忙）必须宽容
    assert ls._HEALTH_TIMEOUT >= 15.0, f"复用探测超时太短：{ls._HEALTH_TIMEOUT}s"
    # 就绪探测必须短，否则"关窗取消"要等一个完整超时才能被感知
    assert ls._READY_TIMEOUT <= 5.0, f"就绪探测超时太长：{ls._READY_TIMEOUT}s"
    assert hp == ls._READY_TIMEOUT, f"_probe 默认超时应取就绪值：{hp}"
    assert hh == ls._READY_TIMEOUT, f"_health_ok 默认超时应取就绪值：{hh}"
    worst = ls._HEALTH_RETRY * ls._HEALTH_TIMEOUT + (ls._HEALTH_RETRY - 1)
    assert worst <= 60, f"复用探测最坏耗时 {worst:.0f}s 过长，启动会像卡死"

    # is_running 必须走长超时：否则外部实例一忙就被误报成"未运行"
    captured = {}
    real = ls._health_ok
    ls._health_ok = lambda base, retries=1, timeout=None: (
        captured.update(timeout=timeout), False)[1]
    try:
        assert ls.is_running({"local": {"port": free_port()}}) is False
    finally:
        ls._health_ok = real
    assert captured.get("timeout") == ls._HEALTH_TIMEOUT, \
        f"is_running 未走长超时：{captured.get('timeout')}"
    return (f"复用 {ls._HEALTH_TIMEOUT:.0f}s×{ls._HEALTH_RETRY} 次"
            f"（最坏 {worst:.0f}s）/ 就绪 {ls._READY_TIMEOUT:.0f}s / is_running 走长超时")


check("探针超时策略（复用宽容 / 就绪短）", t_timeout_policy)


def t_probe_real():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            code = 200 if self.path == "/health" else 404
            self.send_response(code)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        assert ls._probe(base, timeout=3.0) is True, "健康服务被判为不可用"
        assert ls._health_ok(base, retries=1) is True, "带重试探测失败"
        # 404 的服务：/health 与 /v1/models 都拿不到 200 -> 必须判 False
        assert ls._probe(base + "", timeout=3.0) is True
        dead = free_port()
        t0 = time.time()
        assert ls._probe(f"http://127.0.0.1:{dead}", timeout=1.0) is False
        assert time.time() - t0 < 3.0, "探测未遵守 timeout"
        return f"/health 200 -> True；端口 {dead} 无服务 -> False（超时受控）"
    finally:
        httpd.shutdown()
        httpd.server_close()


check("_probe / _health_ok 真实 HTTP 判定", t_probe_real)


# --------------------------------------------------------------------------
print()
print("=" * 72)
print("F) Job Object：内核级子进程树回收")
print("=" * 72)


def t_job_object():
    if os.name != "nt":
        return "跳过（非 Windows）"
    import subprocess
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                            creationflags=0x08000000)   # CREATE_NO_WINDOW，不弹窗
    job, err = ls._attach_job(proc)
    try:
        assert isinstance((job, err), tuple) and len((job, err)) == 2, "签名应为 (job, 原因)"
        if job is None:
            # 本进程已在不允许嵌套的 Job 中时才会走到这里，属已知限制
            assert err, "失败时必须给出原因（否则静默失败无法排查）"
            proc.kill()
            proc.wait(timeout=10)
            return f"环境不支持（已给出原因：{err[:40]}），非代码缺陷"
        assert err == "", err
        assert proc.poll() is None, "附加后进程不应立即退出"
        import ctypes
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(job))
        for _ in range(100):
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        assert proc.poll() is not None, \
            "关闭 Job 句柄后子进程仍存活——强杀主程序会残留推理进程"
        return "句柄关闭后子进程被内核回收（程序被强杀也不残留）"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


check("Job Object 内核级回收子进程", t_job_object)


# --------------------------------------------------------------------------
print()
print("=" * 72)
print(f"结果：失败 {len(FAIL)} 项，硬错误 {len(HARD)} 项")
if FAIL:
    print("  失败：", FAIL)
if HARD:
    print("  硬错误：", HARD)
print("=" * 72)
sys.exit(1 if (FAIL or HARD) else 0)
