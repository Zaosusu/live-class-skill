# AGENTS.md — 给所有 AI Agent 的硬约束

本文件约束任何以 Agent 身份操作本仓库（`live-class-skill`）的 AI（Claude Code / Codex /
WorkBuddy / 其他 Coding Agent）。**请完整阅读并严格遵守**，违反视为事故。

## 1. 反浏览器自动化铁律（最高优先级，不可协商）

本项目是"直播课/会议**代听**"技能，只负责**监听系统正在播放的声音**并离线转写。
**严禁任何浏览器自动化 / 网页自动化 / 用户界面自动化**，包括但不限于：

- ❌ 驱动浏览器打开页面（selenium / playwright / puppeteer / webdriver / CDP / DevTools Protocol
  / 浏览器扩展注入 / 隐形浏览器 / headless 抓取）。
- ❌ 自动点击、填写、滚动、切换标签、刷新页面、模拟按键/输入。
- ❌ 读取/解析页面 DOM、抓取直播流地址、解析平台私有接口、逆向签名。
- ❌ 用任何方式"代用户操作"直播平台网页。

**原因**：自动听课/录屏脚本在直播平台眼里等同"机器人代看"，极容易触发风控封号；
本项目以"用户手动打开 + 系统级声音监听"为设计基石，对平台零介入，最安全。

**正确的做法（必须遵守）**：
- ✅ 打开直播网页 → 由**用户自己**在浏览器里完成；Agent 最多**指导/提示**用户去打开某 URL，
  并请用户确认"页面在播放、音量非 0"。
- ✅ **打开后主动发一次无声排查提醒**（浏览器首次打开常拦截自动播放/默认静音，是"没声音"最常见
  原因）：请用户依次确认 ①地址栏喇叭未静音 ②页面/播放器音量非 0 ③系统音量非 0 ④页面真在播放。
  只要用户能听到声、内录就能抓到；等首块转出文字后再宣告"已开始代听"。
- ✅ Agent 负责的部分只有：启动/管理"系统声音内录"进程、跑本地离线转写、监控增量、
  结束后生成总结。全程不碰浏览器进程。

> 触发词自查：若本仓库任何脚本/流程/文档中出现
> `selenium` `playwright` `puppeteer` `webdriver` `cdp` `headless` `自动打开` `模拟点击`
> `注入脚本` `读取页面` 等，应视为违规，需移除/改正后再继续。

## 2. 平台双版本结构

本仓库一个 skill 分两版，**通用层放仓库根，平台差异分目录**：

```
live-class-skill/
├── SKILL.md                 # mac 版主流程（历史保留，见 mac 说明）
├── win/SKILL_win.md         # Windows 版主流程（本新增）
├── mac/ 相关说明            # macOS 平台实现与 references
├── win/                     # Windows 平台实现
│   ├── scripts/record.py    # WASAPI loopback 内录（零第三方）
│   ├── scripts/_wasapi.py   # 内录引擎（纯 ctypes，系统 API）
│   ├── scripts/setup.py     # Windows 环境检查
│   └── references/
├── scripts/                 # 通用层（两版共用）
│   ├── common.py            # 模型/引擎定位
│   ├── cuda_rt.py           # CUDA 运行库只读探测/注入（GPU 加速）
│   ├── accel.py             # 加速档位决策（GPU优先/CPU兜底）
│   ├── transcribe.py        # 转写（sherpa-onnx，GPU优先/CPU兜底）
│   ├── listen.py            # 持续听课编排（抓主题建目录/近流式/自动收尾）
│   └── ...                  # (mac 遗留脚本，勿删)
├── user/                    # 听课产出根（本地产出，不入库），见 user/README.md
└── AGENTS.md
```

## 3. 通用 ASR/模型资产归属

- **sherpa-onnx 引擎 + 中文离线模型**属于**通用资产**，由仓库根 `scripts/` 的
  `common.py`（模型定位）与 `transcribe.py`（转写）统一管理，mac/win 共用。
- 改模型/换引擎**只改通用层一处**，两个平台都生效，不要在各平台目录重复维护一套。

## 4. 转写加速档位：GPU优先 / CPU兜底 / 可选 API

- 转写引擎自动决策在**通用层**统一实现，`accel.py` + `cuda_rt.py` 负责探测与决策，mac/win 共用。
  - `scripts/cuda_rt.py`：只读探测本机是否有**可复用的 CUDA 运行库**（系统 Toolkit 或 PyTorch 自带），
    并提供运行时注入（跨平台安全，mac/linux 一律返回 None、零副作用）。
  - `scripts/accel.py`：档位决策（`gpu` / `cpu`）+ 打印给用户看的说明。
  - `scripts/transcribe.py`：按档位选 provider（GPU 优先、自动降级 CPU），GPU 档会做启动预热。
- **决策铁律（给 Agent 与 setup）**：
  1. 能上 GPU 就上 GPU，**不需要问用户**；
  2. 无 NVIDIA GPU / 有 GPU 但缺运行库 → **默认自动降级 CPU**（任何机器开箱即用、绝不卡住）；
  3. 是否向用户"确认降级"取决于用户是否在意：**用户不懂/不关心 → 自动用 CPU，别再追问**；
  4. 可顺带告知"可选配第三方 ASR API"，但**用户不配/不懂 → 继续用默认档位**，绝不强制。
- 改这里务必保持 CPU 兜底路径可用——它是"开箱即用"的最后防线。

## 5. 持续听课：统一编排 listen.py 与产出落盘规范

- **推荐一条命令听完一整场**，别再用"手动起 record + 起 transcribe + 盯结束"的散装流程：
  ```bash
  python scripts/listen.py --room <B站直播间号> --user <使用者> [--wait-start]
  ```
  自动完成：抓直播主题建目录 → 内录(默认 5s 近流式小分块) → 秒级增量转写出字 →
  周期检测下播 → 自动停录收尾 → 产出最终 `transcript.txt`。可 nohup 无人值守。
- **产出落盘规范（单一可信源）**：一律落 `user/session/<主播账号>_<直播主题>_<开始时间>/`，
  主播账号+主题+时间全拼在目录名里（主播名/主题均自动抓取，取不到降级 room<id>；
  `--user` 听课人仅记入 meta.json 不进目录名）。目录下含 `chunks/`(音频)、
  `transcripts/transcript.txt`(实时追加的最终文字稿)、`segments.jsonl`、
  `meta.json`、各日志。这些是**本地产出，不入版本库**(已在 .gitignore 排除)，见 user/README.md。
- 手动起零件(record/transcribe)仅用于调试；正式"持续听课"走 listen.py。

## 6. 修改守则

- 不要破坏 mac 版现有脚本的路径引用；两版共用文件只在根 `scripts/` 维护。
- Windows 版内录必须走系统 API（WASAPI loopback），**不得引入**虚拟声卡、ffmpeg、
  第三方录音软件作为运行时依赖；录音脚本本身也无需第三方 Python 包（纯 ctypes）。
- 新增平台差异代码放对应 `mac/` 或 `win/`；新增通用能力放仓库根。
