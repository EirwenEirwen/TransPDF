"""tkinter 图形界面（纯离线版）。"""
import copy
import os
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from . import __version__, APP_NAME, engines, hardware, localserver, pdfproc
from .config import load_config, save_config

try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

LLM_PRESETS = [
    ("智谱 GLM（有免费模型）", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    ("DeepSeek（便宜好用）", "https://api.deepseek.com/v1", "deepseek-chat"),
    ("阿里通义千问", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    ("月之暗面 Kimi", "https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    ("OpenAI", "https://api.openai.com/v1", "gpt-4o-mini"),
]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("700x660")
        self.minsize(580, 540)
        self.cfg = load_config()
        self.q = queue.Queue()
        self.cancel_event = threading.Event()
        self.out_path = None

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._poll)
        threading.Thread(target=self._startup_check, daemon=True).start()

    def _on_close(self):
        """关闭窗口：在后台回收由本程序启动的 llama-server，界面不假死。

        回收包含 terminate + wait(最长 10 秒)，放主线程会让窗口无响应；
        而"模型正在加载"这种最长十几分钟的情况，已由 stop_server 里的
        取消标志中断，不会等到加载完。
        """
        if getattr(self, "_closing", False):
            return
        self._closing = True
        try:
            self.status_var.set("正在回收推理服务…")
            self.update_idletasks()
        except Exception:
            pass
        done = threading.Event()

        def work():
            try:
                localserver.stop_server()
            except Exception:
                pass
            done.set()

        threading.Thread(target=work, daemon=True).start()
        self._wait_close(done)

    def _wait_close(self, done, waited=0):
        """最多给回收 3000ms，超时就交给 Job Object / atexit 兜底，避免用户干等。"""
        if done.is_set() or waited >= 3000:
            self.destroy()
            return
        self.after(100, lambda: self._wait_close(done, waited + 100))

    # ---------------- UI ----------------
    def _build_ui(self):
        pad = dict(padx=12, pady=4)
        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="PDF 论文离线翻译器",
                  font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
        ttk.Label(top, foreground="#666", wraplength=660, justify="left",
                  text="选择一个 PDF 文件，使用本机大模型自动把英文等外文内容替换为中文，"
                       "保持原有排版。全程离线运行，论文内容不出本机。").pack(anchor="w")

        self.hw_var = tk.StringVar(value="正在检测本机硬件…")
        self.hw_label = ttk.Label(self, textvariable=self.hw_var, foreground="#555")
        self.hw_label.pack(anchor="w", padx=12, pady=(6, 0))

        frm1 = ttk.Frame(self)
        frm1.pack(fill="x", **pad)
        ttk.Button(frm1, text="① 选择 PDF 文件…", command=self.pick_file).pack(side="left")
        self.file_var = tk.StringVar(value="尚未选择文件")
        ttk.Label(frm1, textvariable=self.file_var, foreground="#333",
                  wraplength=440, justify="left").pack(side="left", padx=8)

        frm1b = ttk.Frame(self)
        frm1b.pack(fill="x", **pad)
        ttk.Label(frm1b, text="翻译引擎：").pack(side="left")
        self.engine_cb = ttk.Combobox(frm1b, state="readonly", width=36,
                                      values=list(engines.ENGINE_LABELS.values()))
        engine_id = self.cfg.get("engine", "local")
        if engine_id not in engines.ENGINE_LABELS:
            engine_id = "local"
        self.engine_cb.current(list(engines.ENGINE_LABELS).index(engine_id))
        self.engine_cb.pack(side="left")
        ttk.Label(frm1b, foreground="#888",
                  text="默认本地模型，纯离线；选在线接口需在高级设置里配置密钥").pack(side="left", padx=8)

        frm2 = ttk.Frame(self)
        frm2.pack(fill="x", **pad)
        self.model_var = tk.StringVar(value="翻译模型：检查中…")
        ttk.Label(frm2, textvariable=self.model_var).pack(side="left")
        ttk.Button(frm2, text="高级设置…", command=self.open_settings).pack(side="right")

        frm3 = ttk.Frame(self)
        frm3.pack(fill="x", pady=8)
        self.start_btn = ttk.Button(frm3, text="② 开始翻译", command=self.start, width=18)
        self.start_btn.pack(side="left", padx=(12, 6))
        self.cancel_btn = ttk.Button(frm3, text="取消", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left")
        self.open_btn = ttk.Button(frm3, text="打开译文", command=self.open_output, state="disabled")
        self.open_btn.pack(side="right", padx=(6, 12))
        self.folder_btn = ttk.Button(frm3, text="打开所在文件夹", command=self.open_folder, state="disabled")
        self.folder_btn.pack(side="right")

        frm4 = ttk.Frame(self)
        frm4.pack(fill="x", **pad)
        self.progress = ttk.Progressbar(frm4, mode="determinate")
        self.progress.pack(fill="x")
        self.status_var = tk.StringVar(value="就绪。")
        ttk.Label(frm4, textvariable=self.status_var, foreground="#555").pack(anchor="w", pady=(2, 0))

        ttk.Label(self, text="运行日志：").pack(anchor="w", padx=12)
        self.log_box = scrolledtext.ScrolledText(self, height=14, state="disabled",
                                                 font=("Microsoft YaHei UI", 9), wrap="word")
        self.log_box.pack(fill="both", expand=True, padx=12, pady=(0, 8))

    def _log(self, s):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", s + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    # ---------------- 启动自检 ----------------
    def _startup_check(self):
        """后台自检：先定位模型，再按「权重 + KV cache + 计算缓冲」评估本机能否跑动。

        整个流程包在 try 里——该函数跑在守护线程上，异常会静默终止线程，
        界面就永远停在"正在检测本机硬件…"。
        """
        try:
            model = localserver.resolve_model(self.cfg)
            ctx = int(self.cfg.get("local", {}).get("ctx") or 8192)
            ok, text, _ = hardware.capability(model or None, ctx)
            self.q.put(("hardware", ok, text))
            if not ok:
                self.q.put(("log", "警告：本机硬件可能无法流畅运行高质档翻译模型，"
                                   "翻译可能很慢或失败。"))
                return
            if model:
                size_gb = os.path.getsize(model) / 1024 ** 3
                self.q.put(("model", f"翻译模型：{os.path.basename(model)}（{size_gb:.1f} GB）"))
                self.q.put(("log", f"已找到翻译模型：{model}"))
            else:
                self.q.put(("model", "翻译模型：未找到（请把 GGUF 模型放入 models 文件夹，"
                                     "或在高级设置中指定）"))
                self.q.put(("log", "未找到 GGUF 模型文件。请把模型放入程序同目录的 models 文件夹，"
                                   "或在「高级设置」中指定模型目录。"))
        except Exception as e:
            self.q.put(("hardware", False, f"检测失败：{e}"))
            self.q.put(("log", f"硬件/模型自检异常：{e!r}"))

    # ---------------- 事件 ----------------
    def pick_file(self):
        init = self.cfg.get("last_dir") or os.path.expanduser("~")
        path = filedialog.askopenfilename(
            title="选择要翻译的 PDF 文件", initialdir=init,
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")])
        if path:
            self.file_var.set(path)
            self.cfg["last_dir"] = os.path.dirname(path)
            try:
                save_config(self.cfg)
            except RuntimeError:
                pass
            import pymupdf
            try:
                d = pymupdf.open(path)
                self._log(f"已选择：{path}（共 {d.page_count} 页）")
                d.close()
            except Exception as e:
                self._log(f"警告：文件似乎无法正常打开：{e}")

    def open_settings(self):
        SettingsDialog(self)

    def start(self):
        src = self.file_var.get()
        if src == "尚未选择文件" or not os.path.exists(src):
            messagebox.showwarning("提示", "请先选择一个 PDF 文件。")
            return
        base, ext = os.path.splitext(src)
        dst = base + "_中文翻译" + ext
        engine_id = list(engines.ENGINE_LABELS)[self.engine_cb.current()]
        self.cfg["engine"] = engine_id
        try:
            save_config(self.cfg)
        except RuntimeError:
            pass
        self.cancel_event = threading.Event()
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.open_btn.configure(state="disabled")
        self.folder_btn.configure(state="disabled")
        self.progress.configure(value=0)
        self.status_var.set("开始…")
        threading.Thread(target=self._work, args=(src, dst, engine_id), daemon=True).start()

    def _work(self, src, dst, engine_id):
        cfg = dict(self.cfg)
        try:
            pdfproc.translate_pdf(
                src, dst, cfg=cfg, engine=engine_id,
                log=lambda s: self.q.put(("log", s)),
                progress=lambda f, m: self.q.put(("progress", f, m)),
                cancel=self.cancel_event)
            self.q.put(("done", dst))
        except InterruptedError:
            self.q.put(("cancelled",))
        except Exception as e:
            self.q.put(("error", str(e)))

    def cancel(self):
        self.cancel_event.set()
        self.status_var.set("正在取消…")

    def _poll(self):
        try:
            while True:
                ev = self.q.get_nowait()
                kind = ev[0]
                if kind == "log":
                    self._log(ev[1])
                elif kind == "progress":
                    self.progress.configure(value=ev[1] * 100)
                    self.status_var.set(ev[2])
                elif kind == "hardware":
                    self.hw_var.set("本机检测：" + ev[2])
                    self.hw_label.configure(foreground=("#0a7d32" if ev[1] else "#c0392b"))
                elif kind == "model":
                    self.model_var.set(ev[1])
                elif kind == "model_list":
                    pass
                elif kind == "done":
                    self.out_path = ev[1]
                    self.progress.configure(value=100)
                    self.status_var.set(f"翻译完成：{ev[1]}")
                    self._log("—— 翻译完成，输出文件：" + ev[1])
                    self.start_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                    self.open_btn.configure(state="normal")
                    self.folder_btn.configure(state="normal")
                elif kind == "cancelled":
                    self.status_var.set("已取消。")
                    self._log("用户取消了本次翻译。")
                    self.start_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                elif kind == "error":
                    self.status_var.set("出错。")
                    self._log("出错：" + ev[1])
                    self.start_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                    messagebox.showerror("翻译失败", ev[1])
        except queue.Empty:
            pass
        self.after(80, self._poll)

    def open_output(self):
        if self.out_path and os.path.exists(self.out_path):
            os.startfile(self.out_path)

    def open_folder(self):
        if self.out_path and os.path.exists(self.out_path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(self.out_path)])


class SettingsDialog(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master)
        self.title("高级设置")
        self.geometry("680x680")
        self.resizable(False, True)
        # 必须深拷贝：dict() 是浅拷贝，嵌套的 local/llm/baidu 会与主窗口共用对象，
        # 而 _collect() 是就地改写——那样点"关闭"也等于"保存"
        self.cfg = copy.deepcopy(master.cfg)
        self.saved = False
        self._models = []
        self.transient(master)
        self.grab_set()
        self._build()
        self.wait_window()
        # 只有真正点过"保存"才把改动交回主窗口
        if self.saved:
            master.cfg = self.cfg
            threading.Thread(target=master._startup_check, daemon=True).start()

    def _build(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=12, pady=10)
        f = ttk.Frame(nb)
        nb.add(f, text=" 本地模型（默认） ")
        f2 = ttk.Frame(nb)
        nb.add(f2, text=" 在线接口 ")
        r = 0

        ttk.Label(f, text="llama.cpp 目录（含 llama-server.exe）：")\
            .grid(row=r, column=0, sticky="w", pady=(4, 0))
        r += 1
        row = ttk.Frame(f); row.grid(row=r, column=0, columnspan=3, sticky="we")
        self.llama_dir = ttk.Entry(row)
        self.llama_dir.pack(side="left", fill="x", expand=True)
        self.llama_dir.insert(0, self.cfg.get("local", {}).get("llama_dir", ""))
        ttk.Button(row, text="浏览…", command=self._browse_llama).pack(side="left", padx=4)
        ttk.Button(row, text="自动探测", command=self._autodetect).pack(side="left")
        r += 1

        ttk.Label(f, text="翻译模型（GGUF）：").grid(row=r, column=0, sticky="w", pady=(8, 0))
        r += 1
        self.model_cb = ttk.Combobox(f, state="readonly", width=70)
        self.model_cb.grid(row=r, column=0, columnspan=3, sticky="we")
        r += 1
        row2 = ttk.Frame(f); row2.grid(row=r, column=0, columnspan=3, sticky="w", pady=2)
        ttk.Button(row2, text="重新扫描模型", command=self._scan_models).pack(side="left")
        ttk.Label(row2, text="额外模型目录：").pack(side="left", padx=(12, 2))
        self.extra_dir = ttk.Entry(row2, width=30)
        self.extra_dir.insert(0, self.cfg.get("extra_model_dir", ""))
        self.extra_dir.pack(side="left")
        ttk.Button(row2, text="浏览…", command=self._browse_extra).pack(side="left", padx=4)
        r += 1

        opt = ttk.Frame(f); opt.grid(row=r, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.gpu = tk.BooleanVar(value=self.cfg.get("local", {}).get("gpu", True))
        ttk.Checkbutton(opt, text="显卡加速（Vulkan）", variable=self.gpu).pack(side="left")
        ttk.Label(opt, text="   上下文长度：").pack(side="left")
        self.ctx = ttk.Entry(opt, width=8)
        self.ctx.insert(0, str(self.cfg.get("local", {}).get("ctx", 8192)))
        self.ctx.pack(side="left")
        ttk.Label(opt, text="   端口：").pack(side="left")
        self.port = ttk.Entry(opt, width=8)
        self.port.insert(0, str(self.cfg.get("local", {}).get("port", 18080)))
        self.port.pack(side="left")
        r += 1

        btns = ttk.Frame(f); btns.grid(row=r, column=0, columnspan=3, sticky="w", pady=10)
        ttk.Button(btns, text="启动本地服务", command=self._start_server).pack(side="left")
        ttk.Button(btns, text="停止服务", command=self._stop_server).pack(side="left", padx=6)
        self.test_btn = ttk.Button(btns, text="测试翻译", command=self._test)
        self.test_btn.pack(side="left")
        r += 1

        self.srv_status = tk.StringVar(value="服务状态：未检查")
        ttk.Label(f, textvariable=self.srv_status, foreground="#555")\
            .grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1

        # 自定义术语表
        ttk.Label(f, text="自定义术语表（每行一条：英文=中文；程序同目录的 glossary.txt 也会自动加载）：")\
            .grid(row=r, column=0, columnspan=3, sticky="w", pady=(10, 2))
        r += 1
        gloss_frame = ttk.Frame(f)
        gloss_frame.grid(row=r, column=0, columnspan=3, sticky="we")
        self.glossary_text = tk.Text(gloss_frame, width=76, height=8,
                                     font=("Microsoft YaHei UI", 9), wrap="none")
        gl_sb = ttk.Scrollbar(gloss_frame, command=self.glossary_text.yview)
        self.glossary_text.configure(yscrollcommand=gl_sb.set)
        self.glossary_text.pack(side="left", fill="both", expand=True)
        gl_sb.pack(side="right", fill="y")
        for k, v in (self.cfg.get("glossary") or {}).items():
            self.glossary_text.insert("end", f"{k}={v}\n")
        ttk.Label(f, foreground="#888", wraplength=600, justify="left", text=(
            "术语表对全文强制生效（如：transformer=变换器、drop rate=掉落率）。"
            "程序会按文档内容自动识别计算机/游戏等领域并注入对应术语规范，"
            "此处的自定义词条优先级最高。"
        )).grid(row=r + 1, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # ================= 在线接口 =================
        r2 = 0
        ttk.Label(f2, foreground="#555", wraplength=600, justify="left", text=(
            "在线接口为可选能力：不配置也不影响本地离线翻译。配置后可在主界面选择"
            "对应引擎；「自动」模式会在本地模型不可用或失败时按 大模型 → 百度 → DeepL → 免费接口 "
            "的顺序切换。领域术语规范与自定义术语表对「在线大模型」同样生效。"
        )).grid(row=r2, column=0, columnspan=3, sticky="w", pady=(0, 6))
        r2 += 1

        ttk.Label(f2, text="① 在线大模型（OpenAI 兼容，学术翻译质量最佳）：")\
            .grid(row=r2, column=0, columnspan=3, sticky="w")
        r2 += 1
        prow = ttk.Frame(f2); prow.grid(row=r2, column=0, columnspan=3, sticky="w", pady=2)
        ttk.Label(prow, text="服务商预设：").pack(side="left")
        self.preset_cb = ttk.Combobox(prow, state="readonly", width=30,
                                      values=[p[0] for p in LLM_PRESETS])
        self.preset_cb.pack(side="left")
        self.preset_cb.bind("<<ComboboxSelected>>", self._apply_preset)
        r2 += 1
        grid2 = ttk.Frame(f2); grid2.grid(row=r2, column=0, columnspan=3, sticky="we")
        self.llm_base = self._labeled_entry(grid2, 0, "接口地址：", self.cfg.get("llm", {}).get("base_url", ""))
        self.llm_key = self._labeled_entry(grid2, 1, "API Key：", self.cfg.get("llm", {}).get("api_key", ""), show="•")
        self.llm_model = self._labeled_entry(grid2, 2, "模型名称：", self.cfg.get("llm", {}).get("model", ""))
        ttk.Button(f2, text="测试大模型接口", command=lambda: self._test_engine("llm"))\
            .grid(row=r2 + 1, column=0, sticky="w", pady=4)
        r2 += 2

        ttk.Label(f2, text="② 百度翻译开放平台（fanyi-api.baidu.com 注册获取，个人有免费额度）：")\
            .grid(row=r2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        r2 += 1
        grid3 = ttk.Frame(f2); grid3.grid(row=r2, column=0, columnspan=3, sticky="we")
        self.bd_id = self._labeled_entry(grid3, 0, "APP ID：", self.cfg.get("baidu", {}).get("appid", ""))
        self.bd_key = self._labeled_entry(grid3, 1, "密钥：", self.cfg.get("baidu", {}).get("key", ""), show="•")
        ttk.Button(f2, text="测试百度接口", command=lambda: self._test_engine("baidu"))\
            .grid(row=r2 + 1, column=0, sticky="w", pady=4)
        r2 += 2

        ttk.Label(f2, text="③ DeepL API（免费版 Key 以 ：fx 结尾）：")\
            .grid(row=r2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        r2 += 1
        grid4 = ttk.Frame(f2); grid4.grid(row=r2, column=0, columnspan=3, sticky="we")
        self.dl_key = self._labeled_entry(grid4, 0, "API Key：", self.cfg.get("deepl", {}).get("api_key", ""), show="•")
        self.dl_free = tk.BooleanVar(value=self.cfg.get("deepl", {}).get("free", True))
        ttk.Checkbutton(grid4, text="免费版账号", variable=self.dl_free)\
            .grid(row=1, column=0, sticky="w", padx=(4, 8))
        ttk.Button(f2, text="测试 DeepL 接口", command=lambda: self._test_engine("deepl"))\
            .grid(row=r2 + 1, column=0, sticky="w", pady=4)
        r2 += 2

        ttk.Label(f2, text="④ 免费在线接口（无需注册，有每日限额）：")\
            .grid(row=r2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        r2 += 1
        grid5 = ttk.Frame(f2); grid5.grid(row=r2, column=0, columnspan=3, sticky="we")
        self.mm_mail = self._labeled_entry(grid5, 0, "邮箱（选填，提升限额）：", self.cfg.get("mymemory_email", ""))
        ttk.Button(f2, text="测试免费接口", command=lambda: self._test_engine("mymemory"))\
            .grid(row=r2 + 1, column=0, sticky="w", pady=4)

        fb = ttk.Frame(self)
        fb.pack(fill="x", padx=12, pady=(0, 10))
        ttk.Button(fb, text="保存", command=self._save, width=12).pack(side="right", padx=4)
        ttk.Button(fb, text="关闭", command=self.destroy, width=10).pack(side="right")

        self._scan_models()

    # ---- 设置页动作 ----
    def _labeled_entry(self, parent, row, label, value, show=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="e", padx=4, pady=3)
        e = ttk.Entry(parent, width=52, show=show or "")
        e.insert(0, value)
        e.grid(row=row, column=1, sticky="w")
        return e

    def _apply_preset(self, _=None):
        idx = self.preset_cb.current()
        if 0 <= idx < len(LLM_PRESETS):
            _, base, model = LLM_PRESETS[idx]
            self.llm_base.delete(0, "end")
            self.llm_base.insert(0, base)
            self.llm_model.delete(0, "end")
            self.llm_model.insert(0, model)

    def _test_engine(self, engine):
        self._collect()
        ok, msg = engines.test_connection(engine, self.cfg)
        (messagebox.showinfo if ok else messagebox.showwarning)("测试结果", msg, parent=self)

    def _browse_llama(self):
        d = filedialog.askdirectory(title="选择 llama.cpp 目录（含 llama-server.exe）",
                                    initialdir=self.llama_dir.get() or os.path.expanduser("~"))
        if d:
            self.llama_dir.delete(0, "end")
            self.llama_dir.insert(0, d)
            self._scan_models()

    def _autodetect(self):
        d = localserver.autodetect_llama_dir()
        if d:
            self.llama_dir.delete(0, "end")
            self.llama_dir.insert(0, d)
            self._scan_models()
        else:
            messagebox.showinfo("未找到", "常见位置未发现 llama.cpp，请手动浏览选择。", parent=self)

    def _browse_extra(self):
        d = filedialog.askdirectory(title="选择模型所在文件夹",
                                    initialdir=self.extra_dir.get() or os.path.expanduser("~"))
        if d:
            self.extra_dir.delete(0, "end")
            self.extra_dir.insert(0, d)
            self._scan_models()

    def _collect(self):
        self.cfg.setdefault("local", {})
        self.cfg["local"]["llama_dir"] = self.llama_dir.get().strip()
        self.cfg["extra_model_dir"] = self.extra_dir.get().strip()
        self.cfg["local"]["gpu"] = bool(self.gpu.get())
        try:
            self.cfg["local"]["ctx"] = max(2048, int(self.ctx.get()))
        except ValueError:
            pass
        try:
            self.cfg["local"]["port"] = int(self.port.get())
        except ValueError:
            pass
        if self.model_cb.current() >= 0 and self._models:
            self.cfg["local"]["model_path"] = self._models[self.model_cb.current()][0]
        # 在线接口配置
        self.cfg["llm"] = {
            "base_url": self.llm_base.get().strip(),
            "api_key": self.llm_key.get().strip(),
            "model": self.llm_model.get().strip(),
        }
        self.cfg["baidu"] = {"appid": self.bd_id.get().strip(), "key": self.bd_key.get().strip()}
        self.cfg["deepl"] = {"api_key": self.dl_key.get().strip(), "free": bool(self.dl_free.get())}
        self.cfg["mymemory_email"] = self.mm_mail.get().strip()
        # 术语表：每行 英文=中文
        gloss = {}
        for raw in self.glossary_text.get("1.0", "end").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and v and len(k) <= 100 and len(v) <= 100:
                gloss[k] = v
        self.cfg["glossary"] = gloss

    def _scan_models(self):
        self._collect()
        self._models = localserver.find_gguf_models(self.cfg)
        names = [f"{os.path.basename(p)}（{s / 1024 ** 3:.1f} GB）" for p, s in self._models]
        self.model_cb.configure(values=names)
        cur = self.cfg.get("local", {}).get("model_path", "")
        idx = next((i for i, (p, _) in enumerate(self._models) if p == cur),
                   0 if self._models else -1)
        self.model_cb.current(max(idx, 0) if self._models else -1)
        if not self._models:
            self.model_cb.set("（未找到 GGUF 模型）")
        self._refresh_status()

    def _refresh_status(self):
        """异步刷新服务状态。

        is_running 对"外部/复用的"实例会做网络探测，且给的是长超时
        （localserver._HEALTH_TIMEOUT = 15 秒，因为对方可能正在生成而腾不出
        HTTP 线程），放 Tk 主线程会把界面冻住十几秒。故必须丢到后台线程。
        """
        self.srv_status.set("服务状态：检查中…")
        cfg = dict(self.cfg)

        def work():
            try:
                up = localserver.is_running(cfg)
            except Exception:
                up = False
            text = ("服务状态：运行中 ✓（可直接测试或开始翻译）" if up
                    else "服务状态：未运行（点击「启动本地服务」，翻译时也会自动启动）")
            self.after(0, lambda m=text: self.srv_status.set(m))

        threading.Thread(target=work, daemon=True).start()

    def _start_server(self):
        self._collect()
        self.srv_status.set("服务状态：正在启动并加载模型…")

        def work():
            try:
                base = localserver.ensure_server(self.cfg, log=lambda s: None)
                msg = f"服务状态：运行中 ✓（{base}）"
            except Exception as e:
                # 必须立刻取出成普通变量：except 结束后 e 会被解绑，
                # 延迟执行的 lambda 引用 e 会抛 NameError
                msg = f"服务状态：启动失败 —— {e}"
            self.after(0, lambda m=msg: self.srv_status.set(m))

        threading.Thread(target=work, daemon=True).start()

    def _stop_server(self):
        """异步停止：stop_server 内含 terminate + wait(最长 10 秒)，不能占着主线程。"""
        self.srv_status.set("服务状态：正在停止…")

        def work():
            try:
                localserver.stop_server()
            except Exception:
                pass
            self.after(0, self._refresh_status)

        threading.Thread(target=work, daemon=True).start()

    def _test(self):
        self._collect()
        # 必须传引擎 id（签名是 test_connection(engine, cfg)）；打包为 --windowed 时
        # 无控制台，主线程抛异常用户只会看到"点了没反应"。
        # local 分支内部走 15 秒长超时探针，故放后台线程，避免冻住界面。
        cfg = copy.deepcopy(self.cfg)          # 不让引擎回写污染界面上的配置
        self.test_btn.config(state="disabled", text="测试中…")

        def work():
            try:
                ok, msg = engines.test_connection("local", cfg)
            except Exception as e:            # 后台线程里的异常必须自己接住
                ok, msg = False, f"测试异常：{type(e).__name__}: {e}"
            self.after(0, lambda: self._test_done(ok, msg))

        threading.Thread(target=work, daemon=True).start()

    def _test_done(self, ok, msg):
        """回到 Tk 主线程再动界面：messagebox 不能在子线程里弹。"""
        self.test_btn.config(state="normal", text="测试翻译")
        (messagebox.showinfo if ok else messagebox.showwarning)("测试结果", msg, parent=self)

    def _save(self):
        self._collect()
        try:
            save_config(self.cfg)
        except RuntimeError as e:
            messagebox.showerror("保存失败", str(e), parent=self)
            return
        self.saved = True
        self.destroy()


def run_gui():
    App().mainloop()
