"""硬件自检：物理内存、显卡显存（DXGI 枚举），判断能否带动高质档本地模型。

高质档模型 12~17GB：
- 全进显卡：显存 ≥ 14GB 最舒服
- 拆分显存+内存：显存 ≥ 9GB 且内存 ≥ 16GB 可用（MoE 模型专家层放内存）
- 纯 CPU：内存 ≥ 24GB 可用（较慢），32GB 舒服
"""
import ctypes
import ctypes.wintypes
import os


def _fmt_gb(n):
    return round(n / (1024 ** 3), 1)


def total_ram():
    """物理内存总大小（字节），失败返回 0。"""
    try:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_uint), ("dwMemoryLoad", ctypes.c_uint),
                ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                ("ullAvailExtendedVirtual", ctypes.c_uint64),
            ]
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return int(st.ullTotalPhys)
    except Exception:
        pass
    return 0


def _dxgi_adapters_raw():
    """纯 ctypes 实现 IDXGIFactory1::EnumAdapters1 + IDXGIAdapter1::GetDesc1。"""
    out = []
    try:
        dxgi = ctypes.oledll.dxgi
        # IID_IDXGIFactory1 = {770aae78-f26f-4dba-a829-253c83d1b38e}
        iid = (ctypes.c_ubyte * 16)(
            0x78, 0xAE, 0x0A, 0x77, 0x6F, 0xF2, 0x4D, 0xBA,
            0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x8E)

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]

        class DXGI_ADAPTER_DESC1(ctypes.Structure):
            _fields_ = [
                ("Description", ctypes.c_wchar * 128),
                ("VendorId", ctypes.c_uint32), ("DeviceId", ctypes.c_uint32),
                ("SubSysId", ctypes.c_uint32), ("Revision", ctypes.c_uint32),
                ("DedicatedVideoMemory", ctypes.c_size_t),
                ("DedicatedSystemMemory", ctypes.c_size_t),
                ("SharedSystemMemory", ctypes.c_size_t),
                ("AdapterLuid", LUID), ("Flags", ctypes.c_uint),
            ]

        factory = ctypes.c_void_p()
        # CreateDXGIFactory1(riid, out) —— HRESULT 由 oledll 自动检查
        dxgi.CreateDXGIFactory1(ctypes.byref(iid), ctypes.byref(factory))
        if not factory.value:
            return out
        # COM 虚表：IDXGIFactory1 -> QI(0) AddRef(1) Release(2) EnumAdapters(3) EnumAdapters1(4)
        vtbl = ctypes.cast(ctypes.cast(factory, ctypes.POINTER(ctypes.c_void_p)).contents.value,
                           ctypes.POINTER(ctypes.c_void_p * 5)).contents
        proto_enum = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))
        enum_adapters1 = proto_enum(vtbl[4])

        i = 0
        while True:
            adapter = ctypes.c_void_p()
            hr = enum_adapters1(factory, i, ctypes.byref(adapter))
            if hr != 0 or not adapter.value:
                break
            # IDXGIAdapter1 虚表 -> QI(0) AddRef(1) Release(2) EnumOutputs(3) GetDesc1(4)
            a_vtbl = ctypes.cast(
                ctypes.cast(adapter, ctypes.POINTER(ctypes.c_void_p)).contents.value,
                ctypes.POINTER(ctypes.c_void_p * 5)).contents
            proto_desc = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(DXGI_ADAPTER_DESC1))
            get_desc1 = proto_desc(a_vtbl[4])
            desc = DXGI_ADAPTER_DESC1()
            hr = get_desc1(adapter, ctypes.byref(desc))
            if hr == 0:
                soft = bool(desc.Flags & 2)  # DXGI_ADAPTER_FLAG_SOFTWARE
                out.append((desc.Description, int(desc.DedicatedVideoMemory), soft))
            # Release adapter
            release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(a_vtbl[2])
            release(adapter)
            i += 1
    except Exception:
        pass
    return out


