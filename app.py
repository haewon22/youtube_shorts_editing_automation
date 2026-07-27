#!/usr/bin/env python3
"""
숏츠 편집 스튜디오 — 로컬 웹앱 백엔드

브라우저 UI(index.html)에서 단계별로 조작:
  ① URL → 타임스탬프 추출 → 원본 구간 컷 (미리보기/다운로드)
  ② 공백(무음) 감지 → 구간별 직접 선택 제거
  ③ 배속 · 피치 · 자막 조정 → 렌더 (미리보기/다운로드)
  ⚡ 한 번에: URL → 최종 편집본

실행:  python3 app.py   → http://127.0.0.1:5000
"""
import os, sys, json, uuid, subprocess

# ── 의존성 ──
try:
    from flask import Flask, request, jsonify, send_file, send_from_directory
except ImportError:
    print("Flask 설치 중...", flush=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "flask",
                    "--break-system-packages", "-q"], check=True)
    from flask import Flask, request, jsonify, send_file, send_from_directory

import find_timestamp as ft
import edit_shorts as es
from run import download_segment

es.prefer_full_ffmpeg()                     # libass 포함 ffmpeg-full 우선

ROOT = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(ROOT, "work")
os.makedirs(WORK, exist_ok=True)
PRESETS_FILE = os.path.join(ROOT, "presets.json")


def load_presets() -> dict:
    try:
        with open(PRESETS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_presets(p: dict):
    with open(PRESETS_FILE, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)

app = Flask(__name__, static_folder=None)
JOBS: dict[str, dict] = {}                  # job_id → 상태


# ─── 공통 헬퍼 ───────────────────────────────────────────────────────────────

def _complement(dur, removes):
    """전체 [0,dur]에서 remove 구간을 뺀 '유지' 구간 목록."""
    rem = sorted((max(0.0, s), min(dur, e)) for s, e in removes if e > s)
    keeps, cur = [], 0.0
    for s, e in rem:
        if s > cur + 0.02:
            keeps.append((cur, s))
        cur = max(cur, e)
    if dur - cur > 0.02:
        keeps.append((cur, dur))
    return keeps or [(0.0, dur)]


def _job_dir(job):
    d = os.path.join(WORK, job)
    os.makedirs(d, exist_ok=True)
    return d


def ensure_job(job: str) -> bool:
    """JOBS에 없으면 disk(work/<job>/meta.json)에서 복구. 서버 재시작·새로고침 후에도
    이전 작업을 이어갈 수 있게 한다."""
    if job in JOBS:
        return True
    if not isinstance(job, str) or "/" in job or "\\" in job:
        return False
    d = os.path.join(WORK, job)
    meta, cut = os.path.join(d, "meta.json"), os.path.join(d, "cut.mp4")
    if not (os.path.exists(meta) and os.path.exists(cut)):
        return False
    try:
        with open(meta, encoding="utf-8") as f:
            m = json.load(f)
    except Exception:
        return False
    JOBS[job] = {"dir": d, "cut": cut, **m}
    return True


EXTRACT_PAD = 5.0        # 앞뒤 여유분(초). 나중에 클립 경계를 이 안에서 늘릴 수 있음


def do_extract(shorts, original, whisper_model="base", force_whisper=False,
               pad=EXTRACT_PAD):
    """URL → 타임스탬프 → 원본 구간 컷(앞뒤 pad 여유 포함). 새 job 생성."""
    job = uuid.uuid4().hex[:10]
    d = _job_dir(job)
    res = ft.analyze(shorts, original, d, whisper_model=whisper_model,
                     force_whisper=force_whisper)
    if res is None:
        raise RuntimeError("타임스탬프 추출 실패 (자막/단어가 안 겹침). "
                           "force_whisper 를 켜보세요.")
    start, end = res["start"], res["end"]
    cut = os.path.join(d, "cut.mp4")
    seg = download_segment(original, start, end, cut, pad)   # [start-pad, end+pad]
    if not seg or not os.path.exists(seg):
        raise RuntimeError("원본 구간 다운로드 실패")
    fps, sr, dur = es.probe(cut)
    _make_waveform(cut, os.path.join(d, "wave.png"))
    # 감지된 클립이 여유 붙은 cut.mp4 안에서 차지하는 구간
    raw_start = max(start - pad, 0.0)
    clip_start = min(max(start - raw_start, 0.0), dur)
    clip_end = min(end - raw_start, dur)
    meta = {"shorts": shorts, "original": original, "start": start, "end": end,
            "fps": fps, "sr": sr, "cut_dur": dur,
            "clip_start": clip_start, "clip_end": clip_end}
    JOBS[job] = {"dir": d, "cut": cut, **meta}
    try:
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
    except Exception:
        pass
    return {"job": job, "start": ft.fmt(start), "end": ft.fmt(end),
            "cut_dur": dur, "clip_start": clip_start, "clip_end": clip_end,
            "media": f"/media/{job}/cut.mp4", "wave": f"/media/{job}/wave.png"}


def _make_waveform(src, out):
    """타임라인 배경용 투명 파형 PNG (실패해도 무시)."""
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", src, "-filter_complex",
             "showwavespic=s=1200x120:colors=#8b7dff,format=rgba,"
             "colorkey=black:0.15:0.0", "-frames:v", "1", out],
            capture_output=True)
    except Exception:
        pass


