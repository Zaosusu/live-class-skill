#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_wasapi.py — Windows 系统自带 WASAPI loopback 内录引擎（纯 ctypes，零第三方依赖）。

做什么
  等价于 mac 版用 ScreenCaptureKit 抓"系统正在播放的声音"：
  不解析任何直播/会议协议，不装虚拟声卡、不用 ffmpeg、不装任何第三方录音软件，
  直接读 Windows Core Audio 默认"渲染(输出)设备"的 **loopback(回环)流**，
  即系统当前正在播放的一切声音 → 统一重采样为 16000Hz 单声道 float32。

为什么可行
  WASAPI loopback 是 Windows 自带的系统接口（Windows Vista 起就有），
  OBS / 各会议转录软件在 Windows 上"录制扬声器(输出)"用的正是同一接口。
  本文件用 Python 标准库 ctypes + ole32 手写 COM 调用，不依赖 comtypes /
  sounddevice / numpy 等任何第三方包，也不碰浏览器、不做任何自动化。

对外接口（给 record.py 用）
  probe_loopback() -> {"ok":bool, "message":str}   # 探测能否读默认输出设备的回环
  LoopbackRecorder(sample_rate=16000, channels=1)
      .start()              # 打开 loopback 流
      .read_block()         # 拉一包：返回 array('f') 16k/mono；无可读返回空
      .stop()

内部
  - 事件驱动的拉取：IOle 不引入复杂回调，采用 sleep 短轮询 GetNextPacketSize，
    稳健且跨 Windows 版本一致。
  - 格式自适应：真实混音格式可能是 48k/44.1k、单/双/更多声道、float32/int32/
    int16 交织。本引擎自动识别 → 下混(声道取平均) → 线性插值降采样到 16k mono。
  - 全程仅调用系统公开 COM 接口（IMMDeviceEnumerator/IMMDevice/IAudioClient/
    IAudioCaptureClient），无任何第三方组件、无浏览器自动化。
