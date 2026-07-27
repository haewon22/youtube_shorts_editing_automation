#!/usr/bin/env python3
"""
숏츠 자동 편집기 — 네가 숏츠 제작자가 된다!

편집 파이프라인 (모두 CLI로 조정 가능, 기본값은 아래 상수):
  1. 말 사이 긴 공백 제거   (--pause-threshold 보다 긴 무음만 잘라냄)
  2. 자막 번인             (--no-subs 로 끄기)
  3. 배속 1.10x            (--speed)
  4. 피치 0.91            (--pitch, 배속과 독립적으로 음정만 조절)

동작:
  · 무음 탐지(ffmpeg silencedetect) → 긴 공백만 컷, 말 주변 여백은 유지
  · 배속+피치를 한 패스에서: asetrate(피치)+atempo(템포보정)+setpts(영상)
  · 자막은 편집이 끝난 영상을 Whisper로 인식해야 타이밍이 맞으므로 나중에 번인

사용법:
  python3 edit_shorts.py <입력영상_또는_URL> -o out.mp4
  python3 edit_shorts.py in.mp4 --pause-threshold 0.35 --speed 1.15 --pitch 0.90
  python3 edit_shorts.py in.mp4 --no-subs
"""
import os, sys, re, glob, shutil, subprocess, tempfile, argparse


# ─── 기본값 (네가 준 수치를 base로) ──────────────────────────────────────────
DEF_SPEED           = 1.10      # 배속
DEF_PITCH           = 1.00      # 피치 (1.0 = 원음 고정)
DEF_PAUSE_THRESHOLD = 0.50      # 이 길이(초)를 넘는 무음을 '긴 공백'으로 보고 제거
DEF_KEEP_PAD        = 0.15      # 컷할 때 말 앞뒤로 남겨두는 여백(초) — 너무 붙으면 어색
DEF_SILENCE_DB      = -30.0     # 이 dB 이하를 무음으로 간주 (배경음 있으면 -35~-25 조절)
DEF_WHISPER_MODEL   = "base"    # 자막 음성인식 모델

# 자막 스타일 기본값 (세로 숏츠 기준)
DEF_FONT_SIZE       = 16        # libass 폰트 크기 (PlayResY 288 기준, 크게 하려면 ↑)
DEF_FONT_COLOR      = "white"   # white / yellow / 또는 &HAABBGGRR ASS 색상코드
DEF_SUB_MARGIN_V    = 60        # 자막 아래 여백(px 상당) — 위로 올리려면 ↑
DEF_SUB_MAX_CHARS   = 18        # 자막 한 줄 최대 글자수 (넘으면 다음 컷으로 분할)
DEF_SUB_MAX_DUR     = 2.5       # 자막 한 컷 최대 길이(초)

_COLORS = {"white": "&H00FFFFFF", "yellow": "&H0000FFFF",
           "black": "&H00000000", "green": "&H0000FF00"}


# ─── 유틸 ────────────────────────────────────────────────────────────────────

def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _has_filter(name: str) -> bool:
    out = _run(["ffmpeg", "-hide_banner", "-filters"]).stdout
    return re.search(rf"\s{re.escape(name)}\s", out) is not None


# keg-only 로 링크 안 된 ffmpeg-full(자막용 libass 포함)이 있으면 우선 사용
_FULL_DIRS = ("/opt/homebrew/opt/ffmpeg-full/bin",
              "/usr/local/opt/ffmpeg-full/bin")


def prefer_full_ffmpeg() -> str | None:
    """libass 포함 ffmpeg-full 이 설치돼 있으면 PATH 앞에 붙여 우선 쓰게 한다."""
    for d in _FULL_DIRS:
        if os.path.isdir(d) and not os.environ.get("PATH", "").startswith(d):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            return d
    return None


def is_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


def need(tool: str):
    if shutil.which(tool) is None:
        sys.exit(f"✗ '{tool}' 가 설치되어 있지 않습니다. 먼저 설치해 주세요.")


