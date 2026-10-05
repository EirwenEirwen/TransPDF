"""本地大模型服务管理（llama.cpp + GGUF，纯离线）。

职责：
- 扫描 GGUF 模型（发布包 models/、用户自定义目录、llama.cpp 相邻目录）
- 启动前探测并复用端口上已有的健康服务
- 自动启动，失败逐级降级：全显存 -> MoE 专家层放内存 -> 纯 CPU
- 退出时回收自建服务进程（atexit + GUI 关闭 + Windows Job Object，强杀也不残留）；复用的外部服务不回收

两条关键判定规则：
1. 自建实例的存活用 proc.poll() 判定，不做网络探测——llama-server 生成长文本时可能
   腾不出 HTTP 线程，把"探针超时"当成"服务不存在"会导致重复加载十几 GB 模型，
   甚至杀掉正在服务的实例。探针只用于探测外部服务、确认新实例真的拿到了端口。
2. 端口被占用但探不通 != 该进程没用了。只回收确证属于本程序的孤儿实例（记录文件
   owned=True 且 PID 命中占用者），其余一律不擅自结束，改为抛可读错误交用户决定。
"""
import atexit
import csv
import ctypes
import io
import json
import os
import subprocess
import threading
import time

import requests

from .config import app_dir

DEFAULT_PORT = 18080
_HEALTH_TIMEOUT = 15.0     # 复用探测：对方可能在生成，必须宽容
_READY_TIMEOUT = 5.0       # 就绪探测：短超时才能让"关窗取消"及时生效
_HEALTH_RETRY = 2          # 单次超时不足以判定服务不在
_LOAD_TIMEOUT = 900        # 等待模型加载的上限（秒）
_LOCK_NAME = ".localserver.lock"
_SERVER_LOG = "llama-server.log"

_state = {
    "proc": None,       # 本进程启动的 llama-server；None 表示当前 base 是复用的外部服务
    "job": None,        # Job Object 句柄：随句柄关闭回收子进程树
    "log_fh": None,     # llama-server 日志文件句柄
    "base": None,
    "ready": False,     # 已确认可用：命中后直接返回，不再做网络探测
    "jinja": True,
    "lock": threading.Lock(),        # 只保护以上字段，临界区必须短（绝不等模型加载）
    "start_lock": threading.Lock(),  # 串行化"启动服务"，避免并发拉起多个实例
    "cancel_start": threading.Event(),
    "atexit_done": False,
}


class TranslationErrorLocal(Exception):
    """本地推理相关错误（带用户可读的中文说明）。"""


# --------------------------------------------------------------------------
# 健康探测
# --------------------------------------------------------------------------

def _probe(base, timeout=_READY_TIMEOUT):
    """一次健康探测。返回 True/False（不抛异常）。"""
    try:
        r = requests.get(base + "/health", timeout=timeout)
        if r.status_code == 200:
            return True
    except Exception:
        pass
    try:
        r = requests.get(base + "/v1/models", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def _health_ok(base, retries=1, timeout=_READY_TIMEOUT):
    """带重试的健康探测。单次超时不代表服务不在，故给 retries 次机会。"""
    for i in range(max(1, int(retries))):
        if _probe(base, timeout):
            return True
        if i + 1 < retries:
            time.sleep(1.0)
    return False


def is_running(cfg):
    """服务是否可用（供界面显示状态）。走长超时：外部实例忙时服务其实是活的。"""
    with _state["lock"]:
        if _state["ready"] and _is_alive(_state["proc"]):
            return True
    port = int(cfg.get("local", {}).get("port") or DEFAULT_PORT)
    return _health_ok(f"http://127.0.0.1:{port}", retries=1, timeout=_HEALTH_TIMEOUT)


def jinja_ok():
    return _state.get("jinja", True)


# --------------------------------------------------------------------------
# 进程与端口
# --------------------------------------------------------------------------

def _is_alive(proc):
    """子进程是否仍存活。proc 为 None（复用的外部服务）时返回 False。"""
    if proc is None:
        return False
    try:
        return proc.poll() is None
    except Exception:
        return False


def _port_owner_pids(port):
    """返回监听该端口的进程 PID 集合；无法判定时返回 None（区别于"确认为空"）。"""
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             errors="replace", timeout=15).stdout
    except Exception:
        return None
    want = f":{int(port)}"
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3] == "LISTENING":
            if parts[1].endswith(want) and parts[4].isdigit() and parts[4] != "0":
                pids.add(int(parts[4]))
    return pids


