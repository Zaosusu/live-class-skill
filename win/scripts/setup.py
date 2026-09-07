#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""setup.py — Windows 版环境检查/一键就绪（含 GPU 自动探测 / CPU 兜底）。

Windows 版 live-class-skill 分三层：
  1. 内录（系统声音）→ WASAPI loopback，纯系统 API，零第三方 —— record.py 直接可用；
  2. 转写引擎 sherpa-onnx（Python 本地离线 ASR）→ 与 mac 版共用同一个
     中文 zipformer-ctc 模型。引擎档位自动决策：
       有 NVIDIA GPU + 可复用 CUDA 运行库 → 装 sherpa CUDA 版（GPU 优先）；
       否则 → 装 CPU 版（零 CUDA 依赖，兜底，开箱即用）；
  3. 离线中文模型（约 370MB）→ 缓存于 %USERPROFILE%\\.cache\\live-class-skill\\models。

用法：
  python setup.py               # 只检查并打印报告（含加速档位决策）
  python setup.py --fix         # 尽力自动修复：建 scripts\\.venv、按档位装 sherpa、下载模型

注意：转写与模型与 mac 版完全共享（同一引擎、同一模型缓存目录），
只要 mac 版装过，Windows 版通常无需重复下载模型。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

# 让脚本能 import 仓库根 scripts/ 下的通用模块（accel.py / cuda_rt.py）
ROOT_SCRIPTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts")
if ROOT_SCRIPTS not in sys.path:
    sys.path.insert(0, ROOT_SCRIPTS)

try:
    import accel
    import cuda_rt
except Exception as e:  # pragma: no cover
    print(f"[fatal] 无法加载通用模块 accel/cuda_rt：{e}")
    sys.exit(1)

HOME = os.path.expanduser("~")
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VENV_DIR = os.path.join(SKILL_DIR, "win", "scripts", ".venv")
VENV_PY = os.path.join(VENV_DIR, "Scripts", "python.exe")
# marker：记录本机选定的加速档位与 CUDA 运行库目录（transcribe.py 运行时读取）
ACCEL_MARKER = os.path.join(VENV_DIR, "accel.json")
MODELS_ROOT = os.environ.get("LCC_MODELS_DIR",
                             os.path.join(HOME, ".cache", "live-class-skill", "models"))
MODEL_SUBDIR = "sherpa-onnx-zipformer-ctc-zh-int8-2025-07-03"
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
             "sherpa-onnx-zipformer-ctc-zh-int8-2025-07-03.tar.bz2")

# sherpa CUDA 版 wheel（仅当本机可上 GPU 时用）。可 LCC_CUDA_WHEEL 覆盖为一个本地 .whl 路径。
CUDA_WHEEL_URL = ("https://hf-mirror.com/csukuangfj2/sherpa-onnx-wheels/resolve/main/"
                  "cuda/1.13.7/sherpa_onnx-1.13.7+cuda12.cudnn9-cp313-cp313-win_amd64.whl")
CUDA_WHEEL_FILENAME = os.path.basename(CUDA_WHEEL_URL)

ok = lambda m: print(f"  [OK]   {m}")
miss = lambda m: print(f"  [缺失] {m}")
info = lambda m: print(f"  [指引] {m}")
step = lambda m: print(f"\n== {m} ==")


