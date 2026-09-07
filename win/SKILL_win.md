---
name: live-class-skill-win
description: "直播课/线上课程/会议「代听」助手（Windows 版）：用户自行打开直播网页出声 → 用 Windows 系统自带 WASAPI loopback 内录系统声音（零第三方、零浏览器自动化）→ sherpa-onnx 本地离线转写 → 结束后生成结构化总结笔记。当用户说「帮我听/代听这场直播」「把 B站/抖音/小红书直播课录下来转成文字」「没时间上课，结束后给我一份笔记/总结」「替我去听会，结束时给我要点」，且运行环境是 Windows 时使用。适用于任何能在浏览器/App 出声的线上直播课与会议。⚠️ 本技能绝不使用任何浏览器自动化（防封号）：只负责「监听系统声音」，打开直播页由用户自己完成。"
agent_created: true
---

# live-class-skill（Windows 版）— 直播课/会议「代听」助手

用户没时间一直盯直播，由本技能完成：**用户打开直播网页出声 → 内录系统声音 → 本地离线转写 → 结束后总结**。
产出两份文件：① 逐字稿 `transcript.txt`；② 结构化总结 `summary.md`。

> **平台入口**：本文件是 Windows 版完整流程。mac 版见仓库根 `SKILL.md` / `mac` 目录。
> 两版共享同一套离线 ASR 引擎与模型（通用层在仓库根 `scripts/`：`transcribe.py`、`common.py`）。

## ⚠️ 铁律：零浏览器自动化（防封号）

本技能**只监听系统正在播放的声音**，**绝不驱动、注入、控制、读取任何浏览器/App**。
打开直播网页这一步由**用户自己**在系统默认浏览器里完成，或在桌面 App（如腾讯会议）里打开。

- ✅ 允许：提示/指导用户打开某个 URL、确认页面在播放且音量非 0、监听系统声音、离线转写、总结。
- ❌ 禁止：selenium / playwright / puppeteer / webdriver / CDP / 浏览器插件注入 / 模拟点击输入 / 读取页面 DOM / 抓直播流地址。
  一句话：**所有 agent 不得调用任何"浏览器自动化"能力来配合本技能**（详见仓库根 `AGENTS.md`）。

## 核心原理

```
用户在浏览器/App 播放直播声音 ──▶ Windows 系统混音器（默认输出设备）
                                        │ WASAPI loopback（Windows 系统自带 Core Audio 接口，零第三方）
                                        ▼
         win/scripts/_wasapi.py + record.py  内录 → chunks/seg_*.wav（16kHz 单声道 16bit）
                                        │
        根 scripts/transcribe.py（sherpa-onnx 本地离线，与 mac 版共用模型）
                                        ▼
        transcripts/transcript.txt → 主代理生成总结 summary.md
```

关键事实（决定整个流程怎么做）：
- **内录零安装、零第三方**：直接用 Windows 系统自带的 **WASAPI loopback**（Core Audio 的回环采集）
  抓"默认输出设备正在播放的声音"（OBS / 各会议转录软件在 Windows 录"扬声器输出"同款官方接口）。
  不需要虚拟声卡（BlackHole/VB-Cable）、不需要 ffmpeg、不装任何第三方录音软件、不切换输出设备、
  不干扰正常听声。**唯一前提**：系统有一个"已启用"的默认音频输出设备（扬声器/耳机/USB 声卡）。
- **无浏览器自动化**：打开直播页由用户手动完成，本技能从不驱动浏览器。
- **转写引擎 sherpa-onnx 本地离线**（中文 zipformer-ctc int8 模型，约 370MB，缓存于
  `%USERPROFILE%\.cache\live-class-skill\models`），不联网、不花钱、速度快。与 mac 版共用同一模型。
- **转录是增量的**：录音与转写并行，随时可看已转内容，中断后可续。

## 使用流程

