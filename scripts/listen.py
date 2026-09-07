#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""listen.py — 持续听完整场直播/直播课并「边听边流式出字」的全自动编排。

把「认链接 → (能抓则)抓主播/主题 → 内录(近流式小分块) → 增量转写出字 →
结束检测 → 自动收尾」串成一条命令，可无人值守跑 60 分钟以上一整场。

会话落盘规范（单一可信源）：
    <skill>/user/session/<主播账号>_<直播主题>_<YYYYMMDD-HHMM>/   ← B站等能抓元数据时
    <skill>/user/session/<YYYYMMDD-HHMM>/                          ← 抓不到(抖音/小红书/视频号等)降级纯时间
        ├── meta.json                 # source url / 标题 / 主播名 / 听课人 / 开始时间 / 段长 等
        ├── chunks/seg_*.wav          # 音频分块（默认 5s 一段，近流式粒度）
        ├── transcripts/
        │   ├── segments.jsonl       # 逐段结构化转写（实时追加）
        │   └── transcript.txt       # 纯文字稿（实时追加，最终交付物）
        ├── record.log / transcribe.log / listen.log
"近流式"= 内录按约 5s 高频落小分块，转写器秒级增量处理，观感接近实时字幕；
  该小模型(离线 int8 zipformer-ctc)转写远快于录音，无累积延迟，可稳定跑很久。

输入：--url 接受任意直播链接(含抖音/小红书/视频号等无公开接口的平台)；
     --room 也可直接给 B站房间号(数字)作简写。给链接即可，脚本自己判断能否抓数据：
   - 能抓(B站等)：主播_主题_时间命名，自动轮询开播/下播
   - 抓不到(其余平台)：目录=纯本地时间，直接开始内录系统声(你需自行打开该页出声)，
     用「连续静音 --silent-timeout 分钟」自动判结束(默认3分钟，主播下播/关页→系统无声
     →超时收尾)，可再叠加 --max-minutes 定时兜底，或 Ctrl-C 手动收尾

用法：
    python scripts/listen.py --url <任意直播URL> --user <听课人> [选项]
  # B站数字简写：
    python scripts/listen.py --room 1816490612 --user me [选项]
  # 没人值守后台跑(B站，会自动等下播)：
  nohup python scripts/listen.py --url https://live.bilibili.com/1816490612 \
        --user me --wait-start > user/listen_me.log 2>&1 &

选项：
  --url URL             任意直播链接(见上说明；与 --room 二选一)
  --room ID             B站直播间号(数字，--url 的简写)
  --user NAME           听课人标识(仅记入 meta.json，不进目录名)
  --segment SEC         音频分块秒数，越小越接近流式(默认 5)
  --poll SEC            结束检测轮询间隔秒(默认 30)
  --down N              连续 N 次检测到下播即结束(默认 5，约 2.5 分钟；仅 B站)
  --wait-start          未开播时轮询等待，而非直接退出(仅 B站)
  --max-wait-min M      等待开播最久 M 分钟(配合 --wait-start，默认无限；仅 B站)
  --max-minutes M       最多录 M 分钟后自动收尾(兜底；盲录平台可叠加)
  --silent-timeout M    盲录平台连续静音 M 分钟自动收尾(默认3；0=关闭；仅盲录生效)
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
CARD_API = "https://api.bilibili.com/x/web-interface/card?mid={}"


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


def room_owner(room):
    """返回直播间房主（主播）的 B站昵称，自动抓取；失败返回空串。

    先由 get_info 拿 uid，再用 card 接口按 mid 拿昵称(name)。
    """
    try:
        uid = room_info(room).get("uid")
        if not uid:
            return ""
        d = http_json(CARD_API.format(uid)) or {}
        card = d.get("data") or {}
        return ((card.get("card") or {}).get("name") or "").strip()
    except Exception:
        return ""