def _has_sherpa(py):
    try:
        r = subprocess.run([py, "-c", "import sherpa_onnx"],
                           capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def _sys_python():
    return sys.executable


def _write_accel_marker(accel_val, cuda_dir=None):
    os.makedirs(VENV_DIR, exist_ok=True)
    data = {"accel": accel_val, "cuda_rt": cuda_dir or None}
    try:
        with open(ACCEL_MARKER, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        info(f"（无法写档位标记 {ACCEL_MARKER}：{e}）")


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


def _install_cuda_wheel(py):
    """下载并安装 sherpa CUDA 版 wheel（仅 GPU 档位）。返回是否成功。"""
    info(f"档位=GPU：安装 sherpa CUDA 版（约 191MB，需联网）...")
    here = os.path.dirname(os.path.abspath(__file__))
    dl_dir = os.path.join(here, ".gpu_dl")
    os.makedirs(dl_dir, exist_ok=True)
    local = os.environ.get("LCC_CUDA_WHEEL")
    wheel_path = None
    if local and os.path.exists(local):
        wheel_path = local
    else:
        # 优先复用已在 dl 目录里的 wheel；没有才下载
        cached = os.path.join(dl_dir, CUDA_WHEEL_FILENAME)
        if os.path.exists(cached):
            wheel_path = cached
        else:
            target = os.path.join(dl_dir, CUDA_WHEEL_FILENAME)
            info("  下载 " + CUDA_WHEEL_URL)
            try:
                import urllib.request
                urllib.request.urlretrieve(CUDA_WHEEL_URL, target)
                wheel_path = target
            except Exception as e:
                miss(f"  CUDA 版 wheel 下载失败：{e}")
                return False
    try:
        r = subprocess.run([py, "-m", "pip", "install", "--no-deps", wheel_path],
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            miss(f"  CUDA 版 sherpa 安装失败：{(r.stderr or r.stdout)[-400:]}")
            return False
        # CUDA 版 wheel 是 --no-deps 安装的，需补上转写读音频的依赖（soundfile→numpy）
        r2 = subprocess.run([py, "-m", "pip", "install", "soundfile"],
                            capture_output=True, text=True, timeout=600)
        if r2.returncode != 0:
            miss(f"  补装 soundfile 失败：{(r2.stderr or r2.stdout)[-400:]}")
        return _has_sherpa(py)
    except Exception as e:
        miss(f"  CUDA 版 sherpa 安装异常：{e}")
        return False


def _install_cpu_sherpa(py):
    info("档位=CPU：安装 sherpa-onnx（CPU 版，零 CUDA 依赖）...")
    try:
        r = subprocess.run([py, "-m", "pip", "install", "sherpa-onnx", "soundfile"],
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            miss(f"  sherpa-onnx 安装失败：{(r.stderr or r.stdout)[-400:]}")
            return False
        return _has_sherpa(py)
    except Exception as e:
        miss(f"  sherpa-onnx 安装异常：{e}")
        return False


def check_sherpa(fix):
    step("2/3 转写引擎 sherpa-onnx（加速档位自动决策：GPU优先 / CPU兜底）")
    print("  " + accel.banner().replace("\n", "\n  "))
    dec = accel.detect()
    want_gpu = dec["accel"] == "gpu"

    # 复用已装且档位匹配的 venv
    if os.path.exists(VENV_PY) and _has_sherpa(VENV_PY):
        ok(f"python + sherpa-onnx 已就绪：{VENV_PY}")
        # 已有 venv 但没写 marker 或档位变了 → 修正 marker
        if not os.path.exists(ACCEL_MARKER):
            _write_accel_marker("gpu" if want_gpu else "cpu", dec.get("cuda_rt"))
        return VENV_PY, True
    elif _has_sherpa(_sys_python()):
        # 用系统 python（不带 GPU 自动档位能力时按现档位标注）
        ok(f"python + sherpa-onnx（系统）：{_sys_python()}")
        return _sys_python(), True

    miss("没有可用的 sherpa-onnx 环境")
    info("候选解释器：" + _sys_python())
    if not fix:
        info("修复：python setup.py --fix")
        info(f"  （将在 {VENV_DIR} 建 venv；GPU 机器装 CUDA 版，否则装 CPU 版）")
        return None, False

    info("正在创建 venv ...")
    try:
        subprocess.run([_sys_python(), "-m", "venv", VENV_DIR], check=True)
        subprocess.run([VENV_PY, "-m", "pip", "install", "--upgrade", "pip"],
                       check=True, capture_output=True, timeout=180)
    except Exception as e:
        miss(f"创建 venv 失败：{e}")
        return None, False

    if want_gpu:
        ok_engine = _install_cuda_wheel(VENV_PY)
    else:
        ok_engine = _install_cpu_sherpa(VENV_PY)

    if not ok_engine and want_gpu:
        # GPU 版装失败 → 自动回落 CPU，保证开箱即用
        info("GPU 版不可用，自动回落安装 CPU 版 ...")
        ok_engine = _install_cpu_sherpa(VENV_PY)
        dec = {"accel": "cpu", "cuda_rt": None}

    if ok_engine and _has_sherpa(VENV_PY):
        _write_accel_marker(dec.get("accel", "cpu"), dec.get("cuda_rt"))
        ok(f"安装完成：{VENV_PY}（档位={dec.get('accel')}）")
        return VENV_PY, True
    miss("安装后仍无法 import sherpa_onnx")
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
        print("提示：本机若检测到 NVIDIA GPU+CUDA，会自动用 GPU；否则用 CPU 兜底。")
        print("     也可配置第三方 ASR API（可选，不配则用上面档位）。")


if __name__ == "__main__":
    main()
