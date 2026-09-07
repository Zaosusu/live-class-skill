#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""listen.py — 持续听完整场直播并「边听边流式出字」的全自动编排。

把「抓主题建目录 → 内录(近流式小分块) → 增量转写出字 → 结束检测 → 自动收尾」
串成一条命令，可无人值守跑 60 分钟以上一整场直播。

会话落盘规范（单一可信源）：
    <skill>/user/<使用者>/session/<YYYYMMDD-HHMM>_<直播主题>/
        ├── meta.json                 # room/url/标题/开始时间/段长 等
        ├── chunks/seg_*.wav          # 音频分块（默认 5s 一段，近流式粒度）
        ├── transcripts/
        │   ├── segments.jsonl       # 逐段结构化转写（实时追加）
        │   └── transcript.txt       # 纯文字稿（实时追加，最终交付物）
        ├── record.log / transcribe.log / listen.log
"近流式"= 内录按约 5s 高频落小分块，转写器秒级增量处理，观感接近实时字幕；
  该小模型(离线 int8 zipformer-ctc)转写远快于录音，无累积延迟，可稳定跑很久。

用法：
    python scripts/listen.py --room <B站直播间号> --user <使用者> [选项]
  # 没人值守后台跑：
  nohup python scripts/listen.py --room 1816490612 --user me --wait-start \
        > user/<me>/listen.log 2>&1 &

选项：
  --room ID             B站直播间号(必填)
  --user NAME           使用者标识，决定 user/<NAME>/ 归属(必填)
  --segment SEC         音频分块秒数，越小越接近流式(默认 5)
  --poll SEC            结束检测轮询间隔秒(默认 30)
  --down N              连续 N 次检测到下播即结束(默认 5，约 2.5 分钟)
  --wait-start          未开播时轮询等待，而非直接退出
  --max-wait-min M      等待开播最久 M 分钟(配合 --wait-start，默认无限)
  --max-minutes M       最多录 M 分钟后自动收尾(兜底，默认等直播自然结束)
  --plan-only           只探测并打印将建的会话目录，不实际录音/转写
说明：转写引擎自动探测装有 sherpa 的 venv python(GPU优先/CPU兜底已内置)。
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

IS_WIN = os.name == "nt"
SKILL = common.SKILL_DIR
ROOM_API = "https://api.live.bilibili.com/room/v1/Room/get_info?room_id={}"