def _pid_names():
    """返回 {pid: 进程名小写}，用于判断端口占用者是不是 llama-server。失败返回 {}。"""
    if os.name != "nt":
        return {}
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, errors="replace",
                             timeout=20).stdout
    except Exception:
        return {}
    names = {}
    for row in csv.reader(io.StringIO(out)):
        if len(row) >= 2 and row[1].strip().isdigit():
            names[int(row[1])] = row[0].strip().lower()
    return names


def _kill_pid(pid):
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=20)
        return True
    except Exception:
        return False


def _owns_port(pid, port):
    """端口是否确实由 pid 提供服务（无法判定时返回 True，即不阻断）。"""
    owners = _port_owner_pids(port)
    if not owners:
        return True
    return int(pid) in owners


# --------------------------------------------------------------------------
# 实例记录文件（仅用于诊断与"残留实例"识别，不承担关键判定）
# --------------------------------------------------------------------------

def _lock_path():
    return os.path.join(app_dir(), _LOCK_NAME)


def _write_lock(pid, port, model, owned=True):
    try:
        with open(_lock_path(), "w", encoding="utf-8") as f:
            json.dump({"pid": int(pid), "port": int(port), "owned": bool(owned),
                       "model": os.path.basename(model or ""),
                       "started": time.strftime("%Y-%m-%d %H:%M:%S")},
                      f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _clear_lock():
    try:
        os.remove(_lock_path())
    except OSError:
        pass


def _read_lock():
    """读取实例记录，返回 dict；无法读取或格式不符时返回 None。"""
    try:
        with open(_lock_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _is_our_orphan(owners):
    """端口占用者是否确证为本程序上次遗留的实例（可安全回收）。

    唯一判据：记录文件 owned=True 且记录的 PID 命中占用者。记录缺失、owned=False、
    PID 对不上，一律判为"不是我们的"——宁可让用户手动处理，也不擅自结束别人的进程。
    """
    rec = _read_lock()
    if not rec or not rec.get("owned"):
        return False
    try:
        return int(rec.get("pid") or 0) in {int(p) for p in owners}
    except (TypeError, ValueError):
        return False


def _tail_log(n=300):
    """取推理服务日志末尾若干字符，用于把启动失败的真实原因带回给用户。"""
    try:
        with open(os.path.join(app_dir(), _SERVER_LOG), "r",
                  encoding="utf-8", errors="replace") as f:
            return f.read()[-n:].strip()
    except OSError:
        return ""


# --------------------------------------------------------------------------
# 探测与扫描
# --------------------------------------------------------------------------

def autodetect_llama_dir():
    """常见位置探测 llama.cpp 目录。"""
    candidates = [
        os.path.join(app_dir(), "runtime"),          # 发布包内置
        os.path.join(app_dir(), "llama.cpp"),
        os.path.join(os.path.dirname(app_dir()), "llama.cpp"),
        r"F:\LModel\llama.cpp",
        r"D:\LModel\llama.cpp",
        r"C:\LModel\llama.cpp",
    ]
    for c in candidates:
        if find_llama_server(c):
            return c
    return ""


def find_llama_server(llama_dir):
    if not llama_dir:
        return None
    for name in ("llama-server.exe", "llama-server",
                 os.path.join("bin", "llama-server.exe"),
                 os.path.join("build", "bin", "llama-server.exe")):
        p = os.path.join(llama_dir, name)
        if os.path.isfile(p):
            return p
    return None


def model_dirs(cfg):
    """需要扫描 GGUF 的目录列表（去重）。"""
    dirs = [os.path.join(app_dir(), "models")]
    if cfg.get("extra_model_dir"):
        dirs.append(cfg["extra_model_dir"])
    local = cfg.get("local", {})
    if local.get("llama_dir"):
        dirs.append(os.path.join(os.path.dirname(os.path.normpath(local["llama_dir"])), "models"))
        dirs.append(os.path.join(os.path.normpath(local["llama_dir"]), "models"))
    seen, out = set(), []
    for d in dirs:
        d = os.path.normpath(d)
        if d.lower() not in seen and os.path.isdir(d):
            seen.add(d.lower())
            out.append(d)
    return out


def find_gguf_models(cfg):
    """扫描全部模型目录，返回 [(路径, 大小字节)] 按大小降序。"""
    found = {}
    for d in model_dirs(cfg):
        try:
            for name in os.listdir(d):
                if name.lower().endswith(".gguf"):
                    p = os.path.join(d, name)
                    try:
                        found[p] = os.path.getsize(p)
                    except OSError:
                        pass
        except OSError:
            pass
    return sorted(found.items(), key=lambda kv: kv[1], reverse=True)


def resolve_model(cfg):
    """确定要用的模型：优先配置指定；否则取显存装得下的最大模型，再不行取最大的。

    是否"装得下"按 权重 + KV cache + 计算缓冲 估算，而不是只看权重体积。
    """
    from . import hardware
    local = cfg.setdefault("local", {})
    mp = local.get("model_path")
    if mp and os.path.isfile(mp):
        return mp
    if not local.get("llama_dir"):
        local["llama_dir"] = autodetect_llama_dir()  # 先定位引擎，才能扫到相邻 models 目录
    models = find_gguf_models(cfg)
    if not models:
        return ""
    try:
        _, gpus_ = hardware.summary()
        vram = max((m for _, m in gpus_), default=0)
    except Exception:
        vram = 0
    ctx = int(local.get("ctx") or 8192)
    pick = models[0]
    if vram:
        fitting = [m for m in models if hardware.vram_need(m[0], ctx, m[1]) <= vram]
        if fitting:
            pick = fitting[0]
    local["model_path"] = pick[0]
    return pick[0]


# --------------------------------------------------------------------------
# 启停
# --------------------------------------------------------------------------

def _creation_flags():
    return 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW


def _spawn(server, model, port, cfg, extra_args):
    """启动 llama-server，返回 (Popen, 日志文件句柄或 None)。

    显式固定 --parallel 1：该参数默认是 auto，可能把 -c 指定的上下文按槽均分，
    导致长段落被静默截断。日志不再丢弃，启动失败时可把真实原因回给用户。
    """
    cmd = [server, "-m", model, "--host", "127.0.0.1", "--port", str(port),
           "-c", str(int(cfg.get("ctx") or 8192)),
           "--parallel", "1"] + list(extra_args)
    fh = None
    try:
        fh = open(os.path.join(app_dir(), _SERVER_LOG), "w",
                  encoding="utf-8", errors="replace")
        out = fh
    except OSError:
        out = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT,
                                creationflags=_creation_flags())
    except Exception:
        if fh is not None:
            fh.close()
        raise
    return proc, fh


def _launch_attempts(cfg, model_path):
    """按顺序尝试的启动参数组合（先快后省，失败逐级降级）。"""
    from . import hardware
    local = cfg.get("local", {})
    _, gpus_ = hardware.summary()
    vram = max((m for _, m in gpus_), default=0)
    try:
        size = os.path.getsize(model_path)
    except OSError:
        size = 0
    ctx = int(local.get("ctx") or 8192)
    need = hardware.vram_need(model_path, ctx, size) if size else 0
    moe = any(k in os.path.basename(model_path).upper()
              for k in ("-A3B", "-A2B", "MOE"))
    attempts = []
    if local.get("gpu", True):
        if vram and need and vram < need and moe:
            # 显存装不下：MoE 模型把专家层放内存，注意力层放显存
            attempts.append(["--n-cpu-moe", "999", "-ngl", "999", "--jinja"])
        attempts.append(["-ngl", "999", "--jinja"])
        attempts.append(["-ngl", "999"])
    attempts.append(["-ngl", "0", "--jinja"])
    attempts.append(["-ngl", "0"])
    return attempts


def _describe_mode(args):
    """根据实际启动参数推断运行模式（只用于日志，必须与参数一致）。"""
    ngl = "0"
    if "-ngl" in args:
        try:
            ngl = args[args.index("-ngl") + 1]
        except IndexError:
            ngl = "0"
    if "--n-cpu-moe" in args:
        return "GPU + 内存拆分（MoE 专家层在内存）"
    if ngl == "0":
        return "纯 CPU（较慢）"
    return "GPU 全量"


def _attach_job(proc):
    """把 llama-server 加入 Windows Job Object（KILL_ON_JOB_CLOSE）。

    返回 (job 句柄 或 None, 失败原因)。失败原因必须写进日志——静默失败会让
    "程序被强杀后残留十几 GB 进程"无从排查。
    """
    if os.name != "nt":
        return None, "非 Windows 平台，无 Job Object"
    try:
        k32 = ctypes.windll.kernel32
        # 64 位下 HANDLE 必须按指针宽度传参，显式声明 restype/argtypes
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                ctypes.c_void_p, ctypes.c_uint]
        k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class _BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class _EXTENDED(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BASIC),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None, f"CreateJobObjectW 失败（错误码 {ctypes.get_last_error()}）"
        info = _EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info),
                                           ctypes.sizeof(info)):  # JobObjectExtendedLimitInformation
            k32.CloseHandle(job)
            return None, "SetInformationJobObject 失败"
        try:
            handle = int(proc._handle)
        except AttributeError:
            k32.CloseHandle(job)
            return None, "拿不到子进程句柄（Popen._handle 不可用）"
        if not k32.AssignProcessToJobObject(ctypes.c_void_p(job),
                                            ctypes.c_void_p(handle)):
            err = ctypes.get_last_error()
            k32.CloseHandle(job)
            # 常见于本进程已在某个 Job 中且该 Job 不允许嵌套
            return None, f"AssignProcessToJobObject 失败（错误码 {err}）"
        return int(job), ""
    except Exception as e:
        return None, repr(e)


