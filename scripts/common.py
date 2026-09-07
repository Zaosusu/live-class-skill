#!/usr/bin/env python3
"""live-class-copilot 共享环境探测库（可移植版，不绑定任何特定宿主环境）。

技能内所有脚本统一从这里定位：
  - Python 解释器（须装有 sherpa_onnx）
  - 系统声音捕获工具 scripts/bin/capture（ScreenCaptureKit，零安装）
  - ASR 模型目录（默认 ~/.cache/live-class-copilot/models，可 LCC_MODELS_DIR 覆盖）

可通过环境变量覆盖默认探测：
  LCC_PYTHON / LCC_MODELS_DIR
"""
import os
import shutil
import subprocess

HOME = os.path.expanduser("~")
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# sherpa-onnx 中文离线 zipformer-ctc int8 模型（模型子目录名，与 setup.sh 一致）
MODEL_SUBDIR = "sherpa-onnx-zipformer-ctc-zh-int8-2025-07-03"
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
             "sherpa-onnx-zipformer-ctc-zh-int8-2025-07-03.tar.bz2")

DEFAULT_MODELS_DIR = os.path.join(HOME, ".cache", "live-class-copilot", "models")
# 优先：技能自带虚拟环境 scripts/.venv（setup.sh 会创建）
VENV_PY = os.path.join(SKILL_DIR, "scripts", ".venv", "bin", "python")
DEFAULT_CAPTURE_BIN = os.path.join(SKILL_DIR, "scripts", "bin", "capture")


def find_python_with_sherpa():
    """返回一个装有 sherpa_onnx 的 python 解释器路径；找不到返回 None。

    探测顺序：LCC_PYTHON → scripts/.venv → 系统 python3。
    """
    candidates = []
    env = os.environ.get("LCC_PYTHON")
    if env:
        candidates.append(env)
    if os.path.exists(VENV_PY):
        candidates.append(VENV_PY)
    which = shutil.which("python3")
    if which:
        candidates.append(which)
    for py in candidates:
        if not os.path.exists(py):
            continue
        try:
            r = subprocess.run([py, "-c", "import sherpa_onnx"],
                               capture_output=True, timeout=20)
            if r.returncode == 0:
                return py
        except Exception:
            continue
    return None


def capture_bin():
    """返回已编译的系统声音捕获工具路径；缺失返回 None。"""
    env = os.environ.get("LCC_CAPTURE")
    if env and os.path.exists(env) and os.access(env, os.X_OK):
        return env
    if os.path.exists(DEFAULT_CAPTURE_BIN) and os.access(DEFAULT_CAPTURE_BIN, os.X_OK):
        return DEFAULT_CAPTURE_BIN
    return None


def models_dir():
    """返回模型根目录（不存在则自动创建）。"""
    d = os.environ.get("LCC_MODELS_DIR") or DEFAULT_MODELS_DIR
    os.makedirs(d, exist_ok=True)
    return d


def model_files():
    """返回 {dir, model, tokens}；模型缺失返回 None。"""
    d = os.path.join(models_dir(), MODEL_SUBDIR)
    model, tokens = (os.path.join(d, "model.int8.onnx"),
                     os.path.join(d, "tokens.txt"))
    if os.path.exists(model) and os.path.exists(tokens):
        return {"dir": d, "model": model, "tokens": tokens}
    return None


if __name__ == "__main__":
    print("python:", find_python_with_sherpa())
    print("capture:", capture_bin())
    print("models:", model_files())
