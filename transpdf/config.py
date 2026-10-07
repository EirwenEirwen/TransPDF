"""配置读写：配置文件保存在程序同目录（exe 目录或项目根目录）。"""
import json
import os
import sys

DEFAULTS = {
    "engine": "local",        # local / auto / llm / baidu / deepl / mymemory
    "local": {
        "llama_dir": "",      # llama.cpp 目录（默认自动探测 runtime/ 或已有安装）
        "model_path": "",     # GGUF 模型文件路径
        "gpu": True,          # 显卡加速（Vulkan）
        "ctx": 8192,          # 上下文长度
        "port": 18080,        # 本地服务端口
    },
    "llm": {                  # OpenAI 兼容在线大模型接口
        "base_url": "",
        "api_key": "",
        "model": "",
    },
    "baidu": {"appid": "", "key": ""},
    "deepl": {"api_key": "", "free": True},
    "mymemory_email": "",     # 免费在线接口的可选邮箱（提升每日限额）
    "figure": {"tool_dir": ""},  # manga-translator-ui 目录（图片文字翻译）
    "do_figures": True,       # 默认同时翻译图片内文字（工具缺失时自动跳过）
    "extra_model_dir": "",    # 额外的模型扫描目录（如 F:\LModel\models）
    "glossary": {},           # 自定义术语表 {"英文": "中文"}（也可用同目录 glossary.txt）
    "last_dir": "",           # 上次打开的文件夹
}


def app_dir():
    """exe 所在目录（打包后）或项目根目录（源码运行）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def config_path():
    return os.path.join(app_dir(), "config.json")


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and k in cfg and isinstance(cfg[k], dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    try:
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError as e:
        raise RuntimeError(f"无法保存配置文件：{e}")