def do_silence(job, pause_threshold, silence_db, keep_pad):
    st = JOBS[job]
    sils = es.detect_silences(st["cut"], silence_db, pause_threshold)
    dur = st["cut_dur"]
    # 각 무음을 keep_pad 남기고 잘라낼 '제거 구간'으로 변환
    removes = []
    for s, e in sils:
        rs, re_ = s + keep_pad, e - keep_pad
        if re_ - rs > 0.05:
            removes.append({"start": round(rs, 2), "end": round(re_, 2),
                            "dur": round(re_ - rs, 2)})
    st["silences"] = removes
    total = sum(r["dur"] for r in removes)
    return {"silences": removes, "total_remove": round(total, 2),
            "cut_dur": round(dur, 2), "expected": round(dur - total, 2)}


def do_transcribe(job, whisper_model):
    """컷(cut.mp4) 전체를 한 번만 인식 → cue 리스트(cut.mp4 타임라인, time_scale=1)."""
    st = JOBS[job]
    cues = es.transcribe_cues(st["cut"], whisper_model, es.DEF_SUB_MAX_CHARS,
                              es.DEF_SUB_MAX_DUR, time_scale=1.0)
    st["cues"] = cues
    return {"cues": [{"start": round(s, 2), "end": round(e, 2), "text": t}
                     for s, e, t in cues]}


def do_render(job, speed, pitch, removes, out_name="final.mp4"):
    """최종 영상만 렌더 (컷 + 배속/피치). 자막은 클라이언트가 SRT로 내보냄."""
    if out_name not in ("final.mp4",):                 # 경로 조작 방지
        out_name = "final.mp4"
    st = JOBS[job]
    keeps = _complement(st["cut_dur"], [(r["start"], r["end"]) for r in removes])
    d = st["dir"]
    edited = os.path.join(d, "_edited.mp4")
    es.edit_pass(st["cut"], edited, keeps, speed, pitch, st["fps"], st["sr"])
    out = os.path.join(d, out_name)
    if os.path.exists(out):
        os.remove(out)
    os.replace(edited, out)
    _, _, fdur = es.probe(out)
    return {"media": f"/media/{job}/{out_name}", "final_dur": round(fdur, 2),
            "out": out_name}


# ─── 라우트 ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(ROOT, "index.html")


@app.post("/api/extract")
def api_extract():
    b = request.get_json(force=True)
    try:
        return jsonify(do_extract(
            b["shorts"].strip(), b["original"].strip(),
            whisper_model=b.get("whisper_model", "base"),
            force_whisper=bool(b.get("force_whisper", False)),
            pad=float(b.get("pad", EXTRACT_PAD))))
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.get("/api/job/<job>")
def api_job(job):
    if not ensure_job(job):
        return jsonify({"error": "만료된 작업"}), 404
    st = JOBS[job]
    return jsonify({"cut_dur": st["cut_dur"], "clip_start": st["clip_start"],
                    "clip_end": st["clip_end"], "start": ft.fmt(st["start"]),
                    "end": ft.fmt(st["end"]),
                    "media": f"/media/{job}/cut.mp4",
                    "wave": f"/media/{job}/wave.png"})