def _shutdown_locked():
    """回收本进程启动的服务。调用方必须已持有 _state["lock"]。"""
    proc = _state.get("proc")
    if _is_alive(proc):
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    # 关闭 Job 句柄：即使 terminate 失败，KILL_ON_JOB_CLOSE 也会终止子进程树
    job = _state.get("job")
    if job:
        try:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(job))
        except Exception:
            pass
    fh = _state.get("log_fh")
    if fh is not None:
        try:
            fh.close()
        except Exception:
            pass
    _state["proc"] = None
    _state["job"] = None
    _state["log_fh"] = None
    _state["base"] = None
    _state["ready"] = False
    _clear_lock()


def _shutdown():
    with _state["lock"]:
        _shutdown_locked()


def stop_server():
    """回收本进程启动的服务，可在任意线程调用。

    先置位 cancel_start 让进行中的启动流程尽快退出——否则在"模型加载中"关窗口
    会一直等到加载完（最坏十几分钟）。
    """
    _state["cancel_start"].set()
    with _state["lock"]:
        _shutdown_locked()


def _on_exit():
    """atexit 钩子：进程正常退出时回收子进程。"""
    try:
        _state["cancel_start"].set()
        with _state["lock"]:
            _shutdown_locked()
    except Exception:
        pass


def _adopt(base, port, log, owned):
    """登记当前使用的服务。owned=False 表示不是本进程启动的，退出时不回收。"""
    with _state["lock"]:
        _state["base"] = base
        _state["ready"] = True
        if not owned:
            _state["proc"] = None
            _state["job"] = None
    if owned:
        return
    owners = sorted(_port_owner_pids(port) or [])
    log(f"[本地] 检测到端口 {port} 已有可用的翻译服务，直接复用"
        f"（程序退出时不关闭该服务）")
    _write_lock(owners[0] if owners else 0, port, "", owned=False)


