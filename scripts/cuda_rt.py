#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cuda_rt.py — 跨平台安全地定位/注入 CUDA 运行库（GPU 加速用）。

背景：sherpa-onnx 的 GPU（CUDA）版在 Windows 上运行 onnxruntime 的 CUDA provider 时，
需要在进程的 DLL 搜索路径里能找到一组运行库（cudart/cublas/cublasLt/cudnn）。
这组库可能来自：系统 CUDA Toolkit 的 bin 目录、或某个 PyTorch(torch) 随包捆绑的 torch/lib。
本模块负责"找出来 + 注入"，全程只读、跨平台安全：
  - Windows：探测到就返回可用目录；转写前用它前插 PATH + add_dll_directory。
  - macOS / Linux / 无 GPU / 无 CUDA：一律返回 None，且不产生任何副作用。
绝不安装、绝不下载任何 CUDA——只复用机器上已存在的运行库。

可用环境变量覆盖：
  LCC_CUDA_DIR=某目录   （若已知，直接指定运行库目录，跳过自动探测）

典型用法（Windows 转写进程启动时）：
  from cuda_rt import inject_cuda_rt, find_cuda_rt_dir
  d = find_cuda_rt_dir()
  if d: inject_cuda_rt(d)
"""
import os
import sys
import glob

# onnxruntime CUDA provider 加载时需要的一组运行库（在某个"含齐的目录"里存在即可）。
# sherpa GPU wheel 是 cuda12.cudnn9 → 需要 CUDA 12.x 的 cudart/cublas + cuDNN 9。
REQUIRED_RT_DLLS = [
    "cudart64_12.dll",     # CUDA Runtime
    "cublas64_12.dll",     # cuBLAS
    "cublasLt64_12.dll",   # cuBLAS-Lt（onnxruntime CUDA 依赖它，缺了就报 missing）
    "cudnn64_9.dll",       # cuDNN 9
]
# 不同版本会有 cudart64_13/cublas64_13/cudnn64_8 等，做一次宽松匹配更稳。
_RELAXED = {
    "cudart64_12.dll": "cudart64_",
    "cublas64_12.dll": "cublas64_",
    "cublasLt64_12.dll": "cublasLt64_",
    "cudnn64_9.dll": "cudnn64_",
}


def _dir_has_runtime(d):
    """判断一个目录是否"含齐" CUDA 运行库。宽松匹配 cudart/cublas/cublasLt/cudnn。"""
    if not d or not os.path.isdir(d):
        return False
    try:
        names = set(os.listdir(d))
    except OSError:
        return False
    for exact, prefix in _RELAXED.items():
        if exact in names:
            continue
        if any(n.startswith(prefix) and n.endswith(".dll") for n in names):
            continue
        return False
    return True


def _candidate_dirs():
    """返回待探测目录（按优先级从高到低）。"""
    out = []

    def add(d):
        if d and os.path.isdir(d) and d not in out:
            out.append(d)

    # 0) 显式覆盖
    add(os.environ.get("LCC_CUDA_DIR"))
    # 1) 系统 CUDA 环境变量（含各次版本）
    for k, v in os.environ.items():
        if k.upper().startswith("CUDA_PATH"):
            add(os.path.join(v, "bin"))
    # 2) 标准 Toolkit 安装目录（v12.x/v13.x 的 bin）
    tk = r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA"
    for d in sorted(glob.glob(os.path.join(tk, "v*", "bin")), reverse=True):
        add(d)
    # 3) 常见 Python/conda 环境里的 PyTorch torch/lib（随包捆绑的 CUDA 运行库）
    torch_roots = []
    #   a) 当前解释器所在的 venv/site-packages
    for sp in getattr(sys, "path", []):
        t = os.path.join(sp, "torch", "lib")
        if os.path.isdir(t):
            torch_roots.append(t)
    #   b) 常见 anaconda envs 与用户目录（浅层，防全盘慢）
    conda_base = None
    for probe in (sys.base_prefix, os.path.join(os.path.expanduser("~"), "anaconda3"),
                  os.path.join(os.path.expanduser("~"), "miniconda3"),
                  r"D:\Program Files\anaconda3", r"C:\ProgramData\anaconda3",
                  r"C:\Program Files\anaconda3"):
        if probe and os.path.isdir(os.path.join(probe, "envs")):
            conda_base = probe
            break
    if conda_base:
        for env in sorted(glob.glob(os.path.join(conda_base, "envs", "*"))):
            torch_roots.append(os.path.join(env, "Lib", "site-packages", "torch", "lib"))
        torch_roots.append(os.path.join(conda_base, "Lib", "site-packages", "torch", "lib"))
    for t in torch_roots:
        add(t)
    # 4) 已存在 PATH 里的候选（如已装 CUDA 且加过 bin 到 PATH）
    for p in os.environ.get("PATH", "").split(os.pathsep):
        add(p)
    return out


def find_cuda_rt_dir():
    """返回第一个"含齐 CUDA 运行库"的目录；找不到返回 None（跨平台安全）。"""
    if os.name != "nt":
        return None
    for d in _candidate_dirs():
        if _dir_has_runtime(d):
            return d
    return None


def inject_cuda_rt(d):
    """把 CUDA 运行库目录注入进程 DLL 搜索路径（前插 PATH + add_dll_directory）。"""
    if not d or not os.path.isdir(d):
        return False
    os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(d)
        except Exception:
            pass
    return True


if __name__ == "__main__":
    d = find_cuda_rt_dir()
    if d:
        print("CUDA_RT_DIR=" + d)
        inject_cuda_rt(d)
    else:
        print("CUDA_RT_DIR=None (未发现可用 CUDA 运行库；将用 CPU)")
