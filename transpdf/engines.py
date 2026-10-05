"""翻译引擎层。

引擎：
- local    : 本地大模型（llama.cpp / GGUF，默认，纯离线）
- llm      : OpenAI 兼容云端大模型接口（质量最佳，学术推荐）
- baidu    : 百度翻译开放平台 API
- deepl    : DeepL API
- mymemory : 免费在线接口（免注册，有每日限额）
- auto     : 本地优先，失败自动切换到已配置的在线接口（末位兜底 mymemory）

领域自适应（计算机/游戏等术语规范）与自定义术语表对 local 和 llm 引擎生效；
baidu/deepl/mymemory 为纯机器翻译接口，不支持提示词。
本模块仅在用户选择在线引擎时才发起外部网络请求。
"""
import hashlib
import os
import re
import threading
import time

import requests

from . import localserver

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}


class TranslationError(Exception):
    pass


ENGINE_LABELS = {
    "local": "本地模型（离线，默认）",
    "auto": "自动（本地优先，失败改用在线接口）",
    "llm": "在线大模型 API",
    "baidu": "百度翻译 API",
    "deepl": "DeepL API",
    "mymemory": "免费在线接口（无需配置）",
}


# --------------------------------------------------------------------------
# 学术提示词与领域自适应
# --------------------------------------------------------------------------

ACADEMIC_PROMPT = """你是一位资深的学术论文翻译专家，精通各学科领域的英文学术文献（尤其是地球科学、能源、工程领域），负责将文献翻译成简体中文。翻译要求：
1. 术语准确：使用该领域的标准中文译名和行业规范写法，符合中文科技论文的表达习惯；重要术语首次出现时可在译名后用括号标注英文原文，例如「注意力机制（attention）」。
2. 地层学与地质年代规范：Paleozoic / Mesozoic / Cenozoic 在指地层或储层时译「古生界 / 中生界 / 新生界」，仅当明确指地质年代时才译「代」；Formation 译「组」，Member 译「段」，中国地层的通行段名保留原写法（如 He 8 Member 译「盒8段」）。
3. 中国地名、地层名、井名等汉语拼音专名必须回译为地质文献通行的标准汉字（如 Yanghugou Formation → 羊虎沟组，Wulalike Formation → 乌拉力克组，Hengshanbao → 横山堡，well YT3 → YT3 井）；无法确定标准写法时保留英文原名并在括号内标注拼音。
4. 常见专业术语按行业规范翻译，不要自创直译，例如：back thrust → 后冲带（后冲断层），imbricate belt → 叠瓦冲断带，structural style → 构造样式，strike（地质）→ 走向，source-reservoir configuration → 源-储配置，tight gas → 致密气，grain beach → 颗粒滩，self-generation and self-storage → 自生自储，trap → 圈闭，hydrocarbon accumulation model → 油气成藏模式。
5. 数学公式、变量名、函数名、代码、单位、URL、文件名、参考文献引用编号（如 [12]、(Smith et al., 2020)）一律原样保留，不作翻译；数字与单位保持原文写法（如 1 800 km²、410 km²）。
6. 专有名词（人名、机构名、数据集名、模型名、系统名）保留英文原文。
7. 语句严谨、书面化，符合中文学术论文表达习惯；长句可按中文习惯拆分重组，但不得增删原意；不要使用「如上所述」「上文提到」等指代原文行文结构的措辞，直接按事实陈述。
8. 只输出译文正文，不要输出任何解释、列表符号、省略号、前缀或后缀；译文必须是连贯成段的完整语句。"""


