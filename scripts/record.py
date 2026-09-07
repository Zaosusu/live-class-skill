#!/usr/bin/env python3
"""record.py — 启停"系统正在播放的声音"捕获（ScreenCaptureKit 零安装方案）。

不再需要 BlackHole / ffmpeg / 切换输出设备：
本机编译好的 bin/capture 直接经 macOS 系统自带 ScreenCaptureKit 取系统混音，
产出 16kHz 单声道 wav 分块到 <session>/chunks/，供 transcribe.py 增量转写。

前提：已在 系统设置→隐私与安全性→屏幕录制 给本工具所在的 App 授权（一次性）。

用法：
  python3 record.py check                       # 检查"屏幕录制"权限是否已授予
  python3 record.py start  --session DIR [--segment 20]
  python3 record.py stop   --session DIR

说明：
  - start 用 exec 替换自身为 bin/capture（常驻前台，由调用方放后台运行）；
    capture 自行写 <session>/record.pid，收到 SIGTERM 会优雅落盘所有分块。
  - 浏览器/会议 App 正在播放声音时才有内容；静音段不产生分块。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402


def _capture_or_die():
    cap = common.capture_bin()
    if not cap:
        sys.exit("[record] 未找到已编译的捕获工具 bin/capture，请运行 setup.sh。")
    return cap


def cmd_check(_args):
    cap = _capture_or_die()
    r = os.system(f'"{cap}" --probe')
    if os.name == "posix":
        code = os.waitstatus_to_exitcode(r) if r >= 0 else r
    else:
        code = r
    if code != 0:
        print("[record] 屏幕录制权限未授予。请打开系统设置，在"
              " 隐私与安全性 → 屏幕录制 中勾选运行本工具的 App"
              "（终端 / IDE），然后完全退出并重启该 App。")
    return code


def cmd_start(args):
    cap = _capture_or_die()
    session = os.path.abspath(args.session)
    os.makedirs(session, exist_ok=True)
    print(f"[record] 启动系统声音捕获（ScreenCaptureKit）… 输出: {session}/chunks/")
    os.execv(cap, [cap, "--session", session, "--segment", str(args.segment)])
    # execv 不返回；失败才会走到这里
    sys.exit("[record] 启动捕获失败")


def cmd_stop(args):
    session = os.path.abspath(args.session)
    pidfile = os.path.join(session, "record.pid")
    if not os.path.exists(pidfile):
        sys.exit("[record] 未找到 record.pid，捕获进程可能已退出。")
    cap = _capture_or_die()
    os.execv(cap, [cap, "--stop", "--session", session])


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="检查屏幕录制权限")
    ps = sub.add_parser("start", help="开始捕获系统声音（常驻，exec 为 capture）")
    ps.add_argument("--session", required=True, help="会话目录")
    ps.add_argument("--segment", type=int, default=20, help="每段秒数(默认20)")
    pt = sub.add_parser("stop", help="停止捕获")
    pt.add_argument("--session", required=True, help="会话目录")
    args = p.parse_args()
    {"check": cmd_check, "start": cmd_start, "stop": cmd_stop}[args.cmd](args)


if __name__ == "__main__":
    main()