### 第 1 步：收集信息
确认以下信息（用户已给齐则跳过）：
- 直播/课程 **URL**（或平台 + 主播/房间）；
- **结束信号**怎么给：默认用户说「结束/好了」；也可约定时长/由 AI 观察；
- 输出目录：默认建在当前工作区 `直播代听/<名称>-<日期>-<开始时间>/`。

### 第 2 步：环境检查（每次开始前必跑）
```bat
python <skill>\win\scripts\setup.py
```
- 三段都 `[OK]` → 继续。有 `[缺失]` → 按指引修复，或 `python <skill>\win\scripts\setup.py --fix`
  （自动建 `.venv` 装 sherpa-onnx、下载模型，需联网几分钟）。
- 常见第一段 `[缺失]`：本机没有"启用的默认音频输出设备"——接好扬声器/耳机/USB 声卡即可；
  只要系统能正常出声，内录就绪。

### 第 3 步：准备会话目录
```bat
set SESSION=<输出目录>\<名称>-%date:~0,4%%date:~5,2%%date:~8,2%-%time:~0,2%%time:~3,2%
mkdir %SESSION%\chunks
```
会话目录约定（与 mac 版一致）：
```
<session>\
├── meta.json            # 捕获元信息（engine=wasapi_loopback/开始时间/分段秒数）
├── record.pid           # 内录进程 pid
├── chunks\seg_*.wav     # 约 20 秒一个的分块（16kHz 单声道 16bit；有声才落盘）
├── transcripts\
│   ├── segments.jsonl   # 每块一行 {file,text,audio_seconds,...}
│   └── transcript.txt   # 纯文本逐字稿（随转写实时更新）
├── stop.flag            # 收尾信号（record.py stop 写入，内录进程见到即优雅退出）
└── summary.md           # 结束时生成的总结
```

### 第 4 步：启动内录与转写（两个常驻进程）
```bat
set PY=<setup.py 探测到的含 sherpa 的 python，默认 win\scripts\.venv\Scripts\python.exe>
:: 1) 内录（WASAPI loopback）
start /b "" "%PY%" <skill>\win\scripts\record.py start --session %SESSION% > %SESSION%\record.log 2>&1
:: 2) 转写（复用根目录通用转写器）
start /b "" "%PY%" <skill>\scripts\transcribe.py --session %SESSION% --watch > %SESSION%\transcribe.log 2>&1
```
启动 2 秒后确认两个日志无报错：
- `type %SESSION%\record.log` 出现「开始内录系统声音」；
- `type %SESSION%\transcribe.log` 无异常。

### 第 5 步：用户打开直播并确认「有声音进内录」
1. **请用户自己在浏览器（Edge/Chrome 等）手动打开直播 URL**；需登录则用户扫码/登录（登录态浏览器侧保留）。
   本技能**不自动打开、不驱动浏览器**。
2. **打开后，务必主动向用户发一次固定提醒**（浏览器首次打开常拦截自动播放/默认静音，是无声最常见原因）：
   > 请确认直播间**正在播放、且能听到声音**。若没声音，请依次检查：
   > ① **地址栏喇叭图标**不是「已静音」；② 页面/播放器**音量**不是 0；
   > ③ **系统**右下角音量不是 0/静音；④ 页面确实**在播放**（有画面/时间在走）。
   > 只要其中一处能听到声即可，本技能只录「系统正在播放的声音」。
3. 等约 25 秒让第一个分块落地，检查是否转出文字：
   ```bat
   type %SESSION%\transcripts\segments.jsonl
   ```
   - 有文字 → 链路通了，向用户汇报「已开始代听」，进入监控。
   - 为空/(静音) → 再提示用户按上面 ①–④ 排查一遍（通常就是标签页静音或没点播放）；
     确认出声后过 10 秒再看一次。仍无则排查：⑤ 平台是否要登录；⑥ 声音是否输出到了别的设备。