DOMAIN_PROFILES = {
    "cs": {
        "label": "计算机科学",
        "keywords": [
            r"neural network", r"machine learning", r"deep learning",
            r"reinforcement learning", r"gradient", r"dataset", r"benchmark",
            r"compiler", r"throughput", r"latency", r"\bcache\b", r"thread",
            r"kernel", r"distributed system", r"\bGPU\b", r"\bCPU\b",
            r"inference", r"fine-tuning", r"training loss", r"accuracy",
            r"algorithm", r"complexity", r"API", r"ablation",
        ],
        "terms": (
            "【计算机领域术语规范】machine learning 机器学习；deep learning 深度学习；"
            "neural network 神经网络；reinforcement learning 强化学习；supervised/unsupervised learning "
            "监督学习/无监督学习；fine-tuning 微调；inference 推理；overfitting 过拟合；"
            "generalization 泛化；token 词元；prompt 提示词；embedding 嵌入表示；"
            "attention mechanism 注意力机制；benchmark 基准测试；dataset 数据集；"
            "throughput 吞吐量；latency 延迟；cache 缓存；thread 线程；process 进程；"
            "concurrency 并发；parallelism 并行；distributed system 分布式系统；"
            "compiler 编译器；runtime 运行时；memory 内存；bandwidth 带宽；overhead 开销；"
            "robustness 鲁棒性；scalability 可扩展性；accuracy/precision/recall "
            "准确率/精确率/召回率；ablation study 消融实验；state-of-the-art 最先进的。"
            "正文中的行内代码一律原样保留：函数名与 API（如 env.step()、getUserData）、"
            "下划线/驼峰标识符（如 max_pool_size、BatchNorm）、命令与文件路径不翻译。"
        ),
    },
    "game": {
        "label": "游戏/电竞",
        "keywords": [
            r"\bgame\b", r"gameplay", r"\bplayer", r"\bRTS\b", r"\bMOBA\b",
            r"\bFPS\b", r"esport", r"StarCraft", r"\bDota\b", r"League of Legends",
            r"game engine", r"pathfinding", r"level design", r"matchmaking",
            r"\bNPC\b", r"playtest", r"gaming", r"\bHUD\b", r"roguelike",
        ],
        "terms": (
            "【游戏领域术语规范】real-time strategy (RTS) 即时战略；MOBA 多人在线战术竞技；"
            "first-person shooter (FPS) 第一人称射击；MMO 大型多人在线；gameplay 玩法；"
            "game mechanics 游戏机制；game engine 游戏引擎；physics engine 物理引擎；"
            "rendering 渲染；frame rate 帧率；tick rate 逻辑帧率；pathfinding 寻路；"
            "navigation mesh 导航网格；procedural content generation 程序化内容生成；"
            "level design 关卡设计；unit 单位；micro-operation 微操作；macro-management 宏观运营；"
            "matchmaking 匹配系统；lag compensation 延迟补偿；rollback 回滚同步；"
            "esports 电子竞技；player 玩家；NPC 非玩家角色（NPC）；HUD 平视显示界面（HUD）；"
            "playtesting 玩家测试；player retention 玩家留存；monetization 付费变现；"
            "loot box 开箱；gacha 抽卡；Elo rating Elo 等级分；win rate 胜率；"
            "action space 动作空间；observation space 观测空间；self-play 自博弈。"
            "游戏名称是专有名词：有官方中文名的必须用官方名（League of Legends 英雄联盟、"
            "Overwatch 守望先锋、Minecraft 我的世界、CrossFire 穿越火线）；"
            "没有通行官方译名的保留英文原名（如 StarCraft II、Dota 2、Civilization VI）。"
        ),
    },
}

_GLOSSARY_MAX = 400


def detect_domains(text):
    """按关键词命中次数判断文档领域，返回命中的领域 id 列表。"""
    text_low = text.lower()
    active = []
    for did, prof in DOMAIN_PROFILES.items():
        hits = sum(len(re.findall(k, text_low)) for k in prof["keywords"])
        if hits >= 5:
            active.append(did)
    return active


def _load_glossary(cfg):
    """合并配置中的术语表与程序同目录 glossary.txt（每行 英文=中文）。

    返回 (术语表, 因超出上限被丢弃的条数)。丢弃条数交给调用方记日志——
    静默截断会让用户"明明填了却不生效"却查不出原因。
    """
    gloss = {}
    try:
        for k, v in (cfg.get("glossary") or {}).items():
            k, v = str(k).strip(), str(v).strip()
            if k and v and len(k) <= 100 and len(v) <= 100:
                gloss[k] = v
    except Exception:
        pass
    try:
        from .config import app_dir
        p = os.path.join(app_dir(), "glossary.txt")
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if k and v and len(k) <= 100 and len(v) <= 100:
                        gloss[k] = v
    except Exception:
        pass
    items = list(gloss.items())
    dropped = max(0, len(items) - _GLOSSARY_MAX)
    return dict(items[:_GLOSSARY_MAX]), dropped


