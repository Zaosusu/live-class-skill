---
name: live-class-skill
description: "直播课/线上课程/会议「代听」助手（macOS 版，Windows 版见 win/SKILL_win.md）：指导用户自行打开直播网址出声后，持续录制系统声音（mac 用系统自带 ScreenCaptureKit）→本地转写成文字→结束后生成结构化总结笔记。⚠️ 绝不使用任何浏览器自动化（防封号）。当用户说「帮我听/代听这场直播」「把 B站/抖音/小红书直播课录下来转成文字」「没时间上课，结束后给我一份笔记/总结」「替我去听会，结束时给我要点」，或给出一个直播/课程/会议 URL 要求收音转写并总结时使用。适用于任何能在浏览器出声的线上直播课（B站/抖音/小红书/视频号/腾讯会议等）。"
agent_created: true
---

# live-class-skill — 直播课/会议「代听」助手

用户没时间一直盯直播，由本技能完成：**打开直播页面 → 持续收音 → 本地转写 → 结束后总结**。
产出两份文件：① 逐字稿 `transcript.txt`；② 结构化总结 `summary.md`。

> **运行平台选择**：
> - **macOS** → 本文即完整流程（ScreenCaptureKit 收音）。
> - **Windows** → 改用 **[`win/SKILL_win.md`](win/SKILL_win.md)**（WASAPI loopback 内录）。
> 两版共用同一离线转写引擎与模型（仓库根 `scripts/`）。
>
> ⚠️ **反浏览器自动化铁律**：本技能只监听系统声音，**绝不驱动/注入/读取浏览器**。
> 打开直播网页由用户自己完成。所有 agent 须遵守仓库根 [`AGENTS.md`](AGENTS.md)。

## 核心原理

```
浏览器/App 播放直播声音 ──▶ macOS 系统混音器
                                   │ ScreenCaptureKit（macOS 14+ 系统自带，无需任何安装）
                                   ▼
              capture 工具（scripts/bin/capture）分块录音(20s/块, 16kHz 单声道)
                                   │
              transcribe.py 增量转写（sherpa-onnx 本地离线）
                                   ▼
        transcripts/transcript.txt → 主代理生成总结 summary.md
```

关键事实（决定整个流程怎么做）：
- **收音零安装**：直接用 macOS 系统自带的 **ScreenCaptureKit** 捕获"正在播放的系统声音"
  （OBS / 会议转录软件同款官方接口），**不需要虚拟声卡（BlackHole）、不需要 ffmpeg、
  不需要切换系统输出设备、不干扰正常听声**。唯一前置：一次性授予
  **「屏幕录制」隐私权限**（系统设置→隐私与安全性→屏幕录制，勾选运行本工具的 App）。
- **转写引擎 sherpa-onnx 本地离线**（中文 zipformer-ctc int8 模型，约 370MB，
  缓存于 `~/.cache/live-class-skill/models/`），不联网、不花钱、速度快（远超实时）。
- **转录是增量的**：录音与转写并行，随时可看已转内容，中断后可续。

## 使用流程

### 第 1 步：收集信息
确认以下信息（用户已给齐则跳过）：
- 直播/课程 **URL**（或平台 + 主播/房间）
- **结束信号**怎么给：默认用户说「结束/好了」；也可约定时长/由 AI 观察
- 输出目录：默认建在当前工作区 `直播代听/<名称>-<日期>-<开始时间>/`

### 第 2 步：环境检查（每次开始前必跑）
```bash
bash <技能目录>/scripts/setup.sh
```
- 全部 `[OK]` → 继续。有 `[缺失]` → 按指引修复；
  **capture 未编译**：`bash setup.sh --fix`（需 swiftc，Xcode CLT 已装则直接成功）。
- **「屏幕录制」权限未授予**（首次使用必经）：
  1. 打开 系统设置 → 隐私与安全性 → **屏幕录制**；
  2. 勾选运行本工具的 App（终端 / IDE，即从哪启动本技能就勾哪个）；
  3. **完全退出并重启该 App**（权限变更需重启生效）后，让用户回复「好了」，
     再重跑 setup.sh 确认权限 OK。
- 也可直跑 `python3 <技能目录>/scripts/common.py` 看探测结果。

### 第 3 步：准备会话目录
```bash
SESSION="<输出目录>/<名称>-$(date +%Y%m%d-%H%M)"
mkdir -p "$SESSION/chunks" "$SESSION/transcripts"
```
会话目录约定：
```
<session>/
├── meta.json            # 捕获元信息（引擎/开始时间）
├── record.pid           # 捕获进程 pid
├── chunks/seg_*.wav     # 约 20 秒一个的音频分块（有声音才落盘）
├── transcripts/
│   ├── segments.jsonl   # 每块一行 {file, text, audio_seconds,...}
│   └── transcript.txt   # 纯文本逐字稿（随转写实时更新）
├── done.flag            # 收尾信号（touch 后转写器处理完残余自动退出）
└── summary.md           # 结束时生成的总结
```