### 第 6 步：监听进展（低频轮询）
每 1–2 分钟对比一次 `segments.jsonl` 行数增量：
- 持续增长 → 正常，不打扰用户；
- **停止增长 ≥ 3 分钟** → 询问用户「收音停了，是课间/结束/卡住了吗？」再处理；
- 用户中途嘱托「帮我记一下 XX」→ 记下，收尾总结时单独成节回应，不打断内录。

### 第 7 步：收尾（结束信号到达）
触发条件：用户说「结束/好了」、约定时长到、或确认直播下播。
```bat
python <skill>\win\scripts\record.py stop --session %SESSION%   :: 1. 通知内录进程优雅落盘退出
:: 2. transcribe 见到 stop.flag/进程结束，处理完残余后自动退出（至多等 60 秒）
```
无需恢复任何输出设备（本方案从不改动系统输出）。

### 第 8 步：生成总结（核心增值步骤）
1. 读 `transcript.txt` 全文（长文可分段读）；
2. 明显重复段落（回声/重播导致）可在总结里忽略，但**不改写逐字稿原文件**；
3. 生成 `summary.md`，按课程/会议性质取舍结构：
```markdown
# <课程/会议名> — 直播代听笔记（<日期>）
## 一句话摘要
## 内容脉络（分节标题，标注大致时间）
## 关键要点（编号列出，可附原文短句）
## 行动项（要做的事 / 待准备 / 下节预告）
## 数据与金句（如有）
## 存疑/没听懂（便于用户回看对应时段）
```
4. 用户中途嘱托的「帮我记一下 XX」在此单独成节回应。

### 第 9 步：交付
- 用结果展示把 `summary.md` 与 `transcript.txt` 呈现给用户；
- 口头汇报 3–5 句：讲了什么、几个关键点、文件存放位置；
- 询问是否需要导出其他格式（用户要才做，不主动）。

## 故障速查

| 症状 | 原因与处理 |
|---|---|
| setup 第一段 `[缺失]` 内录不可用 | 无"启用的默认输出设备"：接好扬声器/耳机/USB 声卡，确保系统能出声后重跑 setup.py |
| record start 报「初始化失败 0x88890008」 | 默认输出设备被禁用/拔出：设备 > 音频 启用默认输出，或插上耳机再试 |
| record 启动成功但全是静音/无文字 | 系统音量 0、直播没在播、标签页静音：请用户调高音量/确认播放 |
| 转写全为空 | 直播页静音/没点播放/需登录：请用户确认页面在播放后再看日志 |
| 声音忽断 | 网络卡顿属正常，恢复自动续录；持续静音按第 6 步处理 |
| 磁盘占用 | 20s×16k 单声道约 115MB/小时，可接受 |
| 静音段无分块 | 正常设计：有声才落盘，转写只处理实际内容 |

## 已知边界
- 只录系统正在播放的声音（直播内容），不录麦克风；
- 受 DRM 保护的媒体（个别平台版权内容）可能静音，属系统限制；
- 内录期间系统通知音会混入，开始前提醒用户静音无关应用；
- 浏览器需处于"出声播放"状态才有数据；不驱动浏览器、不读取页面；
- 模型路径可用 `LCC_MODELS_DIR` 覆盖；转写引擎路径逻辑见根 `scripts/common.py`。

## 资源说明
- `win/scripts/_wasapi.py`：WASAPI loopback 内录引擎（纯 ctypes，零第三方）；
- `win/scripts/record.py`：内录启停入口（`check` / `start` / `stop`）；
- `win/scripts/setup.py`：Windows 环境检查/一键就绪（`--fix` 建 venv 装 sherpa、下载模型）；
- 根 `scripts/transcribe.py`：增量转写器（sherpa-onnx，`--watch` 轮询），mac/win 共用；
- 根 `scripts/common.py`：模型/引擎定位（mac/win 共用）；
- `references/windows-audio-setup.md`：内录原理与零第三方说明、常见问题。