def build_system_prompt(cfg, domains=None, log=None):
    """组装系统提示词：学术底版 + 领域术语块 + 用户术语表。"""
    parts = [ACADEMIC_PROMPT]
    for did in domains or []:
        prof = DOMAIN_PROFILES.get(did)
        if prof:
            parts.append(prof["terms"])
    gloss, dropped = _load_glossary(cfg)
    if dropped and log:
        log(f"[术语表] 超过 {_GLOSSARY_MAX} 条上限，已忽略后 {dropped} 条，"
            f"请精简术语表或使用 glossary.txt 分批维护。")
    if gloss:
        lines = ["【必须遵守的术语表】以下术语全文使用指定译名："]
        # 与加载上限保持一致，不再二次截断
        for k, v in gloss.items():
            lines.append(f"- {k} → {v}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# 公共
# --------------------------------------------------------------------------

def _strip_reasoning(text):
    """去掉部分推理模型夹带的思考块与包裹引号。"""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"“”'‘’":
        text = text[1:-1].strip()
    return text


_HAN_RE = re.compile(r"[\u4e00-\u9fff]")


def _plausible(src, dst):
    """译文的合理性检查：必须含汉字、长度不超原文的合理倍数。"""
    dst = _strip_reasoning(dst or "")
    if not dst:
        return False
    if not any(_HAN_RE.match(ch) for ch in dst):
        return False
    if len(dst) > max(len(src) * 4, len(src) + 60):
        return False
    return True


# --------------------------------------------------------------------------
# 本地大模型（默认，离线）
# --------------------------------------------------------------------------

def local_translate(texts, cfg, log=None, domains=None):
    base_url = localserver.ensure_server(cfg, log) + "/v1"
    system_prompt = build_system_prompt(cfg, domains, log)
    use_kwarg = localserver.jinja_ok()
    out, errors = [], 0
    for t in texts:
        result = ""
        for attempt in (0, 1):
            try:
                content = _chat_once(base_url, system_prompt, t, 600,
                                     use_kwarg and attempt == 0)
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else 0
                if attempt == 0 and status in (400, 422, 500):
                    use_kwarg = False  # 服务端不认思考开关，去掉重试
                    continue
                errors += 1
                log(f"[翻译] 本地模型返回 HTTP {status}，该段保留原文。")
                break
            except Exception as e:
                errors += 1
                log(f"[翻译] 本地模型调用失败（{e!r}），该段保留原文。")
                break
            if _plausible(t, content):
                result = _strip_reasoning(content)
                break
            if attempt == 0:
                continue  # 空译文/疑似幻觉，换参数重试一次
            errors += 1
            log("[翻译] 译文异常（为空或疑似编造），该段保留原文。")
        out.append(result)
    if texts and errors == len(texts):
        # 整批全失败（服务在但出不来译文）必须抛错：静默返回空译文会让 auto 模式
        # 不切换在线引擎，用户拿到一份"翻译完成"却全是原文的文件。
        raise TranslationError(
            "本地模型连续翻译失败（服务无响应或资源不足），"
            "已自动改用其他可用引擎。")
    return out


def _chat_once(base_url, system_prompt, text, timeout, use_think_kwarg):
    payload = {
        "model": "local",
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
    }
    if use_think_kwarg:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    r = requests.post(base_url + "/chat/completions", json=payload, timeout=timeout,
                      headers={"Content-Type": "application/json"})
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


# --------------------------------------------------------------------------
# 在线大模型（OpenAI 兼容）
# --------------------------------------------------------------------------

def llm_translate(texts, cfg, log=None, domains=None):
    llm = cfg.get("llm", {})
    base = (llm.get("base_url") or "").rstrip("/")
    key = llm.get("api_key") or ""
    model = llm.get("model") or ""
    if not base or not key:
        raise TranslationError("大模型接口未配置完整（需要接口地址和 API Key），可在「高级设置 → 在线接口」中配置")
    system_prompt = build_system_prompt(cfg, domains, log)
    out = []
    for t in texts:
        payload = {
            "model": model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": t},
            ],
        }
        try:
            r = requests.post(base + "/chat/completions",
                              headers={"Authorization": "Bearer " + key,
                                       "Content-Type": "application/json"},
                              json=payload, timeout=120)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            detail = e.response.text[:200] if e.response is not None else ""
            raise TranslationError(f"大模型接口 HTTP {status}：{detail}") from e
        except Exception as e:
            raise TranslationError(f"大模型接口调用失败：{e!r}") from e
        content = _strip_reasoning(content)
        if not _plausible(t, content):
            # 与本地引擎同款幻觉拦截：云模型对短输入同样可能编造，异常译文保留原文
            log("[翻译] 在线大模型译文异常（为空或疑似编造），该段保留原文。")
            out.append("")
            continue
        out.append(content)
    return out