"""
import array
import ctypes
import os
import struct
import time
from ctypes import (wintypes, POINTER, byref, c_void_p, c_uint32,
                    c_int, c_longlong, c_ulonglong, cast)

# --------------------------------------------------------------------------
# 基本常量与 COM 基础设施
# --------------------------------------------------------------------------
ole32 = ctypes.windll.ole32
ole32.CoInitialize(None)

CLSCTX_ALL = 23  # CLSCTX_INPROC_SERVER|INPROC_HANDLER|LOCAL_SERVER|REMOTE_SERVER

# Core Audio 相关 GUID（均为 Windows 系统公开常量）
CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
IID_IMMDevice = "{D666063F-1587-4E43-81F1-B948E807363F}"
IID_IAudioClient = "{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}"
IID_IAudioCaptureClient = "{C8ADBD64-E71E-48A0-A4DE-185C395CD317}"

# GUID → 二进制(小端结构：Data1..4 + Data2..2 + Data3..2 + Data4..8)
def _guid_bytes(guid_str):
    s = guid_str.strip("{}").replace("-", "")
    d1 = int(s[0:8], 16)
    d2 = int(s[8:12], 16)
    d3 = int(s[12:16], 16)
    rest = bytes.fromhex(s[16:32])
    return struct.pack("<IHH", d1, d2, d3) + rest

# AUDCLNT 常量
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
AUDCLNT_E_DEVICE_INVALIDATED = 0x88890004
AUDCLNT_E_BUFFER_ERROR = 0x88890018
WAVE_FORMAT_EXTENSIBLE = 0xFFFE
WAVE_FORMAT_IEEE_FLOAT = 0x0003
WAVE_FORMAT_PCM = 0x0001
KSDATAFORMAT_SUBTYPE_IEEE_FLOAT = "{00000003-0000-0010-8000-00AA00389B71}"
KSDATAFORMAT_SUBTYPE_PCM = "{00000001-0000-0010-8000-00AA00389B71}"

# 等待设备的超时与重试
_POLL_INTERVAL = 0.02
_PROBE_TIMEOUT = 1.0


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


# --------------------------------------------------------------------------
# 手写 COM vtable 调用工具
# --------------------------------------------------------------------------
class _ComPtr:
    """极简 COM 对象封装：只做 AddRef/Release + 按索引调 vtable 方法。"""
    def __init__(self, p):
        self.p = c_void_p(p) if not isinstance(p, c_void_p) else p
        self._vtable = None
        if self.p.value:
            self._vtable = ctypes.cast(
                ctypes.cast(self.p, POINTER(c_void_p))[0],
                POINTER(c_void_p))

    def release(self):
        if self.p.value:
            try:
                self._call(2)  # Release 是 vtable index 2
            except Exception:
                pass
            self.p = c_void_p()
            self._vtable = None

    def _call(self, index, restype=c_int, *argtypes):
        """调用 vtable 第 index 个方法。argtypes 为该方法的参数类型(不含 this)。"""
        if self._vtable is None or not self.p.value:
            raise OSError("COM 对象已释放")
        proto = ctypes.CFUNCTYPE(restype, c_void_p, *argtypes)
        fn = ctypes.cast(self._vtable[index], proto)
        return fn(self.p)

    def call(self, index, restype, argtypes, *args):
        if self._vtable is None or not self.p.value:
            raise OSError("COM 对象已释放")
        proto = ctypes.CFUNCTYPE(restype, c_void_p, *argtypes)
        fn = ctypes.cast(self._vtable[index], proto)
        return fn(self.p, *args)


# --------------------------------------------------------------------------
# 便捷封装：把 COM 返回的指针转成指定接口封装
# --------------------------------------------------------------------------
def _as_comptr(raw):
    return _ComPtr(raw)


# --------------------------------------------------------------------------
# 顶层：获取默认渲染设备的 IAudioClient
# --------------------------------------------------------------------------
class _Device:
    """封装 IMMDevice + 默认输出设备打开。"""
    @staticmethod
    def _mk(riid_bytes):
        """CoCreateInstance(MMDeviceEnumerator, riid) → _ComPtr。"""
        clsid = ctypes.create_string_buffer(_guid_bytes(CLSID_MMDeviceEnumerator))
        riid = ctypes.create_string_buffer(riid_bytes)
        ppv = c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.cast(clsid, POINTER(ctypes.c_ubyte)),
            None, CLSCTX_ALL,
            ctypes.cast(riid, POINTER(ctypes.c_ubyte)),
            byref(ppv))
        if hr != 0:
            raise OSError(f"CoCreateInstance 失败 HRESULT=0x{hr & 0xFFFFFFFF:08X}")
        return _ComPtr(ppv.value)

    def __init__(self):
        self._enum = self._mk(_guid_bytes(IID_IMMDeviceEnumerator))

    # vtable: IMMDeviceEnumerator[0]QueryInterface [1]AddRef [2]Release
    #         [3]EnumAudioEndpoints [4]GetDefaultAudioEndpoint
    def get_default_render_device(self):
        eDataFlow = 0   # eRender
        eRole = 0       # eConsole
        ppdev = c_void_p()
        hr = self._enum.call(4, c_int, [c_int, c_int, POINTER(c_void_p)],
                             eDataFlow, eRole, byref(ppdev))
        if hr != 0:
            raise OSError(f"GetDefaultAudioEndpoint 失败 HRESULT=0x{hr & 0xFFFFFFFF:08X}")
        return _ComPtr(ppdev.value)

    def close(self):
        try:
            self._enum.release()
        except Exception:
            pass


def _device_activate_audio_client(dev):
    """IMMDevice.Activate(IAudioClient)。vtable: dev[3]=Activate"""
    riid = ctypes.create_string_buffer(_guid_bytes(IID_IAudioClient))
    ppv = c_void_p()
    # Activate(refiid, dwClsCtx, pActivationParams, ppInterface)
    hr = dev.call(3, c_int,
                  [POINTER(ctypes.c_ubyte), c_int, c_void_p, POINTER(c_void_p)],
                  ctypes.cast(riid, POINTER(ctypes.c_ubyte)),
                  CLSCTX_ALL, None, byref(ppv))
    if hr != 0:
        raise OSError(f"Activate(IAudioClient) 失败 HRESULT=0x{hr & 0xFFFFFFFF:08X}")
    return _ComPtr(ppv.value)


def _audio_client_get_mix_format(ac):
    """IAudioClient.GetMixFormat → (raw_ptr, WAVEFORMATEX拷贝)。
    raw_ptr 为系统 CoTaskMem 分配的 WAVEFORMATEX*，须保留供 Initialize(pFormat) 用。
    vtable: ac[8]=GetMixFormat"""
    ppwf = POINTER(WAVEFORMATEX)()
    hr = ac.call(8, c_int, [POINTER(POINTER(WAVEFORMATEX))], byref(ppwf))
    if hr != 0 or not ppwf:
        raise OSError(f"GetMixFormat 失败 HRESULT=0x{hr & 0xFFFFFFFF:08X}")
    raw_ptr = ctypes.cast(ppwf, c_void_p).value
    # 拷贝头字段，避免结构体随指针对象失效
    fmt = ppwf.contents
    out = WAVEFORMATEX()
    ctypes.memmove(byref(out), byref(fmt), ctypes.sizeof(WAVEFORMATEX))
    return raw_ptr, out


# --------------------------------------------------------------------------
# WASAPI loopback 录音器（面向 record.py）
# --------------------------------------------------------------------------
class LoopbackRecorder:
    """读系统正在播放的声音（loopback）→ 16k mono float32。零第三方。"""

    def __init__(self, sample_rate=16000, channels=1):
        self.out_rate = int(sample_rate)
        self.out_channels = int(channels)
        self._ac = None
        self._acc = None
        self._running = False
        self._mix = None   # dict: rate, channels, bytes_per_sample, is_float
        self._frame_bytes = 0

    # -- 公共 API -------------------------------------------------------
    def start(self):
        d = _Device()
        try:
            dev = d.get_default_render_device()
            self._dev = dev
            self._ac = _device_activate_audio_client(dev)
            # 共享模式 loopback 的 Initialize 必须传 GetMixFormat 返回的真实格式指针
            wf_ptr, mix = _audio_client_get_mix_format(self._ac)
            self._wf_ptr = wf_ptr
            self._mix = self._analyze_format(mix)
        finally:
            d.close()

        # IAudioClient.Initialize(AUDCLNT_SHAREMODE_SHARED=0, flags=LOOPBACK,
        #                         hnsBufferDuration=10ms, hnsPeriodicity=0,
        #                         pFormat=真实格式指针, AudioSessionGuid=NULL)
        hns = 10000000 // 2  # 0.5s 参考；实际用默认即可
        hr = self._ac.call(3, c_int,
                           [c_int, c_int, c_longlong, c_longlong,
                            c_void_p, c_void_p],
                           0, AUDCLNT_STREAMFLAGS_LOOPBACK, hns, 0,
                           self._wf_ptr, None)
        if hr != 0:
            raise OSError(f"IAudioClient.Initialize(loopback) 失败 HRESULT=0x{hr & 0xFFFFFFFF:08X}")

        # GetService(IAudioCaptureClient) vtable: ac[0..] 需按官方 vtable。
        # IAudioClient vtable（官方，index 从 0）:
        #  0 QI 1 AddRef 2 Release 3 Initialize 4 GetBufferSize 5 GetStreamLatency
        #  6 GetCurrentPadding 7 IsFormatSupported 8 GetMixFormat 9 GetDevicePeriod
        #  10 Start 11 Stop 12 Reset 13 SetEventHandle 14 GetService
        riid = ctypes.create_string_buffer(_guid_bytes(IID_IAudioCaptureClient))
        pp = c_void_p()
        hr = self._ac.call(14, c_int,
                           [POINTER(ctypes.c_ubyte), POINTER(c_void_p)],
                           ctypes.cast(riid, POINTER(ctypes.c_ubyte)), byref(pp))
        if hr != 0:
            raise OSError(f"GetService(IAudioCaptureClient) 失败 HRESULT=0x{hr & 0xFFFFFFFF:08X}")
        self._acc = _ComPtr(pp.value)

        # Start() vtable index 10
        hr = self._ac.call(10, c_int, [])
        if hr != 0:
            raise OSError(f"IAudioClient.Start() 失败 HRESULT=0x{hr & 0xFFFFFFFF:08X}")
        self._running = True
        return True

    def _analyze_format(self, fmt):
        """判断真实混音格式 → 采样参数。"""
        rate = fmt.nSamplesPerSec or 48000
        nch = fmt.nChannels or 2
        bits = fmt.wBitsPerSample or 32
        tag = fmt.wFormatTag
        is_float = (tag == WAVE_FORMAT_IEEE_FLOAT)
        if tag == WAVE_FORMAT_EXTENSIBLE:
            # 需要读 SubFormat；WAVEFORMATEX.cbSize>0 时其后有 GUID。
            # 简化判断：EXTENSIBLE 的 bits 为浮点时多为 32。
            is_float = (bits == 32)
        bytes_per = bits // 8
        if bytes_per not in (2, 4):
            bytes_per = 4 if is_float else 2
        return {"rate": rate, "channels": nch,
                "bytes": bytes_per, "is_float": is_float}

    def read_block(self):
        """拉一包 loopback 数据 → array('f') 16k mono；无可读返回空 bytes。

        IAudioCaptureClient vtable: 0 QI 1 AddRef 2 Release 3 GetBuffer
        4 ReleaseBuffer 5 GetNextPacketSize
        """
        if not self._running or self._acc is None:
            return b""
        # GetNextPacketSize
        psize = c_uint32(0)
        hr = self._acc.call(5, c_int, [POINTER(c_uint32)], byref(psize))
        if hr != 0 or psize.value == 0:
            return b""
        # GetBuffer
        pdata = c_void_p()
        pframes = c_uint32(0)
        pflags = c_uint32(0)
        pdevpos = c_uint32(0)
        pqpct = c_ulonglong(0)
        hr = self._acc.call(3, c_int,
                            [POINTER(c_void_p), POINTER(c_uint32), POINTER(c_uint32),
                             POINTER(c_uint32), POINTER(c_ulonglong)],
                            byref(pdata), byref(pframes), byref(pflags),
                            byref(pdevpos), byref(pqpct))
        nframes = pframes.value
        if hr != 0 or nframes == 0 or not pdata.value:
            # 仍要 ReleaseBuffer(0)
            self._acc.call(4, c_int, [c_uint32], 0)
            return b""
        out = self._convert(pdata.value, nframes, pflags.value)
        # ReleaseBuffer(nframes)
        self._acc.call(4, c_int, [c_uint32], nframes)
        return out

    def _convert(self, data_ptr, nframes, flags):
        m = self._mix
        rate = m["rate"]; nch = m["channels"]
        bps = m["bytes"]; is_float = m["is_float"]
        if rate <= 0 or nch <= 0 or nframes <= 0:
            return b""
        raw_len = nframes * nch * bps
        raw = ctypes.string_at(data_ptr, raw_len)
        if not raw:
            return b""
        if flags & AUDCLNT_BUFFERFLAGS_SILENT:
            n = max(1, int(nframes * self.out_rate / rate))
            return array.array("f", [0.0]) * n
        # 交织解包
        if is_float and bps == 4:
            fmt = "<" + "f" * (nframes * nch)
            vals = struct.unpack(fmt, raw)
        else:
            fmt = "<" + ("h" if bps == 2 else "i") * (nframes * nch)
            vals = struct.unpack(fmt, raw)
            scale = 32768.0 if bps == 2 else 2147483648.0
            vals = [v / scale for v in vals]
        # 下混
        if nch == 1:
            mono = list(vals)
        else:
            mono = [sum(vals[f * nch:(f + 1) * nch]) / nch for f in range(nframes)]
        # 降采样到 out_rate
        if rate == self.out_rate:
            res = mono
        else:
            ratio = rate / float(self.out_rate)
            out_len = max(1, int(nframes / ratio))
            res = []
            for i in range(out_len):
                src = i * ratio
                i0 = int(src)
                i1 = i0 + 1 if i0 + 1 < len(mono) else i0
                frac = src - i0
                res.append(mono[i0] * (1 - frac) + mono[i1] * frac)
        return array.array("f", res)

    def stop(self):
        self._running = False
        if self._ac is not None:
            try:
                self._ac.call(11, c_int, [])   # Stop()
            except Exception:
                pass
        for obj in ("_acc", "_ac", "_dev"):
            o = getattr(self, obj, None)
            if o is not None:
                try:
                    o.release()
                except Exception:
                    pass
                setattr(self, obj, None)


def probe_loopback():
    """探测能否打开默认输出设备的 loopback。返回 {"ok","message"}。"""
    try:
        r = LoopbackRecorder()
        r.start()
        r.stop()
        m = r._mix or {}
        rate = m.get("rate") or "?"
        nch = m.get("channels") or "?"
        return {"ok": True,
                "message": (f"默认输出设备可用（系统混音 {rate}Hz / {nch} 声道）。"
                            f"WASAPI loopback 内录已就绪，可录制系统正在播放的声音。")}
    except Exception as e:
        return {"ok": False, "message": f"无法打开系统输出设备 loopback：{e}"}


if __name__ == "__main__":
    import json
    print(json.dumps(probe_loopback(), ensure_ascii=False))
