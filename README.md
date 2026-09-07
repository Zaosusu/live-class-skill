# 听课 Skill（live-class-skill）

社媒直播课 / 线上课程「代听」助手 —— **打开直播网页出声 → 内录系统声音 → 本地实时转写出字 → 结束后生成结构化笔记**。
适用于 **B站 / 抖音 / 小红书 / 视频号 / 快手** 等社媒平台的直播课与线上课程，以及千聊 / 小鹅通 / 知识星球等知识付费直播课。

> **定位边界（先读）**：本工具面向**没有回放、也没有自带总结**的社媒直播课 / 线上课程
> （直播课往往和上班、生活时间冲突，错过就没了）。**会议类不是本工具的目标**——腾讯会议、
> 飞书会议、Zoom 等已内置 AI 纪要总结，直接用其自带纪要即可；仅当某场会议确无 AI 纪要
> 又必须代听时，本工具技术上同样能录（见 [`references/platforms.md`](references/platforms.md)），
> 但那不是主场景，README 与技能手册均按直播课定位撰写。

> **平台**：本 skill 一份仓库分两版运行——
> - **macOS 版**：见 [`SKILL.md`](SKILL.md)（ScreenCaptureKit 收音）。
> - **Windows 版**：见 [`win/SKILL_win.md`](win/SKILL_win.md)（WASAPI loopback 内录，零第三方）。
> 两版共用同一离线转写引擎与模型（根 `scripts/`）。
> **反浏览器自动化铁律**（防封号）见 [`AGENTS.md`](AGENTS.md)：本 skill 只监听系统声音，
> 打开直播网页由用户自己完成，所有 agent 一律不得驱动/注入/读取浏览器。

## ✨ 亮点

- **零安装、零第三方内录**：不装虚拟声卡（无需 BlackHole / Soundflower / VB-Cable）、不装 ffmpeg、不切换系统输出设备。
  - macOS：系统自带 **ScreenCaptureKit**（OBS 同款），只需一次性「屏幕录制」授权；
  - Windows：系统自带 **WASAPI loopback**（纯 ctypes 调系统 Core Audio），无需授权、无需装任何软件。
- **100% 本地转写**：sherpa-onnx + 中文 zipformer-ctc 离线模型，不上传任何音频，不花钱，速度远超实时
  （实测 10 秒音频约 0.15 秒识别完成）。mac/win 共用同一模型缓存。
- **增量转录**：边录边转，随时可看已转内容；中断可续跑，不丢进度。
- **边听边录不冲突**：不改变系统输出，你可以照常戴耳机/外放做别的事，互不干扰。
- **零浏览器自动化**：不驱动浏览器，打开直播页由用户手动完成（防平台封号）。

## 原理

```
浏览器/App 播放直播声音 ──▶ 系统混音器
                                 │ macOS ScreenCaptureKit  /  Windows WASAPI loopback
                                 ▼
                内录工具 ──▶ chunks/seg_*.wav（近流式小分块默认 5s，16kHz 单声道）
                                 │
                transcribe.py ──▶ segments.jsonl + transcript.txt（本地 ASR 增量转写）
                                 ▼
                          主代理/大模型 ──▶ summary.md 结构化总结
```

## 环境要求

| 依赖 | 说明 | 获取方式 |
|---|---|---|
| macOS 14+ | ScreenCaptureKit 音频捕获 | 系统自带 |
| Xcode Command Line Tools | 编译 capture 工具（swiftc） | `xcode-select --install` |
| Python 3.9+ | 转写引擎 | `setup.sh --fix` 会自动建 venv 并安装 |
| 屏幕录制权限 | 一次性隐私授权 | 系统设置 → 隐私与安全性 → 屏幕录制 |
| 中文 ASR 模型 | sherpa zipformer-ctc int8，约 370MB | `setup.sh --fix` 自动下载到 `~/.cache/live-class-skill/models/` |

> 说明：转写模型与 Python 包是**首次使用的一次性下载/安装**，均为开源免费组件；之后完全离线。

> **加速档位（Windows）**：转写引擎自动 **GPU优先 / CPU兜底**——检测到 NVIDIA GPU 且系统里有可复用的
> CUDA 运行库（系统 CUDA Toolkit 或任意 PyTorch 自带库）就用 GPU，否则自动用 CPU，任何机器都能跑、不用手动配。
> macOS 默认走 CPU。

## 安装

