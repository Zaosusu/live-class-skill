#!/bin/bash
# setup.sh — 一键检查 live-class-skill 运行环境，缺什么报什么、怎么修。
# 零安装方案：不需要虚拟声卡/不需要 ffmpeg/不需要切换输出设备，
# 系统声音直接由系统自带 ScreenCaptureKit 捕获（macOS 14+，需一次性"屏幕录制"授权）。
#
# 用法: bash setup.sh            # 只检查并打印指引（推荐先跑这个）
#       bash setup.sh --fix      # 尽力自动修复可修复项（建 venv、编译 capture、下载模型）
set -u
cd "$(dirname "$0")"

HOME_DIR="$HOME"
SKILL_DIR="$(cd .. && pwd)"
MODELS_DIR="${LCC_MODELS_DIR:-$HOME_DIR/.cache/live-class-skill/models}"
MODEL_SUBDIR="sherpa-onnx-zipformer-ctc-zh-int8-2025-07-03"
MODEL_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/${MODEL_SUBDIR}.tar.bz2"
VENV_DIR="$SKILL_DIR/scripts/.venv"
VENV_PY="$VENV_DIR/bin/python"
CAPTURE_BIN="$SKILL_DIR/scripts/bin/capture"
FIX=0
[ "${1:-}" = "--fix" ] && FIX=1

ok()   { printf "  \033[32m[OK]\033[0m   %s\n" "$1"; }
miss() { printf "  \033[31m[缺失]\033[0m %s\n" "$1"; }
info() { printf "  \033[36m[指引]\033[0m %s\n" "$1"; }
step() { printf "\n\033[1m== %s ==\033[0m\n" "$1"; }

step "1/4 转写引擎 sherpa-onnx（Python，纯本地 ASR）"
PY=""
if [ -x "$VENV_PY" ] && "$VENV_PY" -c "import sherpa_onnx" >/dev/null 2>&1; then
  PY="$VENV_PY"
else
  # 回退：系统 python3 里若有 sherpa_onnx 也可用
  SYS_PY="$(command -v python3 2>/dev/null)"
  if [ -n "$SYS_PY" ] && "$SYS_PY" -c "import sherpa_onnx" >/dev/null 2>&1; then
    PY="$SYS_PY"
  fi
fi
if [ -n "$PY" ]; then ok "python+sherpa-onnx: $PY"
else
  miss "没有可用的 sherpa-onnx 环境"
  if [ "$FIX" = "1" ]; then
    SYS_PY="$(command -v python3 2>/dev/null)"
    if [ -z "$SYS_PY" ]; then
      info "系统无 python3，请先安装：brew install python 或官网下载"
    else
      info "正在创建虚拟环境 $VENV_DIR 并安装 sherpa-onnx ..."
      "$SYS_PY" -m venv "$VENV_DIR" \
        && "$VENV_PY" -m pip install --quiet --upgrade pip \
        && "$VENV_PY" -m pip install --quiet sherpa-onnx soundfile \
        && ok "sherpa-onnx 安装完成"
    fi
  else
    info "修复：bash setup.sh --fix（将创建 scripts/.venv 并安装 sherpa-onnx soundfile）"
    info "手动方式：python3 -m venv scripts/.venv && scripts/.venv/bin/pip install sherpa-onnx soundfile"
  fi
fi

step "2/4 ASR 模型（中文 zipformer-ctc int8，约 370MB）"
if [ -f "$MODELS_DIR/$MODEL_SUBDIR/model.int8.onnx" ] && [ -f "$MODELS_DIR/$MODEL_SUBDIR/tokens.txt" ]; then
  ok "模型已就绪: $MODELS_DIR/$MODEL_SUBDIR"
else
  miss "模型未下载"
  if [ "$FIX" = "1" ]; then
    info "正在下载 $MODEL_URL ..."
    mkdir -p "$MODELS_DIR" && cd "$MODELS_DIR" \
      && curl -sL -o "${MODEL_SUBDIR}.tar.bz2" "$MODEL_URL" \
      && tar xjf "${MODEL_SUBDIR}.tar.bz2" \
      && rm -f "${MODEL_SUBDIR}.tar.bz2" \
      && ok "模型下载并解压完成"
  else
    info "修复（需几分钟，300+MB）："
    info "  mkdir -p $MODELS_DIR && cd $MODELS_DIR"
    info "  curl -sL -o ${MODEL_SUBDIR}.tar.bz2 \"$MODEL_URL\" && tar xjf ${MODEL_SUBDIR}.tar.bz2"
  fi
fi

step "3/4 系统声音捕获工具 capture（ScreenCaptureKit，macOS 14+）"
if [ -x "$CAPTURE_BIN" ]; then
  ok "capture 已编译: $CAPTURE_BIN"
  # 权限探测
  "$CAPTURE_BIN" --probe >/dev/null 2>&1
  if [ $? -eq 0 ]; then
    ok "「屏幕录制」权限已授予 —— 可以开始录音了"
  else
    info "「屏幕录制」权限未授予（捕获系统声音必需）。"
    info "请打开 系统设置 → 隐私与安全性 → 屏幕录制，勾选运行本工具的 App"
    info "（终端 / IDE），完全退出并重启该 App 后重新运行 setup.sh 确认。"
    info "（若 App 不在列表，点 ＋ 从「应用程序」手动添加）"
  fi
else
  miss "capture 未编译"
  if command -v swiftc >/dev/null 2>&1; then
    if [ "$FIX" = "1" ]; then
      swiftc -O -parse-as-library "$SKILL_DIR/scripts/capture.swift" \
        -o "$CAPTURE_BIN" && ok "capture 编译完成"
    else
      info "修复：swiftc -O -parse-as-library scripts/capture.swift -o scripts/bin/capture"
      info "（需 Xcode CommandLine Tools：xcode-select --install）"
    fi
  else
    miss "swiftc 不可用，无法编译捕获工具"
    info "安装 Xcode CommandLine Tools：xcode-select --install"
  fi
fi

echo
step "4/4 转写加速档位判定（GPU优先 / CPU兜底，自动决策）"
RUN_PY="${PY:-$(command -v python3 2>/dev/null)}"
if [ -n "$RUN_PY" ] && [ -f "$SKILL_DIR/scripts/accel.py" ]; then
  "$RUN_PY" "$SKILL_DIR/scripts/accel.py" 2>/dev/null | sed 's/^/  /' \
    || echo "  (档位判定失败，默认 CPU 兜底，不影响使用)"
else
  echo "  (无可用 python，装好引擎后自动按档位运行；CPU 兜底始终可用)"
fi
if [ -n "$PY" ]; then
  echo
  info "可用命令（把 PY 指到上面的 python）： PY=\"$PY\""
fi

echo
echo "----------------------------------------"
if [ "$FIX" = "1" ]; then echo "检查完毕（已尝试自动修复可修复项）。"; else echo "检查完毕。可加 --fix 自动修复（建 venv/下载模型/编译 capture）。"; fi
