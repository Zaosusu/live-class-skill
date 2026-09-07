#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""record.py — Windows 版：启停"系统正在播放的声音"内录（WASAPI loopback，零第三方）。

等价于 mac 版 record.py + capture.swift：
  - mac 用系统自带 ScreenCaptureKit 抓系统混音；
  - Windows 用系统自带 Core Audio / WASAPI loopback 抓"正在播放的一切声音"。
本脚本 + win/scripts/_wasapi.py 是纯 Python + ctypes + Windows 系统 API 实现，
不装虚拟声卡、不用 ffmpeg、不用任何第三方录音软件，也不碰浏览器、无任何自动化。

产出与会话协议与 mac 版完全兼容（供根目录 scripts/transcribe.py 直接复用）：
  <session>/meta.json            # {"started_at","engine":"wasapi_loopback",...}
  <session>/record.pid           # 本(采集)进程 pid
  <session>/chunks/seg_*.wav     # 16kHz/单声道/16bit PCM 分块(约 segment 秒)
  <session>/stop.flag            # 由 `record.py stop` 写入，采集进程轮询到即优雅退出
  <session>/record.log           # 采集进程自己的日志（由调用方重定向）

用法：
  python record.py check                         # 检查内录(loopback)是否可用
  python record.py start  --session <DIR> [--segment 20]
  python record.py stop   --session <DIR>

说明：
  - start 常驻前台采集；由 agent/调用方放后台运行并重定向日志。
  - stop 通过写 <session>/stop.flag 通知采集进程优雅落盘并退出（等同 mac 的 SIGTERM 语义）。
  - 系统没有声音在播放时不产生分块（与 mac 版一致，静音不落盘）。
  - 全程零第三方、零浏览器自动化。