```bash
git clone https://github.com/Zaosusu/live-class-skill.git
cd live-class-skill

# 一键：建 Python venv + 装 sherpa-onnx + 下载中文模型 + 编译 capture 工具
bash scripts/setup.sh --fix

# 检查全部就绪（含屏幕录制权限探测）
bash scripts/setup.sh
```

首次使用需在 **系统设置 → 隐私与安全性 → 屏幕录制** 勾选承载本工具的终端/IDE
（不在列表就点 ＋ 手动添加），**完全退出并重启**该终端后生效。
授权细节见 [`references/audio-setup.md`](references/audio-setup.md)。

## 用法

### 持续听课一条命令（推荐）

```bash
# 给任意直播链接即可——能抓(B站等)自动按 主播_主题_时间 建目录，
# 抓不到(抖音/小红书/视频号等)自动降级为纯时间目录，照常内录：
python scripts/listen.py --url <任意直播URL> --user <你的标识>

# B站数字简写：
python scripts/listen.py --room <B站房间号> --user <你的标识>

# 没人值守跑一整场（未开播会等、下播自动收尾），后台：
nohup python scripts/listen.py --url https://live.bilibili.com/1816490612 \
      --user me --wait-start > user/listen_me.log 2>&1 &
# 盲录平台(抖音/小红书等)：连续静音默认 3 分钟自动收尾；可叠加 --max-minutes 兜底
nohup python scripts/listen.py --url https://live.douyin.com/xxx --user me \
      --silent-timeout 3 --max-minutes 120 > user/listen_dy.log 2>&1 &
```

### 命令行（不含总结）

```bash
PY=scripts/.venv/bin/python        # 或任意装有 sherpa_onnx 的 python
SESSION=$PWD/demo-$(date +%H%M)     # 会话目录
mkdir -p "$SESSION"

# 终端 1：开始收音（ScreenCaptureKit）
nohup "$PY" scripts/record.py start --session "$SESSION" > "$SESSION/record.log" 2>&1 &

# 终端 2：增量转写（自动轮询新分块）
nohup "$PY" scripts/transcribe.py --session "$SESSION" --watch > "$SESSION/transcribe.log" 2>&1 &

# ……现在打开直播网页正常播放即可……

# 结束后：
touch "$SESSION/done.flag"          # 通知转写器收尾
"$PY" scripts/record.py stop --session "$SESSION"

# 成果：
cat "$SESSION/transcripts/transcript.txt"    # 逐字稿
```

### 作为 AI Agent 技能（推荐）

本仓库同时是一个 **Agent Skill 包**（SKILL.md 规范，Claude Code / Cursor / Codex /
WorkBuddy 等支持 Agent Skills 的终端助手均适用）：
把整个目录放进你所用 Agent 的技能目录（如 `~/.claude/skills/`、`~/.cursor/skills/`，
或按你的工具约定放置），然后对 Agent 说：

> 「帮我把这场 B站直播课录下来转成文字，结束后给我一份总结」

Agent 会按 [`SKILL.md`](SKILL.md) 的 10 步流程自动完成：开直播页 → 收音 → 增量转写 →
结束后产出 `summary.md`（一句话摘要 / 内容脉络 / 关键要点 / 行动项 / 金句 / 存疑清单）。

## 目录结构

```
├── SKILL.md                # Agent 技能手册（10 步操作流程 + 故障速查）
├── references/
│   ├── audio-setup.md      # 屏幕录制权限授权与常见问题
│   └── platforms.md        # 各直播平台注意事项与结束判定
└── scripts/
    ├── capture.swift       # 系统声音捕获（ScreenCaptureKit，编译产物 bin/capture）
    ├── record.py           # 捕获启停入口（check / start / stop）
    ├── transcribe.py       # 增量本地转写（sherpa-onnx，--watch 轮询）
    ├── listen.py           # 持续听课编排（推荐入口：近流式实时出字 + 自动收尾）
    ├── common.py           # 环境探测（可移植，支持 LCC_PYTHON / LCC_MODELS_DIR 覆盖）
    └── setup.sh            # 一键环境检查/修复
```

## 已知边界

- 只录系统正在播放的声音（直播内容），不录麦克风；
- 受 DRM 保护的媒体（个别平台版权内容）可能被 macOS 静音，属系统限制；
- 录音期间系统通知音会混入，开始前建议静音无关应用；
- 静音时段不产生分块（有声才落盘），属正常设计。

## License

[MIT](LICENSE)
