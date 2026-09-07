#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""accel.py — 「运行加速档位」探测与决策（GPU优先 / CPU兜底 / 可选 API）。

统一本技能（mac+win 共用）如何决定用哪个后端跑转写：
  1. 先看有没有 NVIDIA GPU；有 GPU 再看有没有可复用的 CUDA 运行库。
  2. 决策结果分三档：
       gpu   —— 有 NVIDIA GPU 且能找到 CUDA 运行库（sherpa GPU 版才能生效）
       cpu   —— 兜底档，任何机器都能用（sherpa CPU 版，零 CUDA 依赖）
       api   —— 可选第三方 ASR API（如配置了 key，本技能不强依赖）
  3. 交互约定（供 setup / 主代理向用户说明）：
       - 能上 GPU 就上 GPU，不需要问。
       - 没有 GPU / 有 GPU 但缺运行库 → 默认 CPU 兜底（开箱即用，绝不卡住）。
         * 是否主动问用户「要不要降级 CPU」由调用方决定；若用户不懂/不关心 → 自动 CPU。
       - 可顺带告知用户「可选配第三方 ASR API」；用户不懂/不选 → 依旧 CPU。

纯探测、零安装、跨平台安全：Windows 探测 nvidia-smi + CUDA 运行库；mac/Linux 本档位
逻辑同样成立（mac 上 CUDA 一般无 → 走 cpu）。绝不下载任何 CUDA。
可用环境变量强制：
  LCC_ACCEL=cuda|cpu|api    （强制指定档位，跳过自动探测）
"""
import os
import shutil
import subprocess
import sys

try:
    import cuda_rt  # 同目录；纯 Python 定位 CUDA 运行库目录
except Exception:  # 兼容直接 import 场景
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import cuda_rt  # noqa: E402


def has_nvidia_gpu():
    """是否有 NVIDIA GPU。用 nvidia-smi 是否存在做强信号（驱动级，零额外依赖）。"""
    if shutil.which("nvidia-smi"):
        return True
    # Windows 常见落点兜底
    if os.name == "nt":
        return os.path.exists(r"C:\Windows\System32\nvidia-smi.exe")
    return False


def cuda_rt_dir():
    """可用 CUDA 运行库目录（None 表示没找到）。"""
    return cuda_rt.find_cuda_rt_dir()


def detect():
    """返回 {accel, reason, cuda_rt, gpu} 决策。accel ∈ {gpu, cpu}。"""
    forced = (os.environ.get("LCC_ACCEL") or "").strip().lower()
    if forced in ("gpu", "cuda", "cpu", "api"):
        if forced == "cuda":
            forced = "gpu"
        return {"accel": forced, "reason": f"LCC_ACCEL 强制={forced}", "cuda_rt": None, "gpu": has_nvidia_gpu()}
    gpu = has_nvidia_gpu()
    rt = cuda_rt.find_cuda_rt_dir()
    if gpu and rt:
        return {"accel": "gpu", "reason": "有 NVIDIA GPU 且找到 CUDA 运行库", "cuda_rt": rt, "gpu": True}
    if gpu:
        return {"accel": "cpu", "reason": "有 NVIDIA GPU，但未找到可复用的 CUDA 运行库→CPU 兜底", "cuda_rt": None, "gpu": True}
    return {"accel": "cpu", "reason": "未检测到 NVIDIA GPU→CPU 兜底", "cuda_rt": None, "gpu": False}


def banner(show_api_hint=True):
    """打印一段清晰的中文决策说明（供 setup / 主代理转述给用户）。"""
    d = detect()
    lines = []
    if d["gpu"]:
        lines.append("检测到 NVIDIA GPU ✓")
    else:
        lines.append("未检测到 NVIDIA GPU（无独显/驱动）")
    if d["cuda_rt"]:
        lines.append(f"找到可复用 CUDA 运行库：{d['cuda_rt']}")
    else:
        lines.append("未找到可复用 CUDA 运行库")
    if d["accel"] == "gpu":
        lines.append("→ 转写档位：GPU 优先（sherpa CUDA 版）")
    else:
        lines.append("→ 转写档位：CPU 兜底（零 CUDA 依赖，开箱即用）")
    if show_api_hint:
        lines.append("（可选：配置第三方 ASR API 可替换本地引擎；不配则用上面档位）")
    lines.append(f"判定依据：{d['reason']}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(banner())
