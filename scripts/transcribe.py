#!/usr/bin/env python3
"""transcribe.py — 把 session 里的音频分块转写成文字（本地 sherpa-onnx）。

用法：
  python3 transcribe.py --session DIR [--watch]

行为：
  - 一次性模式：扫描 DIR/chunks/*.wav，转写所有未处理过的分块。
  - --watch 模式：每 2 秒轮询一次新分块并转写；当出现 DIR/done.flag
    时，处理完剩余分块后自动退出（用于"直播结束"收尾）。
  - 静音分块（识别为空）记入 jsonl 但不会写进 transcript.txt。
  - 全程增量：随时可以中断，重启后会跳过已转写的分块。

输出：
  DIR/transcripts/segments.jsonl   每块一行记录（含耗时、音频时长、文本）
  DIR/transcripts/transcript.txt   纯文本全文（每次更新后重建）
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402


class SherpaEngine:
    def __init__(self):
        import sherpa_onnx
        mf = common.model_files()
        if mf is None:
            sys.exit("[transcribe] 未找到 ASR 模型。请先运行 setup.sh 下载模型：\n"
                     f"  目录: {common.models_dir()}\n"
                     f"  模型: {common.MODEL_SUBDIR}（约 400MB）")
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_zipformer_ctc(
            model=mf["model"], tokens=mf["tokens"],
            num_threads=4, sample_rate=16000,
            decoding_method="greedy_search")
        self.sample_rate = 16000

    def transcribe(self, wav_path):
        import soundfile as sf
        audio, sr = sf.read(wav_path, dtype="float32", always_2d=True)
        stream = self.recognizer.create_stream()
        stream.accept_waveform(sr, audio[:, 0])
        t0 = time.time()
        self.recognizer.decode_stream(stream)
        return {"text": stream.result.text.strip(),
                "audio_seconds": round(len(audio) / sr, 1),
                "rt_seconds": round(time.time() - t0, 2)}


def wav_files(session_dir):
    chunks = os.path.join(session_dir, "chunks")
    if not os.path.isdir(chunks):
        return []
    files = [os.path.join(chunks, f) for f in os.listdir(chunks)
             if f.endswith(".wav")]
    return sorted(files)


def done_files(jsonl):
    if not os.path.exists(jsonl):
        return set()
    done = set()
    for line in open(jsonl, encoding="utf-8"):
        try:
            done.add(os.path.basename(json.loads(line)["file"]))
        except Exception:
            pass
    return done


def rebuild_transcript(session_dir, jsonl, txt_path):
    """按 jsonl 顺序重建 transcript.txt（只收非空文本）。"""
    lines = []
    if os.path.exists(jsonl):
        for line in open(jsonl, encoding="utf-8"):
            try:
                rec = json.loads(line)
            except Exception:
                continue
            text = (rec.get("text") or "").strip()
            if text:
                lines.append(text)
    os.makedirs(os.path.dirname(txt_path), exist_ok=True)
    tmp = txt_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    os.replace(tmp, txt_path)


def run_session(session_dir, engine, watch):
    session_dir = os.path.abspath(session_dir)
    tdir = os.path.join(session_dir, "transcripts")
    os.makedirs(tdir, exist_ok=True)
    jsonl = os.path.join(tdir, "segments.jsonl")
    txt_path = os.path.join(tdir, "transcript.txt")

    if not watch and not wav_files(session_dir):
        print("[transcribe] 该会话还没有音频分块。")
        return

    while True:
        done = done_files(jsonl)
        todo = [w for w in wav_files(session_dir)
                if os.path.basename(w) not in done]
        if todo:
            with open(jsonl, "a", encoding="utf-8") as jf:
                for w in todo:
                    rec = engine.transcribe(w)
                    rec["file"] = os.path.basename(w)
                    rec["ts"] = time.strftime("%H:%M:%S")
                    jf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    jf.flush()
                    mark = "·" if rec["text"] else "(静音)"
                    print(f"[transcribe] {rec['file']} "
                          f"({rec['audio_seconds']}s/{rec['rt_seconds']}s) {mark} "
                          f"{rec['text'][:40]}", flush=True)
            rebuild_transcript(session_dir, jsonl, txt_path)
        if not watch:
            break
        if os.path.exists(os.path.join(session_dir, "done.flag")):
            print("[transcribe] 检测到 done.flag，收尾退出。")
            break
        time.sleep(2)
    print(f"[transcribe] 完成。全文见 {txt_path}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--session", required=True, help="会话目录")
    p.add_argument("--watch", action="store_true", help="轮询新模式")
    args = p.parse_args()
    engine = SherpaEngine()
    run_session(args.session, engine, args.watch)


if __name__ == "__main__":
    main()