def clean_seg(name):
    """清成可用作路径的单段名：去掉 Windows 非法/控制字符，压空白，截断。"""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip()
    name = re.sub(r"\s+", "_", name)
    return name[:80].strip("_.") or "untitled"


def resolve_target(raw):
    """把用户给的任意输入(直播URL 或 B站房间号)解析成统一目标 dict。

    kind=bilibili：有公开接口，能抓主播/主题、能轮询开播下播；
    kind=blind：无公开接口(抖音/小红书/视频号等 SPA 站)，抓不到元数据，
        也无法自动判开播/下播 → 目录降级为纯本地时间，手动/定时收尾。
    """
    raw = (raw or "").strip()
    t = {"raw": raw, "kind": "blind", "room": "", "url": raw,
         "owner": "", "title": ""}
    if not raw:
        return t
    # 从 URL 或裸数字里认 B站房间号（live.bilibili.com/<id> 或纯数字串）
    m = re.search(r"live\.bilibili\.com/(\d+)", raw)
    room = m.group(1) if m else (raw if raw.isdigit() else "")
    if room:
        t["kind"], t["room"] = "bilibili", room
        t["url"] = f"https://live.bilibili.com/{room}"
        # 只有 bilibili 有能力抓公开元数据
        t["owner"] = clean_seg(room_owner(room))
        t["title"] = clean_seg(room_title(room))
    return t


def session_name(t):
    """由目标 dict 生成目录名：能抓则 主播_主题_时间；抓不到则 纯本地时间。"""
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    parts = []
    if t.get("owner"):
        parts.append(t["owner"])
    if t.get("title"):
        parts.append(t["title"])
    if parts:
        return "_".join(parts + [ts])
    return ts


def build_session_dir(t):
    """返回 user/session/<名字> 目录（名字见 session_name 的降级规则）。"""
    return os.path.join(SKILL, "user", "session", session_name(t))