# --------------------------------------------------------------------------
# 百度翻译开放平台
# --------------------------------------------------------------------------

_baidu_lock = threading.Lock()
_baidu_last = [0.0]


def baidu_translate(texts, cfg, log=None, domains=None):
    b = cfg.get("baidu", {})
    appid, key = b.get("appid") or "", b.get("key") or ""
    if not appid or not key:
        raise TranslationError("百度翻译 API 未配置完整（需要 APPID 和密钥）")
    joined = "\n".join(t.replace("\n", " ").strip() for t in texts)
    if len(joined.encode("utf-8")) > 5500:
        raise TranslationError("百度单次请求文本过长")
    with _baidu_lock:  # 免费标准版 QPS=1
        wait = 1.1 - (time.time() - _baidu_last[0])
        if wait > 0:
            time.sleep(wait)
        _baidu_last[0] = time.time()
    salt = str(int(time.time() * 1000))
    sign = hashlib.md5((appid + joined + salt + key).encode("utf-8")).hexdigest()
    r = requests.get(
        "https://fanyi-api.baidu.com/api/trans/vip/translate",
        params={"q": joined, "from": "auto", "to": "zh", "appid": appid,
                "salt": salt, "sign": sign},
        timeout=30, headers=UA,
    )
    r.raise_for_status()
    data = r.json()
    if "error_code" in data:
        raise TranslationError(f"百度翻译 API 错误 {data['error_code']}：{data.get('error_msg', '')}")
    dst_lines = "\n".join(item["dst"] for item in data.get("trans_result", []))
    parts = dst_lines.split("\n")
    if len(parts) != len(texts):
        raise TranslationError("百度翻译返回行数与请求不一致")
    return parts


# --------------------------------------------------------------------------
# DeepL
# --------------------------------------------------------------------------