def _registry_adapters():
    """注册表兜底：显示适配器类的 HardwareInformation.qwMemorySize。"""
    out = []
    try:
        import winreg
        base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, i)
                    i += 1
                except OSError:
                    break
                try:
                    with winreg.OpenKey(root, sub) as k:
                        name, _ = winreg.QueryValueEx(k, "DriverDesc")
                        try:
                            mem, _ = winreg.QueryValueEx(k, "HardwareInformation.qwMemorySize")
                        except OSError:
                            try:
                                mem, _ = winreg.QueryValueEx(k, "HardwareInformation.MemorySize")
                            except OSError:
                                mem = 0
                        if isinstance(mem, bytes) and len(mem) >= 8:
                            mem = int.from_bytes(mem[:8], "little")
                        out.append((name, int(mem or 0), False))
                except OSError:
                    continue
    except Exception:
        pass
    return out


def gpus():
    """返回 [(名称, 显存字节)]，只保留独立显存 > 512MB 的真实显卡。"""
    adapters = _dxgi_adapters_raw()
    if not adapters:
        adapters = _registry_adapters()
    return [(n, m) for (n, m, soft) in adapters
            if not soft and m > 512 * 1024 * 1024]


def summary():
    """返回 (内存字节, [(显卡名, 显存字节)...])。"""
    return total_ram(), gpus()


# --------------------------------------------------------------------------
# GGUF 元数据读取与显存需求估算
# --------------------------------------------------------------------------

# 只读文件头部（元数据区），不碰张量数据
_GGUF_META_CACHE = {}
_KV_CACHE = {}
_GGUF_META_KEEP = ("architecture", "block_count", "head_count_kv",
                   "head_count", "key_length", "value_length")
DEFAULT_MODEL_SIZE = 13 * 1024 ** 3


def gguf_meta(path, max_bytes=64 * 1024 * 1024):
    """读取 GGUF 头部元数据。失败或非 GGUF 一律返回 {}。

    只保留与显存估算相关的键；其余（尤其是上兆的词表数组）直接跳过，
    不materialize 进内存。GGUF 的元数据都在文件开头，因此无需读整个模型。
    """
    key = os.path.abspath(path) if path else ""
    if key in _GGUF_META_CACHE:
        return _GGUF_META_CACHE[key]
    meta = {}
    try:
        import io as _io
        import struct

        with open(path, "rb") as fh:
            buf = fh.read(max_bytes)
        if buf[:4] != b"GGUF":
            raise ValueError("不是 GGUF 文件")
        s = _io.BytesIO(buf)

        def take(n):
            b = s.read(n)
            if len(b) != n:
                raise EOFError("元数据超出读取上限")
            return b

        def u32():
            return struct.unpack("<I", take(4))[0]

        def u64():
            return struct.unpack("<Q", take(8))[0]

        def string():
            return take(u64()).decode("utf-8", "replace")

        fmt = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i",
               6: "<f", 10: "<Q", 11: "<q", 12: "<d"}

        def value(t):
            if t == 7:
                return bool(take(1)[0])
            if t == 8:
                return string()
            if t == 9:
                et, n = u32(), u64()
                return [value(et) for _ in range(n)]
            return struct.unpack(fmt[t], take(struct.calcsize(fmt[t])))[0]

        def skip_value(t):
            if t == 7:
                s.seek(1, 1)
            elif t == 8:
                s.seek(u64(), 1)
            elif t == 9:
                et, n = u32(), u64()
                for _ in range(n):
                    skip_value(et)
            else:
                s.seek(struct.calcsize(fmt[t]), 1)

        take(4)   # magic（上面已校验）
        u32()     # version
        u64()     # tensor 数量
        n_kv = u64()
        for _ in range(n_kv):
            k = string()
            t = u32()
            if any(w in k for w in _GGUF_META_KEEP):
                meta[k] = value(t)
            else:
                skip_value(t)
    except Exception:
        meta = {}
    _GGUF_META_CACHE[key] = meta
    return meta