def download(url: str, stem: str) -> str:
    print("  입력이 URL → yt-dlp 다운로드 중...", flush=True)
    out = stem + ".mp4"
    r = _run(["yt-dlp", "--no-playlist", "-f",
              "bv*+ba/b", "--merge-output-format", "mp4", "-o", out, url])
    if not os.path.exists(out):
        g = glob.glob(stem + ".*")
        if not g:
            sys.exit(f"✗ 다운로드 실패:\n{r.stderr}")
        out = g[0]
    return out


def probe(path: str):
    """(fps, sample_rate, duration) 반환."""
    def q(args):
        return _run(["ffprobe", "-v", "error", *args, "-of",
                     "default=noprint_wrappers=1:nokey=1", path]).stdout.strip()
    fr = q(["-select_streams", "v:0", "-show_entries", "stream=r_frame_rate"])
    try:
        num, den = fr.split("/"); fps = float(num) / float(den)
    except Exception:
        fps = 30.0
    try:
        sr = int(q(["-select_streams", "a:0", "-show_entries", "stream=sample_rate"]))
    except Exception:
        sr = 44100
    try:
        dur = float(q(["-show_entries", "format=duration"]))
    except Exception:
        dur = 0.0
    return fps, sr, dur


# ─── 1. 무음 탐지 & 유지 구간 계산 ───────────────────────────────────────────

def detect_silences(path: str, db: float, min_len: float) -> list[tuple[float, float]]:
    """min_len 보다 긴 무음 구간 [(start, end), ...] 반환."""
    r = _run(["ffmpeg", "-i", path, "-af",
              f"silencedetect=noise={db}dB:d={min_len}", "-f", "null", "-"])
    log = r.stderr
    starts = [float(m) for m in re.findall(r"silence_start:\s*([0-9.]+)", log)]
    ends   = [float(m) for m in re.findall(r"silence_end:\s*([0-9.]+)", log)]
    sil = list(zip(starts, ends))                       # 길이 불일치 시 짧은쪽 기준
    return sil


def keep_segments(silences, duration, keep_pad) -> list[tuple[float, float]]:
    """무음(긴 공백)을 잘라낸 뒤 남길 구간 목록.

    각 무음은 앞뒤로 keep_pad 만큼만 남기고 가운데를 제거 → 말이 자연스럽게 이어짐.
    """
    keeps = []
    cursor = 0.0
    for s, e in silences:
        seg_end = min(s + keep_pad, e)
        if seg_end > cursor + 0.02:
            keeps.append((cursor, seg_end))
        cursor = max(e - keep_pad, cursor)
    if duration - cursor > 0.02:
        keeps.append((cursor, duration))

    # 인접/미세 구간 정리
    merged: list[list[float]] = []
    for s, e in keeps:
        if e - s < 0.05:
            continue
        if merged and s - merged[-1][1] < 0.03:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


# ─── 2. 배속 + 피치 + 컷 을 한 패스로 ────────────────────────────────────────

def _atempo_chain(factor: float) -> str:
    """atempo 는 0.5~2.0 만 지원 → 필요하면 여러 개로 분해."""
    parts = []
    f = factor
    while f > 2.0:
        parts.append("atempo=2.0"); f /= 2.0
    while f < 0.5:
        parts.append("atempo=0.5"); f /= 0.5
    parts.append(f"atempo={f:.6f}")
    return ",".join(parts)


def edit_pass(inp, out, keeps, speed, pitch, fps, sr):
    """컷 + 배속 + 피치 를 한 번의 인코딩으로 적용."""
    sel = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in keeps)

    # 영상: 선택 프레임을 speed 배속으로 재배치
    vf = f"select='{sel}',setpts=N/({fps:.6f}*{speed})/TB"

    # 오디오: 선택 → 타임라인 리셋 → 피치(asetrate) → 원 샘플레이트 복원 → 템포 보정
    tempo = speed / pitch                                  # asetrate가 템포를 pitch배 하므로 보정
    af = (f"aselect='{sel}',asetpts=N/SR/TB,"
          f"asetrate={int(round(sr * pitch))},aresample={sr},"
          f"{_atempo_chain(tempo)}")

    cmd = ["ffmpeg", "-y", "-i", inp, "-vf", vf, "-af", af,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out]
    r = _run(cmd)
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        sys.exit(f"✗ 편집 인코딩 실패:\n{r.stderr[-1500:]}")