# ---------------------------------------------------------------- 小工具
def http_json(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    # 直连，绕开系统代理可能导致的 502
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def room_info(room):
    try:
        d = (http_json(ROOM_API.format(room)) or {}).get("data") or {}
        return d
    except Exception:
        return {}


def live_status(room):
    return int(room_info(room).get("live_status", -1))  # 1 直播中 / 0 下播


def room_title(room):
    return (room_info(room).get("title") or "").strip()


def clean_seg(name):
    """清成可用作路径的单段名：去掉 Windows 非法/控制字符，压空白，截断。"""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip()
    name = re.sub(r"\s+", "_", name)
    return name[:80].strip("_.") or "untitled"


def build_session_dir(user, room):
    """返回 user/<user>/session/<时间>_<主题> 目录，并自动补主题。"""
    if not user:
        user = os.environ.get("USERNAME") or os.environ.get("USER") or "me"
    owner = clean_seg(user) or "me"
    now = datetime.datetime.now()
    title = clean_seg(room_title(room)) or f"room{room}"
    rel = os.path.join("user", owner, "session",
                       f"{now.strftime('%Y%m%d-%H%M')}_{title}")
    return os.path.join(SKILL, rel)


# ---------------------------------------------------------------- 引擎/子进程
def pick_python():
    """返回能 import sherpa_onnx 的解释器路径，找不到返回 None。"""
    cands = []
    if IS_WIN:
        cands += [os.path.join(SKILL, "win", "scripts", ".venv", "Scripts", "python.exe")]
    else:
        cands += [os.path.join(SKILL, "scripts", ".venv", "bin", "python")]
    cands += [sys.executable]
    env = os.environ.get("LCC_PYTHON")
    if env:
        cands.insert(0, env)
    seen = set()
    for py in cands:
        if not py or py in seen or not os.path.exists(py):
            continue
        seen.add(py)
        try:
            r = subprocess.run([py, "-c", "import sherpa_onnx"],
                               capture_output=True, timeout=30)
            if r.returncode == 0:
                return py
        except Exception:
            continue
    return None


def record_script():
    return (os.path.join(SKILL, "win", "scripts", "record.py") if IS_WIN
            else os.path.join(SKILL, "scripts", "record.py"))


def transcribe_script():
    return os.path.join(SKILL, "scripts", "transcribe.py")


def start_proc(args, logfile):
    os.makedirs(os.path.dirname(logfile), exist_ok=True)
    lf = open(logfile, "a", encoding="utf-8", buffering=1)
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(args, stdout=lf, stderr=subprocess.STDOUT,
                            cwd=SKILL, creationflags=flags,
                            start_new_session=(os.name != "nt")), lf


# ---------------------------------------------------------------- 主体
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--room", required=True, help="B站直播间号")
    ap.add_argument("--user", default="", help="使用者标识(user/<NAME>/)，默认取系统用户名")
    ap.add_argument("--segment", type=int, default=5, help="音频分块秒数(近流式粒度，默认5)")
    ap.add_argument("--poll", type=int, default=30, help="结束检测轮询间隔秒(默认30)")
    ap.add_argument("--down", type=int, default=5, help="连续 N 次下播判定结束(默认5)")
    ap.add_argument("--wait-start", action="store_true", help="未开播则轮询等待")
    ap.add_argument("--max-wait-min", type=int, default=0, help="等开播最久分钟(0=不限)")
    ap.add_argument("--max-minutes", type=int, default=0, help="最多录多少分钟后收尾(0=不限)")
    ap.add_argument("--plan-only", action="store_true", help="只探测并打印目录，不录音")
    a = ap.parse_args()

    seg = max(1, a.segment)
    # ---- 直播状态
    title = room_title(a.room)
    st = live_status(a.room)
    if st != 1 and not a.wait_start:
        ap.error(f"直播间 {a.room} 当前未在播(live_status={st})。"
                 f"标题: {title or '(无)'}\n加 --wait-start 可轮询等待开播。")
    if a.wait_start and st != 1:
        wait_deadline = time.time() + a.max_wait_min * 60 if a.max_wait_min > 0 else None
        print(f"[listen] 直播间 {a.room} 未在播，等待开播… (每 {a.poll}s 查一次)")
        while live_status(a.room) != 1:
            if wait_deadline and time.time() > wait_deadline:
                ap.error(f"等待 {a.max_wait_min} 分钟仍未开播，退出。")
            time.sleep(a.poll)
        print("[listen] 直播已开始。")

    session = build_session_dir(a.user, a.room)
    print(f"[listen] 直播间 {a.room} | 主题: {title or '(未取到)'}")
    print(f"[listen] 会话目录: {session}")
    print(f"[listen] 音频分块: {seg}s/段 (近流式出字)")

    if a.plan_only:
        print("[listen] --plan-only，不启动录音/转写。")
        return 0

    os.makedirs(session, exist_ok=True)
    os.makedirs(os.path.join(session, "chunks"), exist_ok=True)
    os.makedirs(os.path.join(session, "transcripts"), exist_ok=True)

    py = pick_python()
    if not py:
        print("[listen] 错误：找不到装有 sherpa-onnx 的 python，请先运行环境就绪(setup)。")
        return 1

    # 内录可用性检查
    chk = subprocess.run([py, record_script(), "check"], capture_output=True,
                         text=True, timeout=60)
    if chk.returncode != 0:
        print("[listen] 内录不可用，先解决出声设备问题：\n  "
              + (chk.stdout + chk.stderr)[-500:])
        return 1

    meta = {"room": a.room, "url": f"https://live.bilibili.com/{a.room}",
            "title": title, "user": a.user,
            "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "engine": "wasapi_loopback" if IS_WIN else "screencapturekit",
            "segment_seconds": seg, "accel_policy": "gpu->cpu"}
    with open(os.path.join(session, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # 启动内录 + 转写(watch)
    procs, logs = {}, {}
    procs["record"] = start_proc([py, record_script(), "start",
                                  "--session", session, "--segment", str(seg)],
                                 os.path.join(session, "record.log"))
    procs["transcribe"] = start_proc([py, transcribe_script(), "--session", session,
                                      "--watch"],
                                     os.path.join(session, "transcribe.log"))
    logs["record"] = procs["record"][1]; logs["transcribe"] = procs["transcribe"][1]
    print(f"[listen] 已启动 内录(pid {procs['record'][0].pid}) + 转写watch(pid {procs['transcribe'][0].pid})")
    print("[listen] 结束收尾：检测到下播(连续 {a.down} 次) 或 超时自动停；"
          f" Ctrl-C 手动收尾。")

    t_start = time.time()
    down_count = 0
    restart = {"record": 0, "transcribe": 0}
    try:
        while True:
            time.sleep(a.poll)
            # 1) 兜底超时
            if a.max_minutes > 0 and (time.time() - t_start) > a.max_minutes * 60:
                print(f"[listen] 达到 --max-minutes={a.max_minutes}，收尾。")
                break
            # 2) 直播结束检测
            st = live_status(a.room)
            if st == 0:
                down_count += 1
                print(f"[listen] 检测到未在播({down_count}/{a.down})，"
                      f"{a.down - down_count} 次后收尾。")
                if down_count >= a.down:
                    break
            else:
                down_count = 0
            # 3) 子进程看护：崩了就重启(增量幂等，不丢已落盘)
            for name in ("record", "transcribe"):
                p = procs[name][0]
                if p.poll() is not None:
                    if restart[name] >= 2:
                        print(f"[listen] {name} 进程已退出且重启超限，中止。")
                        raise SystemExit(f"[listen] {name} 多次退出，终止。")
                    restart[name] += 1
                    print(f"[listen] {name} 异常退出(code={p.returncode})，第{restart[name]}次重启…")
                    if name == "record":
                        procs[name] = start_proc(
                            [py, record_script(), "start", "--session", session,
                             "--segment", str(seg)],
                            os.path.join(session, "record.log"))
                    else:
                        procs[name] = start_proc(
                            [py, transcribe_script(), "--session", session, "--watch"],
                            os.path.join(session, "transcribe.log"))
                    logs[name].close()
                    logs[name] = procs[name][1]
    except KeyboardInterrupt:
        print("[listen] 收到 Ctrl-C，收尾。")

    # ---- 收尾
    try:
        if procs["record"][0].poll() is None:
            subprocess.run([py, record_script(), "stop", "--session", session],
                           capture_output=True, text=True, timeout=30)
    except Exception:
        pass
    try:
        open(os.path.join(session, "done.flag"), "w").write("done")
    except Exception:
        pass
    for name in ("record", "transcribe"):
        try:
            procs[name][0].wait(timeout=15)
        except Exception:
            try:
                procs[name][0].terminate()
            except Exception:
                pass
        try:
            logs[name].close()
        except Exception:
            pass

    txt = os.path.join(session, "transcripts", "transcript.txt")
    chars = 0
    if os.path.exists(txt):
        chars = len(open(txt, encoding="utf-8").read())
    dur = (time.time() - t_start) / 60
    print(f"[listen] 收尾完成。本次监听 {dur:.1f} 分钟，文字稿 {chars} 字。")
    print(f"[listen] 最终文字稿: {txt}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