### 第 4 步：确认收音权限就绪
上一步已 `[OK]` 则跳过。若未做过权限检查，先跑：
```bash
python3 <技能目录>/scripts/record.py check
```
- 返回「权限正常」→ 继续；
- 否则按提示引导用户完成「屏幕录制」授权并重启 App（同第 2 步说明）。

### 第 5 步：启动录音与转写（两个常驻进程）
```bash
PY="<setup.sh 探测到的 python 路径（默认 scripts/.venv/bin/python）>"
cd "<技能目录>/scripts"
nohup "$PY" record.py start --session "$SESSION" > "$SESSION/record.log" 2>&1 &
nohup "$PY" transcribe.py --session "$SESSION" --watch > "$SESSION/transcribe.log" 2>&1 &
```
启动 2 秒后确认两个日志无报错：
- `cat "$SESSION/record.log"` 出现「开始捕获系统声音」；
- `tail "$SESSION/transcribe.log"` 无异常。

### 第 6 步：打开直播并确认「有声音进录音」
1. **指导用户自己在浏览器手动打开直播 URL**（本技能不驱动浏览器，防封号，见铁律）；
   需登录则引导用户扫码/登录（登录态浏览器侧保留）；
2. **打开后，主动向用户发一次固定提醒**（浏览器首次打开常拦截自动播放/默认静音）：
   > 请确认直播**在播放且能听到声音**。没声音就依次检查：① 地址栏喇叭图标未静音；
   > ② 页面/播放器音量非 0；③ 系统音量非 0；④ 页面确实在播放。
   > 只要你能听到声即可，本技能只录「系统正在播放的声音」。
3. 等约 25 秒让第一个分块落地，检查是否转出文字：
   ```bash
   tail -1 "$SESSION/transcripts/segments.jsonl"
   ```
   - 有文字 → 链路通了，向用户汇报「已开始代听」，进入监控。
   - 为空/(静音) → 再提示用户按上面 ①–④ 排查（常见是标签页静音或没点播放）；仍无则查
     ⑤ 平台是否要登录/权限；修复后重试。

### 第 7 步：监听进展（低频轮询）
每 1–2 分钟对比一次 `wc -l "$SESSION/transcripts/segments.jsonl"` 增量：
- 持续增长 → 正常，不打扰用户；
- **停止增长 ≥ 3 分钟** → 询问用户「收音停了，是课间/结束/卡住了吗？」再处理；
- 用户中途嘱托「帮我记一下 XX」→ 记下，收尾总结时单独成节回应，不打断录音。

### 第 8 步：收尾（结束信号到达）
触发条件：用户说「结束/好了」、约定时长到、或确认直播下播。
```bash
touch "$SESSION/done.flag"                       # 1. 通知转写器收尾
# 2. 等 transcribe 进程退出（会处理完残余分块），最多等 60 秒
"$PY" record.py stop --session "$SESSION"        # 3. 停止捕获（自动落盘尾块）
```
无需恢复输出设备（本方案从不改动系统输出）。

### 第 9 步：生成总结（核心增值步骤）
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

### 第 10 步：交付
- 用结果展示把 `summary.md` 与 `transcript.txt` 呈现给用户；
- 口头汇报 3–5 句：讲了什么、几个关键点、文件存放位置；
- 询问是否需要导出飞书文档/其他格式（用户要才做，不主动）。

## 故障速查

| 症状 | 原因与处理 |
|---|---|
| capture 报「屏幕录制」权限未授予 | 系统设置→隐私与安全性→屏幕录制 勾选宿主 App 并重启，见第 2 步 |
| 全是静音/无文字 | 系统音量 0、直播没在播、标签页静音：调高音量或确认播放（可截图） |
| 转写全为空 | 直播页静音/没点播放/需登录：截屏确认并处理 |
| 声音忽断 | 网络卡顿属正常，恢复自动续录；持续静音按第 7 步处理 |
| 磁盘占用 | 20s×16k 单声道约 115MB/小时，可接受 |
| 静音段无分块 | 正常设计：有声才落盘，转写只处理实际内容 |

## 已知边界
- 只录系统正在播放的声音（直播内容），不录用户麦克风；
- 受 DRM 保护的媒体（个别平台版权内容）可能被 macOS 静音，属系统限制；
- 录音期间系统通知音会混入，开始前提醒用户静音无关应用；
- 平台登录态在浏览器侧；模型路径可用 `LCC_MODELS_DIR` 覆盖（见 common.py 顶部）。

## 资源说明
- `scripts/capture.swift`：系统声音捕获工具（ScreenCaptureKit，编译产物 `scripts/bin/capture`）；
- `scripts/record.py`：捕获启停入口（`check` / `start` / `stop`）；
- `scripts/transcribe.py`：增量转写器（sherpa-onnx，`--watch` 轮询模式）；
- `scripts/common.py`：环境探测（python/capture/模型），其他脚本共用；
- `scripts/setup.sh`：一键环境检查/修复（`--fix` 自动编译 capture、下载模型）；
- `references/audio-setup.md`：「屏幕录制」权限授权与常见问题（零安装说明）；
- `references/platforms.md`：各平台注意事项与结束判定。