# ─── 3. 자막 (편집본 인식 → SRT → 번인) ──────────────────────────────────────

def ensure_faster_whisper():
    try:
        import faster_whisper  # noqa
    except ImportError:
        print("  faster-whisper 설치 중...", flush=True)
        subprocess.run([sys.executable, "-m", "pip", "install",
                        "faster-whisper", "--break-system-packages", "-q"], check=True)


def _srt_time(t: float) -> str:
    if t < 0:
        t = 0.0
    h = int(t // 3600); m = int((t % 3600) // 60); s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def extract_cut_audio(src: str, keeps, out_wav: str, sr: int = 16000) -> str:
    """공백만 제거한 자연 오디오(원본 배속·피치, 16k mono). 자막 인식 정확도용."""
    sel = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in keeps)
    _run(["ffmpeg", "-y", "-v", "error", "-i", src, "-vn",
          "-af", f"aselect='{sel}',asetpts=N/SR/TB",
          "-ar", str(sr), "-ac", "1", out_wav])
    return out_wav


def transcribe_cues(audio: str, model: str, max_chars: int, max_dur: float,
                    time_scale: float = 1.0, hint: str = ""):
    """오디오 → 짧은 자막 컷 리스트 [(start, end, text)] (time_scale 적용).

    time_scale: 인식 타임라인 → 최종 영상 타임라인 배율 (=1/speed).
      배속/피치 없는 자연 오디오로 인식한 시각을 편집본에 맞춘다.
    """
    ensure_faster_whisper()
    from faster_whisper import WhisperModel
    print(f"  Whisper({model}) 자막 인식 중...", flush=True)
    mdl = WhisperModel(model, device="cpu", compute_type="int8")

    kw = dict(language="ko", beam_size=5, word_timestamps=True, vad_filter=True)
    hint = (hint or "").strip()
    if hint:
        kw["initial_prompt"] = hint
    try:
        segs, _ = mdl.transcribe(audio, hotwords=hint or None, **kw)
    except TypeError:
        segs, _ = mdl.transcribe(audio, **kw)

    words = []
    for s in segs:
        for w in (s.words or []):
            if w.word.strip():
                words.append((w.word, w.start, w.end))   # raw (앞 공백 보존)
    if not words:
        raw_cues = [(s.start, s.end, s.text.strip()) for s in segs if s.text.strip()]
    else:
        raw_cues, cur, cs, ce = [], [], None, None
        for raw, ws, we in words:
            if cs is None:
                cs = ws
            line = ("".join(cur + [raw])).strip()
            if cur and (len(line) > max_chars or (we - cs) > max_dur):
                raw_cues.append((cs, ce, "".join(cur).strip()))
                cur, cs = [raw], ws
            else:
                cur.append(raw)
            ce = we
        if cur:
            raw_cues.append((cs, ce, "".join(cur).strip()))

    return [(s * time_scale, e * time_scale, txt) for s, e, txt in raw_cues]


def write_srt(cues, srt_path: str) -> int:
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, (s, e, txt) in enumerate(cues, 1):
            f.write(f"{i}\n{_srt_time(s)} --> {_srt_time(e)}\n{txt}\n\n")
    return len(cues)


def make_srt(video: str, srt_path: str, model: str,
             max_chars: int, max_dur: float, time_scale: float = 1.0,
             hint: str = ""):
    """오디오 인식 → SRT 파일 저장 (번인용/CLI 호환)."""
    cues = transcribe_cues(video, model, max_chars, max_dur, time_scale, hint)
    return write_srt(cues, srt_path)


def _ff_escape(path: str) -> str:
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _burn_libass(inp, srt, out, font_size, color, margin_v, font):
    col = _COLORS.get(color, color)
    style = (f"Fontsize={font_size},PrimaryColour={col},"
             f"Outline=2,Shadow=0,BorderStyle=1,Alignment=2,MarginV={margin_v}")
    if font:
        style += f",FontName={font}"
    vf = f"subtitles={_ff_escape(srt)}:force_style='{style}'"
    r = _run(["ffmpeg", "-y", "-i", inp, "-vf", vf,
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
              "-c:a", "copy", "-movflags", "+faststart", out])
    return os.path.exists(out) and os.path.getsize(out) > 0, r.stderr


