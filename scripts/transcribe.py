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
# GPU 加速辅助（跨平台安全：Windows 上有 CUDA 就注入，否则无害）：
#   cuda_rt   —— 定位/注入 CUDA 运行库目录
#   accel     —— 加速档位决策（gpu/cpu）
import cuda_rt  # noqa: E402
import accel  # noqa: E402


# 转写执行策略：GPU优先(CUDA) → 兜底CPU → 可选第三方API（accel.detect 自动决策）。
# 可用 LCC_ASR_PROVIDER 环境变量手动指定，例如 cuda / cpu / directml / coreml。


class SherpaEngine:
    def __init__(self):
        # GPU 优先 / CPU 兜底：先注入 CUDA 运行库（Windows 上探测到就用，未探测到无害），
        # 再把 provider 顺序定成 cuda→cpu。GPU 不可用自动降级，绝不中断。
        rt = cuda_rt.find_cuda_rt_dir()
        if rt:
            cuda_rt.inject_cuda_rt(rt)
        import sherpa_onnx
        mf = common.model_files()
        if mf is None:
            sys.exit("[transcribe] 未找到 ASR 模型。请先运行 setup.sh 下载模型：\n"
                     f"  目录: {common.models_dir()}\n"
                     f"  模型: {common.MODEL_SUBDIR}（约 400MB）")
        # provider 优先级：LCC_ASR_PROVIDER(手动) > 本机加速档位决定 > cuda > cpu
        forced = os.environ.get("LCC_ASR_PROVIDER", "").strip().lower()
        if forced:
            order = [forced]
        elif accel.detect()["accel"] == "gpu":
            order = ["cuda", "cpu"]
        else:
            order = ["cpu"]  # 无 CUDA 运行库时直接 CPU，省去一次注定失败的 cuda 尝试
        self.recognizer = None
        self.provider = None
        self._init_with_fallback(sherpa_onnx, mf, order)
        self.sample_rate = 16000

    def warmup(self):
        """GPU 冷启动预热：首次真分块常需 ~40s 编译 cuDNN kernel，这里提前做一次。
        用一小段静音波形跑一次 decode，规避真实会话首个分块的卡顿。非 GPU 档位跳过。"""
        if self.provider != "cuda":
            return
        try:
            import numpy as np
            audio = np.zeros(1600, dtype="float32")  # 0.1s 静音，足够触发引擎就绪
            s = self.recognizer.create_stream()
            s.accept_waveform(16000, audio)
            self.recognizer.decode_stream(s)
            sys.stderr.write("[transcribe] GPU 预热完成（首个分块将无需冷启动）\n")
        except Exception as e:
            sys.stderr.write(f"[transcribe] GPU 预热失败（不影响使用）：{e}\n")

    def _init_with_fallback(self, sherpa_onnx, mf, order):
        """按序尝试 provider，失败自动降级，绝不因 GPU 缺失而中断。"""
        tried = []
        for prov in order:
            if not prov:
                continue
            tried.append(prov)
            try:
                self.recognizer = sherpa_onnx.OfflineRecognizer.from_zipformer_ctc(
                    model=mf["model"], tokens=mf["tokens"],
                    num_threads=4, sample_rate=16000,
                    decoding_method="greedy_search", provider=prov)
                self.provider = prov
                import sys as _s
                tag = "GPU" if prov == "cuda" else "CPU"
                _s.stderr.write(f"[transcribe] ASR 执行器: {prov}（{tag}）\n")
                return
            except Exception as e:
                import sys as _s
                _s.stderr.write(f"[transcribe] provider={prov} 不可用({e})，尝试下一档\n")
        sys.exit(f"[transcribe] 无法初始化 ASR（已试 {tried}）")

    def current_provider(self):
        return self.provider

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
    p.add_argument("--no-warmup", action="store_true",
                   help="跳过 GPU 冷启动预热（默认 GPU 档位会预热）")
    args = p.parse_args()
    engine = SherpaEngine()
    if not args.no_warmup:
        engine.warmup()  # GPU 档位才实际执行
    run_session(args.session, engine, args.watch)


if __name__ == "__main__":
    main()
