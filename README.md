# TransPDF — PDF 论文离线翻译器

把英文学术论文 PDF 整体替换为中文，保持原有排版，全程离线运行，论文内容不出本机。
同时针对计科与游戏方向做了一些学术翻译优化。

---

## 目录

- [一、项目简介](#一项目简介)
- [二、功能说明](#二功能说明)
- [三、环境要求](#三环境要求)
- [四、安装与使用](#四安装与使用)
- [五、目录结构](#五目录结构)
- [六、配置项说明](#六配置项说明)
- [七、开发与验证](#七开发与验证)
- [八、注意事项与已知限制](#八注意事项与已知限制)
- [九、许可](#九许可)

---

## 一、项目简介

**按文本块原位替换**——提取带坐标的文本块，识别出需要翻译的外文块，抹除原文后在同一区域写回中文，公式与引用编号原样保留。

翻译可以由**本机运行的大语言模型**完成，论文内容也不会离开电脑。也可以改用在线接口。

---

## 二、功能说明

### 翻译引擎（六选一，或自动回退）

| 引擎 | 说明 |
|---|---|
| **local**（默认） | 本机 llama.cpp + GGUF 模型 |
| **llm** | OpenAI 兼容的云端大模型接口，翻译质量最佳 |
| **baidu** | 百度翻译开放平台 API，按量计费 |
| **deepl** | DeepL API，欧洲语系质量突出 |
| **mymemory** | 免注册的免费在线接口，有每日限额 |
| **auto** | 本地优先；不可用或失败时按 `llm → baidu → deepl → mymemory` 顺序自动切换。本次运行内失败的引擎会自动跳过，不再重试 |

> `auto` 模式的本地优先并非"能跑就用"：**整批全部翻译失败时会主动抛错**触发切换。

### 学术翻译优化

- 内置学术论文提示词：术语用标准译名、首次出现括注英文、符合中文学术表达
- 公式、变量、引用编号、专有名词原样保留
- **领域自适应**：自动识别文档主题并注入对应译名规范，术语按子领域分组维护
  - *计算机科学*（9 个子领域 / 145 条）：机器学习与训练范式、模型与架构、训练与优化、评测指标、系统与性能、软件工程、算法与数据结构、数据库与网络、安全与隐私；行内代码（`env.step()`、`max_pool_size` 等函数名/标识符/命令/路径）一律保留原文
  - *游戏 / 电竞*（9 个子领域 / 148 条）：品类与玩法、核心机制与系统、关卡与内容、游戏 AI 与战斗、图形与渲染、网络与同步、竞技与电竞、运营与商业化、制作与测试；游戏名使用官方中文名（League of Legends → 英雄联盟），无官方译名则保留英文（StarCraft II、Dota 2）
  - 术语块设字符上限（单领域 6000 字符），超出时按子领域整组省略并在日志中提示，避免挤占本地模型的上下文窗口
- **自定义术语表**：支持全文强制生效，优先级最高（见[配置项说明](#六配置项说明)）

### 版面保护与排版适配

- 按文本块原位替换，双栏论文不串栏
- 公式符号行、网址行、行内代码自动跳过不译
- 扫描/竖排/旋转内容保护
- 译文写回原区域，放不下时**自动缩小字号**
- 嵌入中文字体（思源黑体），任何设备打开都正常显示

### 硬件自检与容错

- 启动时检测物理内存与显卡显存（纯 ctypes 读取，无需安装任何驱动检测工具），明确告知能否流畅运行
- 模型幻觉输出自动拦截（保留原文）
- 加密 PDF、无文字层扫描件给出明确提示
- 本地服务进程由 Windows Job Object 托管：**即使程序被强制结束，也不会残留占用十几 GB 内存的推理进程**

---

## 三、环境要求

| 项目 | 要求 |
|---|---|
| 操作系统 | Windows 10 / 11 x64 |
| 显卡 | 显存 ≥ 12GB 可全速运行（N 卡 / A 卡 / 核显均可，走 Vulkan）；无独显时纯 CPU 运行 |
| 内存 | 纯 CPU 运行建议 ≥ 24GB；显存不足需拆分时 ≥ 16GB |
| 磁盘 | 模型体积 + 2GB（高质档模型 12~17GB） |
| PDF | **必须有文字层**（扫描件需先 OCR） |

### 关于发布包体积

模型权重占发布包体积的 99%（程序本体与推理引擎合计约 146MB）。如需减小体积只能换模型，代价是翻译质量：

| 方案 | 体积 | 质量影响 |
|---|---|---|
| 现行：Qwen3-30B-A3B Q3_K_S | 12.4GB | 基准（高质档） |
| 同模型 IQ3_XXS 量化 | ≈11.3GB | 轻微下降 |
| 改用 Qwen3-14B Q4_K_M | ≈9GB | 明显下降一档 |
| 改用 Qwen3-8B Q4_K_M | ≈5GB | 学术翻译显著变差，不建议 |

> 分发到 FAT32 U 盘时单文件不能超 4GB，可用 7-Zip 分卷（`模型.gguf.7z.001/002/…`，但不会减小总体积）。

---

## 四、安装与使用

### 4.1 使用发布包

1. 解压发布包到任意目录（路径**不要含中文与空格**，避免个别环境下路径解析问题）
2. 把 GGUF 模型（`.gguf`）放入 `models/` 文件夹
3. 双击 `TransPDF.exe`
4. 选择 PDF → 点击「开始翻译」→ 完成后点「打开译文」

译文与原文件同目录，文件名以 `_中文翻译` 结尾。

**首次使用提示**：首次翻译需要把模型加载进显存/内存，耗时几十秒到几分钟；之后连续翻译无需重复加载。

### 4.2 从源码运行

```bat
:: 1. 安装依赖
pip install -r requirements.txt

:: 2. 启动图形界面
python main.py
```

从源码运行时，程序会在**项目根目录**查找 `models/`、`runtime/` 与 `config.json`。

模型与推理引擎需要自行准备：

- **推理引擎**：下载 llama.cpp 的 Windows 预编译包，取其中的 `llama-server.exe` 及依赖 DLL 放到 `runtime/`
- **模型**：下载 GGUF 格式模型放入 `models/`
  - 推荐：`Qwen3-30B-A3B-Instruct-2507`（Q3_K_S 量化，约 12.4GB，本项目基准模型）
  - 在 Hugging Face 或 ModelScope 搜索模型名 + `GGUF` 即可找到社区量化版本
  - 显存不足 12GB 时选更小量化（IQ3_XXS）或更小模型（Qwen3-14B / 8B），代价是翻译质量下降
  - 务必选 **Instruct**（指令微调）版本，基座模型无法按提示词要求输出

> **模型为什么不随仓库分发**：单个模型文件 12GB+，远超 GitHub 单文件 100MB 的硬上限，
> 即使付费版（Git LFS 5GB）也无法承载。请从模型社区自行下载。

也可以在界面「高级设置」中直接指定电脑上已有的 llama.cpp 目录与模型位置。

### 4.3 构建发布包

```bat
:: 一键构建（安装依赖 → 准备字体 → PyInstaller 打包 exe）
build.bat

:: 组装发布包（复制 exe + llama.cpp 运行时 + 模型）
python tools\assemble_release.py --llama <llama.cpp目录> --model <模型.gguf>
```

- `build.bat` 使用 PyInstaller 内联参数（`--onefile --windowed`）打包，产物为 `dist\TransPDF.exe`；`TransPDF.spec` 是等价的配置文件，也可用 `pyinstaller TransPDF.spec` 打包
- 若 `fonts/` 下的 TTF 缺失，`build.bat` 会自动从 jsdelivr 下载思源黑体 OTF 并用 `tools/otf2ttf.py` 转成 TTF
- `assemble_release.py` 默认输出到项目下的 `release/TransPDF发布包/`；加 `--no-compact` 可保留 runtime 全部文件（默认只保留 llama-server 运行必需的 24 个）

### 4.4 命令行用法

```bat
TransPDF.exe                                    打开图形界面
TransPDF.exe --version                          显示版本
TransPDF.exe --selftest                         无界面自检（生成样例论文并离线翻译一遍）
TransPDF.exe --input paper.pdf                  输出 paper_中文翻译.pdf
TransPDF.exe --input paper.pdf --output out.pdf 指定输出路径
TransPDF.exe --input paper.pdf --engine auto    指定引擎
```

源码运行时把 `TransPDF.exe` 换成 `python main.py` 即可。

**退出码**（便于自动化判断）：

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 运行环境缺失（tkinter / 硬件 / 模型 / 引擎） |
| 2 | 流程执行或结果校验失败 |

---

## 五、目录结构

### 5.1 源码仓库

```
TransPDF/
├── main.py                     入口：GUI / 命令行 / 自检
├── requirements.txt            运行与构建依赖
├── TransPDF.spec               PyInstaller 打包配置
├── build.bat                   一键构建脚本
├── glossary.txt                （可选）默认术语表，每行「英文=中文」
├── LICENSE                     MIT
├── README.md                   本文件
├── transpdf/                   程序包
│   ├── gui.py                  tkinter 图形界面
│   ├── pdfproc.py              PDF 处理核心：提取 → 识别 → 翻译 → 抹除原文 → 回写中文
│   ├── engines.py              翻译引擎层 + 自动回退 + 领域自适应
│   ├── localserver.py          llama.cpp 服务管理（启停、降级重试、进程回收）
│   ├── hardware.py             硬件自检（内存 + DXGI 显存枚举）+ GGUF 显存需求估算
│   ├── config.py               配置读写
│   └── sample.py               测试样例 PDF 生成
├── tools/                      开发与构建工具
│   ├── assemble_release.py     发布包组装
│   ├── otf2ttf.py              OTF → TTF 字体转换
│   ├── verify_static.py        回归验证（静态 + 纯逻辑）
│   └── verify_runtime.py       回归验证（运行时行为）
├── fonts/                      思源黑体（OFL 许可，可随包分发；缺失时 build.bat 会自动下载）
└── .gitignore
```

### 5.2 发布包结构

```
TransPDF发布包/
├── TransPDF.exe        程序本体
├── runtime/            llama.cpp 推理引擎（含 llama-server.exe）
├── models/             GGUF 模型权重
├── config.json         程序配置（自动生成）
└── 使用说明.txt
```

---

## 六、配置项说明

配置文件 `config.json` 位于**程序同目录**（打包后为 exe 所在目录，源码运行为项目根目录），首次保存设置时自动生成。所有项都可以在界面「高级设置」中修改，一般无需手工编辑。

### 顶层

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `engine` | string | `"local"` | 翻译引擎：`local` / `auto` / `llm` / `baidu` / `deepl` / `mymemory` |
| `extra_model_dir` | string | `""` | 额外的模型扫描目录（除 `models/` 外再扫这里） |
| `glossary` | object | `{}` | 自定义术语表，形如 `{"transformer": "变换器"}`，全文强制生效、优先级最高 |
| `mymemory_email` | string | `""` | 免费接口的可选邮箱，填写可提升每日额度 |
| `last_dir` | string | `""` | 上次打开文件的目录（程序自动维护，无需修改） |

### `local` — 本地模型

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `llama_dir` | string | `""` | llama.cpp 目录。留空则自动探测 `runtime/`、`llama.cpp/` 及 `?:\LModel\llama.cpp` |
| `model_path` | string | `""` | GGUF 模型完整路径。留空则自动扫描各模型目录，**按显存装得下的最大模型**择优 |
| `gpu` | bool | `true` | 是否启用显卡加速（Vulkan）。关闭后强制纯 CPU |
| `ctx` | int | `8192` | 上下文长度。调小可显著省显存，但过长段落会被截断 |
| `port` | int | `18080` | 本地服务端口。被其它程序占用时改这里 |

### `llm` — OpenAI 兼容在线大模型

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `base_url` | string | `""` | 接口地址，如 `https://open.bigmodel.cn/api/paas/v4` |
| `api_key` | string | `""` | API 密钥 |
| `model` | string | `""` | 模型名，如 `glm-4-flash`、`deepseek-chat` |

### `baidu` / `deepl` / `mymemory`

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `baidu.appid` | string | `""` | 百度翻译开放平台 APP ID |
| `baidu.key` | string | `""` | 百度翻译开放平台密钥 |
| `deepl.api_key` | string | `""` | DeepL 密钥 |
| `deepl.free` | bool | `true` | 是否使用 DeepL 免费版端点（Free 版密钥须保持开启） |

### 术语表文件 `glossary.txt`

除在界面填写外，还可以在**程序同目录**放一个 `glossary.txt`，两种来源会合并生效：

```text
# 以 # 开头的行为注释，空行忽略
transformer=变换器
ablation study=消融实验
fine-tuning=微调
```

规则：每行 `英文=中文`，两侧空白自动去除；`=` 只按**第一个**切分，因此译文中可以包含 `=`。单条英/中文长度上限 100 字符，总条数上限 **400 条**，超出部分会被丢弃并在日志中提示。

---

## 七、开发与验证

项目自带两层回归验证脚本，克隆后可直接重跑（路径自适应，不依赖本机绝对路径）：

```bat
python tools\verify_static.py     :: 第 1 层：语法编译 + 模块导入 + 纯逻辑单测
python tools\verify_runtime.py    :: 第 2 层：运行时行为（需要模型文件）
```

两者均以「失败项数 + 硬错误项数」双零为通过判据，退出码非 0 即失败。

**环境提示**：验证需要 `tkinter` 与 `PyMuPDF` 同时可用。若所用解释器缺 `tkinter`（精简版或部分 venv 常见），可借系统 Python 建一个 venv——venv 会继承 base 的标准库，于是 `tkinter` 一并可用：

```bat
python -m venv <venv路径>
<venv路径>\Scripts\pip install pymupdf requests
```

运行时测试全部在临时目录内执行，不写程序目录、不触碰用户数据；除只读的 `netstat` / `tasklist` 外无任何实际进程操作。

### 实现要点

- **替换原理**：PyMuPDF 提取带坐标文本块 → 识别外文块 → redaction 抹除原文 → 原区域回写中文（手工 CJK 折行 / 避头尾 + 自动缩字号）
- **为何不用 MuPDF 的 HTML 排版引擎**：实测其对 CFF 轮廓字体字形错乱、对部分 cmap 结构的 ASCII 数字编码异常，故采用手工折行 + 原生文本写入
- **本地服务存活判定**：自建实例用进程 `poll()` 判定而非网络探针——llama-server 生成长文本时可能腾不出 HTTP 线程，把"探针超时"当成"服务不存在"会导致重复加载十几 GB 模型
- **进程回收**：atexit + 显式关闭 + Windows Job Object（`KILL_ON_JOB_CLOSE`）三重保障

---

## 八、注意事项与已知限制

### 使用注意

- **必须是有文字层的 PDF**。扫描件（图片型）无法翻译，需先用 OCR 工具处理
- 加密 PDF 请先解除密码
- 程序路径与论文路径建议**不含特殊字符**，避免个别环境下路径解析问题
- 纯 CPU 运行速度较慢，长论文建议预留充足时间
- 本地服务端口（默认 18080）若被占用，程序会给出占用者信息，可在「高级设置」改用其它端口
- 若检测到端口上已有推理服务，程序会**直接复用**而不是重新加载模型，避免重复占用显存

### 已知限制

| 限制 | 说明 |
|---|---|
| 扫描件不支持 | 无文字层，需先 OCR |
| 旋转 / 竖排文本块 | 跳过不译，避免版面错乱 |
| 少数字符复制为兼容字形 | 视觉正常，如「句」可能复制出兼容形式 |
| 复杂表格 | 单元格文字按块翻译，版面可能略有出入 |
| 复杂公式行 | 整体跳过（保护不译）；行内混排公式随句子翻译可能变形 |
| 在线接口 | 有速率与额度限制，`mymemory` 免费额度较低 |

### 隐私说明

- 选择 **local** 引擎时全程离线，论文内容不产生任何网络请求
- 选择在线引擎（`llm` / `baidu` / `deepl` / `mymemory`）时，**被翻译的文本会发送到对应服务商**，请自行评估论文的保密要求
- `config.json` 中保存的 API 密钥为明文，请勿把该文件分享给他人（本项目已在 `.gitignore` 中排除）

---

## 九、许可

- 本项目代码：[MIT](LICENSE)
- 思源黑体（Source Han Sans）：SIL Open Font License，可随本软件分发
- llama.cpp：[MIT](https://github.com/ggml-org/llama.cpp)（发布包 `runtime/` 目录）