def _mux_softsub(inp, srt, out):
    """번인 불가 시: 자막을 선택형 트랙으로 임베드 (플레이어에서 켜야 보임)."""
    r = _run(["ffmpeg", "-y", "-i", inp, "-i", srt, "-c", "copy",
              "-c:s", "mov_text", "-metadata:s:s:0", "language=kor",
              "-movflags", "+faststart", out])
    return os.path.exists(out) and os.path.getsize(out) > 0, r.stderr


def apply_subs(inp, srt, out, font_size, color, margin_v, font):
    """자막 적용. libass 있으면 번인, 없으면 소프트 트랙으로 폴백.

    어느 경우든 out 옆에 .srt 를 남겨 사용자가 나중에 쓸 수 있게 한다.
    """
    srt_beside = os.path.splitext(out)[0] + ".srt"
    try:
        shutil.copy(srt, srt_beside)
    except Exception:
        srt_beside = srt

    if _has_filter("subtitles"):
        ok, err = _burn_libass(inp, srt, out, font_size, color, margin_v, font)
        if ok:
            print("  자막 번인 완료 (libass)")
            return
        print(f"  ⚠ 번인 실패, 소프트 자막으로 폴백:\n{err[-500:]}")

    else:
        print("  ⚠ 이 ffmpeg 에 libass(subtitles 필터)가 없어 번인 불가.")
        print(f"     → 번인하려면: brew reinstall ffmpeg  (libass 포함 빌드 필요)")

    ok, err = _mux_softsub(inp, srt, out)
    if not ok:                                    # 소프트 자막도 실패 → 자막없이라도 저장
        print(f"  ⚠ 소프트 자막도 실패 → 자막 없이 저장:\n{err[-400:]}")
        shutil.move(inp, out)
    print(f"  자막 파일 저장: {srt_beside}  (번인 안 됐으면 이 SRT를 활용)")


# ─── 편집 API (run.py 등에서 재사용) ────────────────────────────────────────

def process(src, output, *, speed=DEF_SPEED, pitch=DEF_PITCH,
            pause_threshold=DEF_PAUSE_THRESHOLD, keep_pad=DEF_KEEP_PAD,
            silence_db=DEF_SILENCE_DB, subs=True, whisper_model=DEF_WHISPER_MODEL,
            font_size=DEF_FONT_SIZE, font_color=DEF_FONT_COLOR,
            sub_margin=DEF_SUB_MARGIN_V, font="", sub_hint="",
            sub_max_chars=DEF_SUB_MAX_CHARS, sub_max_dur=DEF_SUB_MAX_DUR):
    """로컬 영상 파일(src) → 편집(공백제거+배속+피치+자막) → output."""
    need("ffmpeg"); need("ffprobe")
    if not os.path.exists(src):
        sys.exit(f"✗ 입력을 찾을 수 없음: {src}")

    fps, sr, dur = probe(src)
    print(f"\n[분석] fps {fps:.2f} | {sr}Hz | 길이 {dur:.1f}s")

    # 1. 유지 구간
    if pause_threshold > 0:
        sil = detect_silences(src, silence_db, pause_threshold)
        keeps = keep_segments(sil, dur, keep_pad)
        kept = sum(e - s for s, e in keeps)
        print(f"[공백제거] 긴 무음 {len(sil)}곳 감지 → "
              f"{dur:.1f}s → {kept:.1f}s ({len(keeps)}개 구간)")
    else:
        keeps = [(0.0, dur)]
        print("[공백제거] 건너뜀")
    if not keeps:
        sys.exit("✗ 남는 구간이 없습니다. threshold/dB 를 조정하세요.")

    with tempfile.TemporaryDirectory() as tmp:
        # 2. 컷 + 배속 + 피치
        edited = os.path.join(tmp, "edited.mp4")
        print(f"[편집] 컷 + {speed}x 배속 + 피치 {pitch} 적용 중...")
        edit_pass(src, edited, keeps, speed, pitch, fps, sr)

        # 3. 자막 — 배속/피치 없는 자연 오디오로 인식(정확도↑) 후 타임스탬프 ÷speed
        if not subs:
            shutil.move(edited, output)
        else:
            natural = os.path.join(tmp, "natural.wav")
            extract_cut_audio(src, keeps, natural)
            srt = os.path.join(tmp, "subs.srt")
            n = make_srt(natural, srt, whisper_model, sub_max_chars, sub_max_dur,
                         time_scale=1.0 / speed, hint=sub_hint)
            if n == 0:
                print("[자막] 인식된 말이 없어 건너뜀")
                shutil.move(edited, output)
            else:
                print(f"[자막] {n}개 컷 생성 → 적용 중...")
                apply_subs(edited, srt, output, font_size, font_color, sub_margin, font)

    print(f"\n✓ 완료 → {output}")