"""
import argparse
import json
import os
import sys
import time
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _wasapi  # noqa: E402

SAMPLE_RATE = 16000
WAVE_BPS = 2      # 16bit PCM
_POLL = 0.05


# ---------------------------------------------------------------- wav 写入
def _write_wav_header(f, data_size):
    """写 16k/mono/16bit PCM wav 头，data_size 为数据字节数。f 当前在开头。"""
    import struct
    f.write(b"RIFF")
    f.write(struct.pack("<I", 36 + data_size))
    f.write(b"WAVE")
    f.write(b"fmt ")
    f.write(struct.pack("<IHHIIHH", 16, 1, 1, SAMPLE_RATE,
                        SAMPLE_RATE * WAVE_BPS, WAVE_BPS, 16))
    f.write(b"data")
    f.write(struct.pack("<I", data_size))


def _s16(v):
    """float [-1,1] → int16。"""
    if v >= 1.0:
        return 32767
    if v <= -1.0:
        return -32768
    return int(v * 32767.0)


class ChunkWriter:
    """把采集到的 float32 mono 样本按 segment 秒写成分块 wav（与 mac capture 兼容）。"""

    def __init__(self, session, segment=20):
        self.chunks_dir = os.path.join(session, "chunks")
        os.makedirs(self.chunks_dir, exist_ok=True)
        self.seg_samples = max(1, int(segment) * SAMPLE_RATE)
        self._fh = None
        self._path = None
        self._samples = 0

    def _new_chunk(self):
        # 写入中的分块用 .part 临时名，finalize 完整后再改名 .wav。
        # 否则监听端（transcribe 按 *.wav 扫描 + 文件名去重）会读到只写了
        # 头的半成品：要么误判静音跳过、要么只转写前半段丢数据。
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        self._name = f"seg_{ts}.wav"
        self._path = os.path.join(self.chunks_dir, self._name + ".part")
        self._fh = open(self._path, "wb")
        self._samples = 0
        _write_wav_header(self._fh, 0)

    def write(self, samples):
        """samples: 可迭代 float（单声道）。"""
        if not samples:
            return
        # 用数组聚合 int16，减少 write 次数
        buf = bytearray()
        if self._fh is None:
            self._new_chunk()
        import array as _arr
        a = _arr.array("h")
        for v in samples:
            a.append(_s16(v))
        self._fh.write(a.tobytes())
        self._samples += len(a)
        if self._samples >= self.seg_samples:
            self._finalize()

    def _finalize(self):
        if self._fh is None:
            return
        data_size = self._samples * WAVE_BPS
        # 回填 RIFF/data 长度
        self._fh.seek(0)
        import struct
        self._fh.write(b"RIFF")
        self._fh.write(struct.pack("<I", 36 + data_size))
        self._fh.seek(40)
        self._fh.write(struct.pack("<I", data_size))
        self._fh.close()
        self._fh = None
        # .part -> .wav（只有完整落盘后才让监听端可见）
        final = os.path.join(self.chunks_dir, self._name)
        try:
            os.replace(self._path, final)
        except OSError:
            pass
        print(f"[record] 写出分块 {self._name} "
              f"({self._samples / SAMPLE_RATE:.1f}s)", flush=True)

    def close(self):
        """收尾：封口不足一段的尾块。"""
        if self._fh is not None:
            self._finalize()


# ---------------------------------------------------------------- 命令
def cmd_check(_args):
    res = _wasapi.probe_loopback()
    if res["ok"]:
        print("[record] 内录就绪：", res["message"], flush=True)
        return 0
    print("[record] 内录不可用：", res["message"], flush=True)
    return 1


def cmd_start(args):
    session = os.path.abspath(args.session)
    os.makedirs(session, exist_ok=True)
    segment = max(1, int(args.segment))

    # 单实例保护：若已有本会话采集进程在跑则退出
    pidfile = os.path.join(session, "record.pid")
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile, encoding="utf-8").read().strip())
            if _pid_alive(old):
                print(f"[record] 已有采集进程 pid={old} 在运行，如需重启先 record.py stop。",
                      flush=True)
                return 1
        except Exception:
            pass

    rec = _wasapi.LoopbackRecorder(sample_rate=SAMPLE_RATE, channels=1)
    try:
        rec.start()
    except Exception as e:
        print(f"[record] 启动内录失败：{e}", flush=True)
        return 1

    # 写 meta / pid
    with open(pidfile, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    # meta.json 写合并而非覆盖：listen.py 可能已写好全量元数据
    # （url/主播/主题/听课人/kind 等），这里只补缺省字段，避免整份覆盖丢失。
    meta_path = os.path.join(session, "meta.json")
    meta = {}
    try:
        if os.path.exists(meta_path):
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f) or {}
            if not isinstance(meta, dict):
                meta = {}
    except Exception:
        meta = {}
    meta.setdefault("started_at", datetime.datetime.now().isoformat(timespec="seconds"))
    meta.setdefault("engine", "wasapi_loopback")
    meta.setdefault("segment_seconds", segment)
    meta.setdefault("sample_rate", SAMPLE_RATE)
    meta.setdefault("channels", 1)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # 清除可能的旧 stop.flag
    stopflag = os.path.join(session, "stop.flag")
    if os.path.exists(stopflag):
        os.remove(stopflag)

    print(f"[record] 开始内录系统声音（WASAPI loopback，{SAMPLE_RATE}Hz 单声道）→ "
          f"{session}/chunks/", flush=True)
    print("[record] 结束：record.py stop --session 该目录；本进程常驻。", flush=True)

    writer = ChunkWriter(session, segment)
    try:
        while True:
            if os.path.exists(stopflag):
                print("[record] 收到停止信号，封口尾块退出。", flush=True)
                break
            blk = rec.read_block()
            if blk:
                writer.write(blk)
            else:
                time.sleep(_POLL)
    except KeyboardInterrupt:
        print("[record] 收到 Ctrl-C，优雅退出。", flush=True)
    finally:
        writer.close()
        rec.stop()
        # 清理 pid
        try:
            if os.path.exists(pidfile):
                os.remove(pidfile)
        except Exception:
            pass
    return 0


def cmd_stop(args):
    session = os.path.abspath(args.session)
    pidfile = os.path.join(session, "record.pid")
    if not os.path.exists(pidfile):
        print("[record] 未找到 record.pid，采集进程可能已退出。", flush=True)
        return 1
    stopflag = os.path.join(session, "stop.flag")
    with open(stopflag, "w", encoding="utf-8") as f:
        f.write("stop")
    # 给采集进程一点时间优雅退出
    for _ in range(50):
        if not os.path.exists(pidfile):
            print("[record] 采集进程已退出，分块已落盘。", flush=True)
            return 0
        time.sleep(0.1)
    print("[record] 已发送停止信号（等待采集进程落盘退出）。", flush=True)
    return 0


def _pid_alive(pid):
    """判断 pid 是否存活（Windows）。"""
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, int(pid))
        if not h:
            return False
        code = ctypes.c_uint32()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        # 259 = STILL_ACTIVE
        return bool(ok) and code.value == 259
    except Exception:
        return False


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="检查内录(loopback)可用性")
    ps = sub.add_parser("start", help="开始内录系统声音（常驻前台）")
    ps.add_argument("--session", required=True, help="会话目录")
    ps.add_argument("--segment", type=int, default=20, help="每段秒数(默认20)")
    pt = sub.add_parser("stop", help="停止内录")
    pt.add_argument("--session", required=True, help="会话目录")
    args = p.parse_args()
    code = {"check": cmd_check, "start": cmd_start, "stop": cmd_stop}[args.cmd](args)
    sys.exit(code or 0)


if __name__ == "__main__":
    main()