def ensure_server(cfg, log=None):
    """确保本地服务就绪，返回 base url（http://127.0.0.1:port）。"""
    log = log or (lambda s: None)
    cfg.setdefault("local", {})
    port = int(cfg["local"].get("port") or DEFAULT_PORT)
    base = f"http://127.0.0.1:{port}"

    # 快速路径：自建实例还活着就直接返回，不做网络探测（每段文本都会走到这里）。
    with _state["lock"]:
        if _state["ready"] and _is_alive(_state["proc"]):
            return _state["base"]

    with _state["start_lock"]:
        with _state["lock"]:
            if _state["ready"] and _is_alive(_state["proc"]):
                return _state["base"]
            proc = _state["proc"]

        # 本次运行启动过但进程已退出：清理干净再走后面的流程
        if proc is not None:
            if _is_alive(proc):
                with _state["lock"]:
                    _state["ready"] = True
                return base
            log("[本地] 先前启动的推理服务已退出，重新启动。")
            _shutdown()

        _state["cancel_start"].clear()

        # 复用探测：端口上已有健康服务（用户手动启动的，或上次残留的）
        if _health_ok(base, retries=_HEALTH_RETRY, timeout=_HEALTH_TIMEOUT):
            _adopt(base, port, log, owned=False)
            return base

        # 端口被占用却探不通：先弄清占用者，避免"加载完 12GB 才发现端口是别人的"。
        # 探不通 != 该进程没用了，所以只回收确证属于本程序的孤儿实例。
        owners = _port_owner_pids(port)
        if owners:
            names = _pid_names()
            svc = [p for p in owners if names.get(p, "").startswith("llama-server")]
            if len(svc) == len(owners):
                if _is_our_orphan(owners):
                    log(f"[本地] 端口 {port} 上的推理服务（PID {sorted(owners)}）是本程序"
                        f"上次遗留的实例，正在回收后重新启动…")
                    for p in owners:
                        _kill_pid(p)
                    time.sleep(1.5)
                else:
                    who = "、".join(f"PID {p}" for p in sorted(owners))
                    raise TranslationErrorLocal(
                        f"端口 {port} 上已有一个推理服务（{who}），但它未通过健康检查。"
                        "最常见的原因是它正在处理较长的文本——这时探针会超时，"
                        "但服务本身是好的。请稍等十几秒后重试；"
                        "若确认它已经无响应，可手动结束该进程，"
                        "或在「高级设置」中改用其它端口。"
                        "（本程序不会自动结束它，以免误杀正在工作的服务。）")
            else:
                who = "、".join(f"{names.get(p) or '未知程序'}(PID {p})" for p in owners)
                raise TranslationErrorLocal(
                    f"端口 {port} 已被其它程序占用（{who}）。"
                    "请关闭该程序，或在「高级设置」中改用其它端口。")

        model = resolve_model(cfg)
        if not model:
            raise TranslationErrorLocal(
                "未找到模型文件。请把高质档 GGUF 模型放到程序同目录的 models 文件夹，"
                "或在「高级设置」中指定模型位置。")
        server = find_llama_server(cfg["local"].get("llama_dir") or autodetect_llama_dir())
        if not server:
            raise TranslationErrorLocal(
                "未找到 llama.cpp 推理引擎。发布包应包含 runtime 文件夹，"
                "或在「高级设置」中指定已有的 llama.cpp 目录。")
        cfg["local"]["llama_dir"] = os.path.dirname(server)

        size = os.path.getsize(model)
        log(f"[本地] 模型：{os.path.basename(model)}（{size / 1024 ** 3:.1f} GB）")
        log("[本地] 正在启动推理服务并加载模型，可能需要几十秒到几分钟…")

        last_err = ""
        for args in _launch_attempts(cfg, model):
            if _state["cancel_start"].is_set():
                raise TranslationErrorLocal("启动已取消。")
            proc, log_fh = _spawn(server, model, port, cfg["local"], args)
            job, job_err = _attach_job(proc)
            if job is None:
                log(f"[本地] 警告：未能把子进程加入 Job Object（{job_err}）；"
                    f"程序被强制结束时可能残留推理进程。")
            with _state["lock"]:
                _state["proc"] = proc
                _state["job"] = job
                _state["log_fh"] = log_fh
                _state["jinja"] = "--jinja" in args
            if not _state["atexit_done"]:
                atexit.register(_on_exit)
                _state["atexit_done"] = True
            _write_lock(proc.pid, port, model, owned=True)

            ready = False
            interval = 2
            deadline = time.time() + _LOAD_TIMEOUT
            while time.time() < deadline:
                if _state["cancel_start"].is_set():
                    log("[本地] 启动被取消，正在回收服务进程…")
                    _shutdown()
                    raise TranslationErrorLocal("启动已取消。")
                if not _is_alive(proc):
                    last_err = "llama-server 提前退出（参数或资源问题）"
                    tail = _tail_log()
                    if tail:
                        last_err += "；服务端输出：" + tail[-200:]
                    log(f"[本地] 启动参数 {' '.join(args)} 失败，尝试下一组…")
                    break
                if _probe(base, timeout=_READY_TIMEOUT):
                    # 健康后还要确认端口确实是本进程在服务。若被别的实例占着
                    # （重复实例就是这样产生的），就回收本次启动的冗余进程、复用在服务的那个。
                    if not _owns_port(proc.pid, port):
                        owners = sorted(_port_owner_pids(port) or [])
                        log(f"[本地] 端口 {port} 已由其它实例（PID {owners}）提供服务，"
                            f"回收本次启动的冗余实例并复用它。")
                        _shutdown()
                        _adopt(base, port, log, owned=False)
                        return base
                    ready = True
                    break
                time.sleep(interval)
            if ready:
                with _state["lock"]:
                    _state["base"] = base
                    _state["ready"] = True
                log(f"[本地] 模型加载完成（运行模式：{_describe_mode(args)}），"
                    f"服务地址 {base}")
                return base
            _shutdown()

        raise TranslationErrorLocal(
            f"推理服务启动失败：{last_err}。"
            "可在「高级设置」里尝试关闭显卡加速或减小上下文长度。")