# ─── 메인 ────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="숏츠 자동 편집기 (공백제거 / 자막 / 배속 / 피치)")
    p.add_argument("input", help="입력 영상 파일 또는 URL")
    p.add_argument("-o", "--output", default="shorts_out.mp4", help="출력 파일")

    p.add_argument("--speed", type=float, default=DEF_SPEED, help="배속 (기본 1.10)")
    p.add_argument("--pitch", type=float, default=DEF_PITCH, help="피치 (기본 0.91)")

    p.add_argument("--pause-threshold", type=float, default=DEF_PAUSE_THRESHOLD,
                   help="이 길이(초)를 넘는 무음만 제거 (기본 0.50). 0 이면 공백제거 안 함")
    p.add_argument("--keep-pad", type=float, default=DEF_KEEP_PAD,
                   help="컷할 때 말 앞뒤 남길 여백(초, 기본 0.15)")
    p.add_argument("--silence-db", type=float, default=DEF_SILENCE_DB,
                   help="무음 판정 dB (기본 -30. 배경음 있으면 -35~-25 조절)")

    p.add_argument("--no-subs", action="store_true", help="자막 끄기")
    p.add_argument("--whisper-model", default=DEF_WHISPER_MODEL,
                   choices=["tiny", "base", "small", "medium",
                            "large-v3-turbo", "large-v3"])
    p.add_argument("--font-size", type=int, default=DEF_FONT_SIZE)
    p.add_argument("--font-color", default=DEF_FONT_COLOR, help="white/yellow/... 또는 ASS코드")
    p.add_argument("--sub-margin", type=int, default=DEF_SUB_MARGIN_V,
                   help="자막 아래 여백(기본 60, 위로 올리려면 ↑)")
    p.add_argument("--font", default="", help="자막 폰트 이름 (한글 폰트 지정 필요시)")
    p.add_argument("--sub-hint", default="",
                   help="고유명사·용어 힌트 (쉼표 구분, 예: '석유,베네수엘라,트럼프')")
    p.add_argument("--sub-max-chars", type=int, default=DEF_SUB_MAX_CHARS)
    p.add_argument("--sub-max-dur", type=float, default=DEF_SUB_MAX_DUR)
    args = p.parse_args()

    prefer_full_ffmpeg()
    print("\n[설정]")
    print(f"  배속 {args.speed}x | 피치 {args.pitch} | "
          f"공백threshold {args.pause_threshold}s (여백 {args.keep_pad}s, {args.silence_db}dB)")
    print(f"  자막 {'끔' if args.no_subs else f'켬 ({args.whisper_model})'}")

    with tempfile.TemporaryDirectory() as tmp:
        src = (download(args.input, os.path.join(tmp, "src"))
               if is_url(args.input) else args.input)
        process(src, args.output, speed=args.speed, pitch=args.pitch,
                pause_threshold=args.pause_threshold, keep_pad=args.keep_pad,
                silence_db=args.silence_db, subs=not args.no_subs,
                whisper_model=args.whisper_model, font_size=args.font_size,
                font_color=args.font_color, sub_margin=args.sub_margin,
                font=args.font, sub_hint=args.sub_hint,
                sub_max_chars=args.sub_max_chars, sub_max_dur=args.sub_max_dur)


if __name__ == "__main__":
    main()