def latest_chunk_mtime(session):
    """返回 session/chunks/ 里最新 .wav 分块的修改时间；无分块返回 0。

    内录只在「有声音播放」时落盘分块（静音不落盘），故分块 mtime 可当作
    「最近一次听到声音」的时间戳，供盲录平台做静音超时自动收尾。
    """
    d = os.path.join(session, "chunks")
    if not os.path.isdir(d):
        return 0.0
    newest = 0.0
    for fn in os.listdir(d):
        if fn.endswith(".wav"):
            try:
                newest = max(newest, os.path.getmtime(os.path.join(d, fn)))
            except OSError:
                pass
    return newest


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
    ap.add_argument("--url", default="", help="任意直播链接(抖音/小红书/视频号/B站…)，与 --room 二选一")
    ap.add_argument("--room", default="", help="B站直播间号(数字，--url 的简写)")
    ap.add_argument("--user", default="", help="听课人标识(仅记入 meta.json 归属，不进目录名)，默认取系统用户名")
    ap.add_argument("--segment", type=int, default=5, help="音频分块秒数(近流式粒度，默认5)")
    ap.add_argument("--poll", type=int, default=30, help="结束检测轮询间隔秒(默认30)")
    ap.add_argument("--down", type=int, default=5, help="连续 N 次下播判定结束(默认5，仅B站)")
    ap.add_argument("--wait-start", action="store_true", help="未开播则轮询等待(仅B站)")
    ap.add_argument("--max-wait-min", type=int, default=0, help="等开播最久分钟(0=不限，仅B站)")
    ap.add_argument("--max-minutes", type=int, default=0, help="最多录多少分钟后收尾(0=不限；盲录平台务必设置)")
    ap.add_argument("--silent-timeout", type=int, default=3,
                    help="盲录平台连续静音多少分钟后自动收尾(分钟，默认3；0=关闭)")
    ap.add_argument("--plan-only", action="store_true", help="只探测并打印目录，不录音")
    a = ap.parse_args()

    seg = max(1, a.segment)
    if not a.url and not a.room:
        ap.error("请提供 --url <任意直播链接> 或 --room <B站房间号>。")
    raw = a.url or a.room
    t = resolve_target(raw)
    kind = t["kind"]

    # ---- 平台分流
    if kind == "bilibili":
        title, owner = t["title"], t["owner"]
        st = live_status(t["room"])
        if st != 1 and not a.wait_start:
            ap.error(f"直播间 {t['room']} 当前未在播(live_status={st})。"
                     f"标题: {title or '(无)'}\n加 --wait-start 可轮询等待开播。")
        if a.wait_start and st != 1:
            wait_deadline = time.time() + a.max_wait_min * 60 if a.max_wait_min > 0 else None
            print(f"[listen] 直播间 {t['room']} 未在播，等待开播… (每 {a.poll}s 查一次)")
            while live_status(t["room"]) != 1:
                if wait_deadline and time.time() > wait_deadline:
                    ap.error(f"等待 {a.max_wait_min} 分钟仍未开播，退出。")
                time.sleep(a.poll)
            print("[listen] 直播已开始。")
        print(f"[listen] B站直播间 {t['room']} | 主播: {owner or '(未取到)'} | 主题: {title or '(未取到)'}")
    else:
        # 无公开接口平台：抓不到元数据、无法判开播下播 → 直接录系统声
        owner = title = ""
        print("[listen] 该平台无公开接口(无法抓主播/主题/直播状态)。")
        print("[listen] 将按 <纯本地时间> 命名并开始内录『系统正在播放的声音』；")
        print("[listen] 请自行打开该直播页出声。结束判定："
              f"连续静音 {a.silent_timeout} 分钟自动收尾"
              + (f"，另叠加 --max-minutes={a.max_minutes} 定时兜底" if a.max_minutes > 0
                 else "，可再叠加 --max-minutes 定时兜底")
              + "，或 Ctrl-C 手动收尾。")

    session = build_session_dir(t)
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

    meta = {"source": raw, "url": t["url"] or raw,
            "room": t["room"], "owner": owner or "", "title": title or "",
            "user": a.user, "kind": kind,
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
    if kind == "bilibili":
        print(f"[listen] 结束收尾：检测到下播(连续 {a.down} 次) 或 超时自动停；"
              " Ctrl-C 手动收尾。")
    else:
        silent = f"，连续静音 {a.silent_timeout} 分钟自动停" if a.silent_timeout > 0 else ""
        print("[listen] 结束收尾：达到 --max-minutes 自动停"
              f"{silent} 或 Ctrl-C 手动收尾（盲录平台无法用接口判下播）。")

    t_start = time.time()
    down_count = 0
    restart = {"record": 0, "transcribe": 0}
    # 盲录平台的静音超时收尾基准：启动时若已有分块(有声音)以其为基准，否则给 silent 分钟宽限出声
    silent_on = (kind != "bilibili" and a.silent_timeout > 0)
    last_audio = latest_chunk_mtime(session) or t_start
    try:
        while True:
            time.sleep(a.poll)
            # 1) 兜底超时
            if a.max_minutes > 0 and (time.time() - t_start) > a.max_minutes * 60:
                print(f"[listen] 达到 --max-minutes={a.max_minutes}，收尾。")
                break
            # 2) 结束检测：B站走 live_status 轮询；盲录平台走静音超时
            if kind == "bilibili":
                st = live_status(t["room"])
                if st == 0:
                    down_count += 1
                    print(f"[listen] 检测到未在播({down_count}/{a.down})，"
                          f"{a.down - down_count} 次后收尾。")
                    if down_count >= a.down:
                        break
                else:
                    down_count = 0
            elif silent_on:
                m = latest_chunk_mtime(session)
                if m > last_audio:          # 有新声音分块，刷新基准
                    last_audio = m
                elif time.time() - last_audio > a.silent_timeout * 60:
                    print(f"[listen] 连续 {a.silent_timeout} 分钟无声音"
                          "(静音超时)，判定直播结束，收尾。")
                    break
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