@app.post("/api/silence")
def api_silence():
    b = request.get_json(force=True)
    job = b["job"]
    if not ensure_job(job):
        return jsonify({"error": "세션 없음 — ①부터 다시"}), 400
    try:
        return jsonify(do_silence(job, float(b.get("pause_threshold", 0.5)),
                                  float(b.get("silence_db", -30)),
                                  float(b.get("keep_pad", 0.15))))
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.post("/api/transcribe")
def api_transcribe():
    b = request.get_json(force=True)
    job = b["job"]
    if not ensure_job(job):
        return jsonify({"error": "세션 없음 — ①부터 다시"}), 400
    try:
        return jsonify(do_transcribe(job, b.get("whisper_model", "base")))
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.post("/api/render")
def api_render():
    b = request.get_json(force=True)
    job = b["job"]
    if not ensure_job(job):
        return jsonify({"error": "세션 없음 — ①부터 다시"}), 400
    try:
        return jsonify(do_render(job, float(b.get("speed", 1.10)),
                                 float(b.get("pitch", 1.0)), b.get("removes", [])))
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.post("/api/oneshot")
def api_oneshot():
    """URL → 추출 → 자동 공백제거 → (자막 인식) → 최종 영상 렌더. 편집기에도 채움."""
    b = request.get_json(force=True)
    try:
        ex = do_extract(b["shorts"].strip(), b["original"].strip(),
                        whisper_model=b.get("whisper_model", "base"),
                        force_whisper=bool(b.get("force_whisper", False)),
                        pad=float(b.get("pad", EXTRACT_PAD)))
        job = ex["job"]
        sil = do_silence(job, float(b.get("pause_threshold", 0.5)),
                         float(b.get("silence_db", -30)),
                         float(b.get("keep_pad", 0.15)))
        removes = JOBS[job].get("silences", [])
        st = JOBS[job]
        clip_removes = []                              # 여유분(클립 밖)도 잘라냄
        if st["clip_start"] > 0.05:
            clip_removes.append({"start": 0.0, "end": st["clip_start"]})
        if st["clip_end"] < st["cut_dur"] - 0.05:
            clip_removes.append({"start": st["clip_end"], "end": st["cut_dur"]})
        rn = do_render(job, float(b.get("speed", 1.10)),
                       float(b.get("pitch", 1.0)), removes + clip_removes)
        out = {**ex, **rn, "removes": removes,
               "removed": len(removes), "removed_sec": sil["total_remove"]}
        if bool(b.get("subs", True)):
            out.update(do_transcribe(job, b.get("whisper_model", "base")))
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.get("/api/presets")
def api_presets_list():
    return jsonify(load_presets())


@app.post("/api/presets")
def api_presets_save():
    b = request.get_json(force=True)
    name = (b.get("name") or "").strip()
    if not name:
        return jsonify({"error": "세트 이름을 입력하세요"}), 400
    p = load_presets()
    p[name] = {"shorts": (b.get("shorts") or "").strip(),
               "original": (b.get("original") or "").strip()}
    save_presets(p)
    return jsonify(p)


@app.post("/api/presets/delete")
def api_presets_delete():
    b = request.get_json(force=True)
    p = load_presets()
    p.pop((b.get("name") or "").strip(), None)
    save_presets(p)
    return jsonify(p)


@app.route("/media/<job>/<path:name>")
def media(job, name):
    if not ensure_job(job):
        return "no job", 404
    return send_file(os.path.join(JOBS[job]["dir"], name), conditional=True)


@app.route("/download/<job>/<path:name>")
def download(job, name):
    if not ensure_job(job):
        return "no job", 404
    path = os.path.join(JOBS[job]["dir"], name)
    return send_file(path, as_attachment=True, download_name=name)


if __name__ == "__main__":
    print("\n  숏츠 편집 스튜디오 → http://127.0.0.1:5000\n")
    app.run(host="127.0.0.1", port=5000, threaded=True, debug=False)
