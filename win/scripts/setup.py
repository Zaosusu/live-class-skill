#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""setup.py — Windows 版环境检查/一键就绪。

Windows 版 live-class-skill 分三层：
  1. 内录（系统声音）→ WASAPI loopback，纯系统 API，零第三方 —— record.py 直接可用；
  2. 转写引擎 sherpa-onnx（Python 本地离线 ASR）→ 与 mac 版共用同一个
     中文 zipformer-ctc 模型，需本机一个装了 sherpa-onnx 的 Python；
  3. 离线中文模型（约 370MB）→ 缓存于 %USERPROFILE%\\.cache\\live-class-skill\\models。

用法：
  python setup.py               # 只检查并打印报告（推荐先跑这个）
  python setup.py --fix         # 尽力自动修复：建 scripts\\.venv 并装 sherpa-onnx、下载模型

注意：转写与模型与 mac 版完全共享（同一引擎、同一模型缓存目录），
只要 mac 版装过，Windows 版通常无需重复下载模型。
"""
import argparse
import os
import shutil
import subprocess
import sys

HOME = os.path.expanduser("~")
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 仓库根目录 scripts/ 是通用层（transcribe.py / common.py 放那）
ROOT_SCRIPTS = os.path.join(SKILL_DIR, "scripts")
VENV_DIR = os.path.join(SKILL_DIR, "win", "scripts", ".venv")
VENV_PY = os.path.join(VENV_DIR, "Scripts", "python.exe")
MODELS_ROOT = os.environ.get("LCC_MODELS_DIR",
                             os.path.join(HOME, ".cache", "live-class-skill", "models"))
MODEL_SUBDIR = "sherpa-onnx-zipformer-ctc-zh-int8-2025-07-03"
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
             "sherpa-onnx-zipformer-ctc-zh-int8-2025-07-03.tar.bz2")

ok = lambda m: print(f"  [OK]   {m}")
miss = lambda m: print(f"  [缺失] {m}")
info = lambda m: print(f"  [指引] {m}")
step = lambda m: print(f"\n== {m} ==")


def _has_sherpa(py):
    try:
        r = subprocess.run([py, "-c", "import sherpa_onnx"],
                           capture_output=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def _sys_python():
    return sys.executable


def check_loopback():
    step("1/3 内录能力（WASAPI loopback，纯系统 API）")
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    try:
        import _wasapi
        res = _wasapi.probe_loopback()
        if res["ok"]:
            ok("系统输出设备可用：" + res["message"])
        else:
            miss("内录不可用：" + res["message"])
            info("需接上/启用一个可用的默认音频输出设备（扬声器/耳机/USB声卡），再重试。")
    except Exception as e:
        miss(f"无法加载内录模块 _wasapi：{e}")


def check_sherpa(fix):
    step("2/3 转写引擎 sherpa-onnx（Python 本地离线 ASR）")
    py = None
    if os.path.exists(VENV_PY) and _has_sherpa(VENV_PY):
        py = VENV_PY
    elif _has_sherpa(_sys_python()):
        py = _sys_python()
    if py:
        ok(f"python + sherpa-onnx：{py}")
        return py, True
    miss("没有可用的 sherpa-onnx 环境")
    info("检测到候选解释器：" + _sys_python())
    if fix:
        info("正在创建 venv 并安装 sherpa-onnx（联网下载，几分钟）...")
        try:
            subprocess.run([_sys_python(), "-m", "venv", VENV_DIR], check=True)
            subprocess.run([VENV_PY, "-m", "pip", "install", "--upgrade", "pip"],
                           check=True, capture_output=True)
            subprocess.run([VENV_PY, "-m", "pip", "install", "sherpa-onnx", "soundfile"],
                           check=True, capture_output=True)
            if _has_sherpa(VENV_PY):
                ok(f"安装完成：{VENV_PY}")
                return VENV_PY, True
            miss("安装后仍无法 import sherpa_onnx")
        except Exception as e:
            miss(f"自动安装失败：{e}")
    else:
        info("修复：python setup.py --fix")
        info(f"  （将在 {VENV_DIR} 建 venv 并安装 sherpa-onnx soundfile）")
    return None, False


def check_model(fix, has_py):
    step("3/3 离线中文模型（zipformer-ctc int8，约 370MB，mac/win 共用缓存）")
    model_dir = os.path.join(MODELS_ROOT, MODEL_SUBDIR)
    model = os.path.join(model_dir, "model.int8.onnx")
    tokens = os.path.join(model_dir, "tokens.txt")
    if os.path.exists(model) and os.path.exists(tokens):
        ok(f"模型已就绪：{model_dir}")
        return True
    miss("模型未下载")
    if fix:
        info("正在下载模型（370MB，需几分钟）...")
        os.makedirs(MODELS_ROOT, exist_ok=True)
        tmp = os.path.join(MODELS_ROOT, MODEL_SUBDIR + ".tar.bz2")
        try:
            import urllib.request
            info("  " + MODEL_URL)
            urllib.request.urlretrieve(MODEL_URL, tmp)
            # 解压 tar.bz2：优先用 python 标准库 tarfile（仅支持 bz2 需要 _bz2，标准库有）
            import tarfile
            with tarfile.open(tmp, "r:bz2") as tf:
                tf.extractall(MODELS_ROOT)
            os.remove(tmp)
            if os.path.exists(model) and os.path.exists(tokens):
                ok(f"模型下载并解压完成：{model_dir}")
                return True
            miss("模型解压后文件不完整，请检查网络/磁盘后重试")
        except Exception as e:
            miss(f"模型下载失败：{e}")
    else:
        info("修复：python setup.py --fix（自动下载并解压）")
        info(f"  目标目录：{MODELS_ROOT}")
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fix", action="store_true", help="自动修复可修复项")
    args = ap.parse_args()
    fix = args.fix

    print(f"技能目录：{SKILL_DIR}")
    check_loopback()
    _py, ok_sherpa = check_sherpa(fix)
    check_model(fix, ok_sherpa)
    print()
    print("-" * 60)
    if not fix:
        print("检查完毕。可加 --fix 自动修复（建 venv 装 sherpa-onnx / 下载模型）。")
        print("转写复用：根目录 scripts/transcribe.py（与 mac 版共用）。")
    else:
        print("检查完毕（已尝试自动修复）。")


if __name__ == "__main__":
    main()