def deepl_translate(texts, cfg, log=None, domains=None):
    d = cfg.get("deepl", {})
    key = d.get("api_key") or ""
    if not key:
        raise TranslationError("DeepL API 未配置 API Key")
    host = "https://api-free.deepl.com" if d.get("free", True) else "https://api.deepl.com"
    r = requests.post(
        host + "/v2/translate",
        headers={"Authorization": "DeepL-Auth-Key " + key},
        data={"text": texts, "target_lang": "ZH"},
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    out = [item["text"] for item in data.get("translations", [])]
    if len(out) != len(texts):
        raise TranslationError("DeepL 返回条数与请求不一致")
    return out


# --------------------------------------------------------------------------
# MyMemory（免费在线）
# --------------------------------------------------------------------------

def _chunk_utf8(text, limit=440):
    """按 UTF-8 字节数切块。

    MyMemory 的 q 参数按**字节**限制（500 字节），旧实现按字符切 440，
    源文含非 ASCII 字符（俄文/希腊文/带重音的拉丁文）时会超出上限被拒。
    """
    out, cur, n = [], [], 0
    for ch in text:
        b = len(ch.encode("utf-8"))
        if cur and n + b > limit:
            out.append("".join(cur))
            cur, n = [], 0
        cur.append(ch)
        n += b
    if cur:
        out.append("".join(cur))
    return out or [""]


def _strip_markup(text):
    """MyMemory 部分响应带内联 XML 标记（<g>/<x>/<bx>），剥离掉。"""
    return re.sub(r"</?g[^>]*>|<x[^>]*/>|<bx[^>]*/>", "", text or "")


def mymemory_translate(texts, cfg, log=None, domains=None):
    email = cfg.get("mymemory_email") or ""
    out = []
    for t in texts:
        chunks = _chunk_utf8(t, 440)
        parts = []
        for ch in chunks:
            params = {"q": ch, "langpair": "en|zh-CN"}
            if email:
                params["de"] = email
            r = requests.get("https://api.mymemory.translated.net/get",
                             params=params, timeout=30, headers=UA)
            r.raise_for_status()
            j = r.json()
            txt = (j.get("responseData") or {}).get("translatedText") or ""
            if j.get("responseStatus") == 403 or "USED ALL AVAILABLE" in txt.upper():
                raise TranslationError("免费接口今日额度已用完（次日恢复），建议配置大模型或百度/DeepL API")
            parts.append(_strip_markup(txt))
        out.append("".join(parts))
    return out


# --------------------------------------------------------------------------
# 统一入口：引擎选择与自动回退
# --------------------------------------------------------------------------

_ENGINE_FUNCS = {
    "local": local_translate,
    "llm": llm_translate,
    "baidu": baidu_translate,
    "deepl": deepl_translate,
    "mymemory": mymemory_translate,
}

# auto 模式下的云引擎优先级
_CLOUD_ORDER = ["llm", "baidu", "deepl", "mymemory"]

# 本次运行中已失败的引擎（auto 模式跳过）
_failed_engines = set()


def reset_engine_failures():
    _failed_engines.clear()


def configured_cloud_engines(cfg):
    """已配置可用的在线引擎，按优先级排序；mymemory 免配置恒为兜底。"""
    order = []
    llm = cfg.get("llm", {})
    if llm.get("api_key") and llm.get("base_url"):
        order.append("llm")
    if cfg.get("baidu", {}).get("appid") and cfg.get("baidu", {}).get("key"):
        order.append("baidu")
    if cfg.get("deepl", {}).get("api_key"):
        order.append("deepl")
    order.append("mymemory")
    return order


def local_available(cfg):
    """本地引擎是否具备运行条件（模型 + llama-server 均已就位）。"""
    try:
        model = localserver.resolve_model(cfg)
        server = localserver.find_llama_server(
            cfg.get("local", {}).get("llama_dir") or localserver.autodetect_llama_dir())
        return bool(model and server)
    except Exception:
        return False


def translate_texts(texts, cfg=None, log=None, domains=None, engine="local"):
    """翻译一批文本，返回等长译文列表。

    engine="auto"：本地优先，失败自动切换到已配置的在线接口；
    本次运行中失败过的引擎不再重试（由 pdfproc 在任务开始时调用 reset_engine_failures）。
    """
    cfg = cfg or {}
    log = log or (lambda s: None)
    texts = [t.strip() for t in texts]
    if not any(texts):
        return list(texts)
    if engine == "auto":
        chain = (["local"] if local_available(cfg) else []) + configured_cloud_engines(cfg)
    else:
        chain = [engine]
    errors = []
    for eid in chain:
        if eid in _failed_engines:
            continue
        label = ENGINE_LABELS.get(eid, eid)
        try:
            return _ENGINE_FUNCS[eid](texts, cfg, log, domains)
        except TranslationError as e:
            errors.append(f"{label}：{e}")
            _failed_engines.add(eid)
            suffix = "（本次翻译后续内容自动改用其他可用接口）" if engine == "auto" else ""
            log(f"[引擎] {label} 失败：{e}{suffix}")
            if engine != "auto":
                raise
        except Exception as e:
            errors.append(f"{label}：{e!r}")
            _failed_engines.add(eid)
            log(f"[引擎] {label} 异常：{e!r}")
            if engine != "auto":
                raise TranslationError(f"{label} 调用失败：{e}")
    raise TranslationError("所有可用翻译接口均失败：" + "；".join(errors))


def test_connection(engine, cfg):
    """设置界面测试某引擎连通性，返回 (成功?, 消息)。"""
    if engine == "local":
        return _test_local(cfg)
    sample = "Machine learning is a branch of artificial intelligence."
    try:
        out = _ENGINE_FUNCS[engine]([sample], cfg)
        return True, "测试成功，示例译文：" + (out[0][:80] if out and out[0] else "(空)")
    except Exception as e:
        return False, f"测试失败：{e}"


def _test_local(cfg):
    if localserver.is_running(cfg):
        try:
            out = local_translate(["Machine learning is a branch of artificial intelligence."], cfg)
            return True, "本地服务正常，示例译文：" + (out[0][:80] if out and out[0] else "(空)")
        except Exception as e:
            return False, f"本地服务已启动但调用失败：{e}"
    return False, "本地服务尚未启动。已配置 llama.cpp 的话点击「启动本地服务」，翻译时也会自动启动。"