def kv_cache_bytes(path, ctx=8192):
    """估算 KV cache 体积（字节，按 f16 每元素 2 字节）。无法解析返回 0。"""
    key = (os.path.abspath(path) if path else "", int(ctx or 0))
    if key in _KV_CACHE:
        return _KV_CACHE[key]
    total = 0
    try:
        meta = gguf_meta(path)
        arch = meta.get("general.architecture", "")
        layers = int(meta.get(f"{arch}.block_count", 0) or 0)
        heads_kv = int(meta.get(f"{arch}.attention.head_count_kv", 0)
                       or meta.get(f"{arch}.attention.head_count", 0) or 0)
        k_len = int(meta.get(f"{arch}.attention.key_length", 0) or 0)
        v_len = int(meta.get(f"{arch}.attention.value_length", 0) or k_len)
        if layers and heads_kv and k_len and ctx:
            total = layers * heads_kv * (k_len + v_len) * 2 * int(ctx)
    except Exception:
        total = 0
    _KV_CACHE[key] = total
    return total


def vram_need(path, ctx=8192, model_size=None):
    """估算把模型全量放进显存所需字节 = 权重 + KV cache + 计算缓冲预留。

    只看权重体积会低估：长上下文模型的 KV cache 与推理计算缓冲同样占显存，
    这是「看起来装得下、实测却回落内存」的常见原因。
    """
    if model_size is None:
        try:
            model_size = os.path.getsize(path)
        except (OSError, TypeError):
            model_size = 0
    if not model_size:
        return 0
    kv = kv_cache_bytes(path, ctx)
    # 计算缓冲、驱动/桌面占用与显存碎片：按权重的 8%，且不低于 512MB
    reserve = max(int(model_size * 0.08), 512 * 1024 ** 2)
    return model_size + kv + reserve


def capability(model_path=None, ctx=8192):
    """判断能否运行目标模型，返回 (能否运行, 描述文本, 建议 ngl 参数)。

    model_path 给定时按「权重 + KV cache + 计算缓冲」估算真实显存需求；
    未给定时退回经验值 13GB。
    """
    GiB = 1024 ** 3
    ram, gpus_ = summary()
    ram_gb = _fmt_gb(ram)
    need = vram_need(model_path, ctx) or DEFAULT_MODEL_SIZE
    need_gb = _fmt_gb(need)
    if gpus_:
        name, vram = max(gpus_, key=lambda x: x[1])
        vram_gb = _fmt_gb(vram)
        desc = f"内存 {ram_gb}GB，显卡 {name}（显存 {vram_gb}GB）"
        if vram >= need:
            return True, desc + f"，可全速运行（模型约需 {need_gb}GB 显存）", 999
        if vram >= 9 * GiB and ram >= 16 * GiB:
            return True, desc + f"，显存与内存拆分运行（模型约需 {need_gb}GB，速度良好）", 999
        if ram >= 24 * GiB:
            return True, desc + "，将以内存为主运行（速度一般）", 24 if vram else 0
        return False, desc + "，低于本软件要求（建议显存 ≥ 12GB，或内存 ≥ 24GB）", 0
    desc = f"内存 {ram_gb}GB，未检测到独立显卡"
    if ram >= 24 * GiB:
        return True, desc + "，将用 CPU 运行（速度较慢）", 0
    return False, desc + "，低于本软件要求（建议显存 ≥ 12GB，或内存 ≥ 24GB）", 0


if __name__ == "__main__":
    import sys
    model = sys.argv[1] if len(sys.argv) > 1 else None
    ok, text, ngl = capability(model)
    print("内存:", _fmt_gb(total_ram()), "GB")
    for n, m in gpus():
        print("显卡:", n, _fmt_gb(m), "GB")
    if model:
        print("模型:", model)
        print("  权重:", _fmt_gb(os.path.getsize(model)), "GB")
        print("  KV cache(8192):", _fmt_gb(kv_cache_bytes(model, 8192)), "GB")
        print("  显存总需:", _fmt_gb(vram_need(model, 8192)), "GB")
    print("结论:", ok, "|", text, "| ngl =", ngl)
