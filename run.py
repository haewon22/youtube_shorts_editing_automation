#!/usr/bin/env python3
"""
전체 파이프라인 — 숏츠+원본 링크 → 타임스탬프 추출 → 원본 구간 편집 → 완성

  1. find_timestamp.analyze  : 숏츠가 원본의 어디(start~end)에서 왔는지 계산
  2. 원본에서 그 구간만 잘라옴 (URL이면 그 부분만 다운로드)
  3. edit_shorts.process     : 공백제거 + 배속 + 피치 + 자막 으로 새로 편집

즉, 원본에서 '소재 구간'을 자동으로 찾아와 네 스타일(1.10x / 피치 0.91 / 자막)로
숏츠를 다시 만들어 준다.

사용법:
  python3 run.py <숏츠_URL> <원본_URL_또는_파일> -o final.mp4
  python3 run.py <숏츠> <원본> --pause-threshold 0.35 --speed 1.15 --refine-audio
"""
import os, sys, glob, argparse, tempfile, subprocess

import find_timestamp as ft
import edit_shorts as es


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def download_segment(url_or_path: str, start: float, end: float,
                     out: str, pad: float) -> str:
    """원본의 [start-pad, end+pad] 구간을 영상+음성으로 확보."""
    s = max(start - pad, 0.0)
    e = end + pad
    if ft.is_url(url_or_path):
        print(f"  원본 {ft.fmt(s)}~{ft.fmt(e)} 구간 다운로드 중...", flush=True)
        cmd = ["yt-dlp", "--no-playlist", "-f", "bv*+ba/b",
               "--merge-output-format", "mp4",
               "--download-sections", f"*{s}-{e}", "--force-keyframes-at-cuts",
               "-o", out, url_or_path]
        _run(cmd)
        if os.path.exists(out):
            return out
        g = glob.glob(out.rsplit(".", 1)[0] + ".*")
        return g[0] if g else ""
    else:
        print(f"  원본 파일에서 {ft.fmt(s)}~{ft.fmt(e)} 구간 추출 중...", flush=True)
        _run(["ffmpeg", "-y", "-v", "error", "-ss", str(s), "-to", str(e),
              "-i", url_or_path, "-c:v", "libx264", "-preset", "veryfast",
              "-crf", "20", "-c:a", "aac", out])
        return out if os.path.exists(out) else ""


def main():
    p = argparse.ArgumentParser(
        description="숏츠+원본 → 타임스탬프 추출 → 원본 구간 자동 편집")
    p.add_argument("shorts", help="숏츠 URL")
    p.add_argument("original", help="원본 URL 또는 로컬 파일")
    p.add_argument("-o", "--output", default="shorts_final.mp4", help="출력 파일")

    # 타임스탬프 단계
    p.add_argument("--pad", type=float, default=0.0,
                   help="잘라올 원본 구간 앞뒤 여유(초, 기본 0)")
    p.add_argument("--refine-audio", action="store_true",
                   help="시작/종료를 오디오 대조로 정밀 보정 후 컷")
    p.add_argument("--force-whisper", action="store_true",
                   help="자막 무시하고 Whisper로 타임스탬프 분석")

    # 편집 단계 (edit_shorts 로 전달)
    p.add_argument("--speed", type=float, default=es.DEF_SPEED)
    p.add_argument("--pitch", type=float, default=es.DEF_PITCH)
    p.add_argument("--pause-threshold", type=float, default=es.DEF_PAUSE_THRESHOLD)
    p.add_argument("--keep-pad", type=float, default=es.DEF_KEEP_PAD)
    p.add_argument("--silence-db", type=float, default=es.DEF_SILENCE_DB)
    p.add_argument("--no-subs", action="store_true")
    p.add_argument("--whisper-model", default=es.DEF_WHISPER_MODEL,
                   choices=["tiny", "base", "small", "medium",
                            "large-v3-turbo", "large-v3"])
    p.add_argument("--font-size", type=int, default=es.DEF_FONT_SIZE)
    p.add_argument("--font-color", default=es.DEF_FONT_COLOR)
    p.add_argument("--sub-margin", type=int, default=es.DEF_SUB_MARGIN_V)
    p.add_argument("--font", default="")
    args = p.parse_args()

    full = es.prefer_full_ffmpeg()          # libass 포함 ffmpeg-full 있으면 우선 사용
    if full:
        print(f"  (자막용 ffmpeg-full 사용: {full})")
    es.need("ffmpeg"); es.need("ffprobe"); es.need("yt-dlp")

    with tempfile.TemporaryDirectory() as tmp:
        # ── 1. 타임스탬프 추출 ────────────────────────────────────────────────
        print("━" * 54)
        print("  [1/3] 타임스탬프 추출")
        print("━" * 54)
        res = ft.analyze(args.shorts, args.original, tmp,
                         whisper_model=args.whisper_model,
                         force_whisper=args.force_whisper)
        if res is None:
            sys.exit("✗ 타임스탬프 추출 실패. --force-whisper 로 재시도해보세요.")

        start, end = res["start"], res["end"]

        # (옵션) 오디오 정밀 보정
        if args.refine_audio:
            ft.ensure_numpy()
            rs, re_, _ = ft.refine_endpoints(
                args.shorts, args.original, res["segments"],
                res["shorts_total_dur"], tmp)
            start, end = rs, re_

        if end <= start:
            sys.exit(f"✗ 구간이 이상합니다 (start={start:.1f}, end={end:.1f}).")
        print(f"\n  ▶ 원본 소재 구간: {ft.fmt(start)} ~ {ft.fmt(end)} "
              f"({end - start:.1f}초)")

        # ── 2. 원본 구간 확보 ────────────────────────────────────────────────
        print("\n" + "━" * 54)
        print("  [2/3] 원본 구간 가져오기")
        print("━" * 54)
        seg = download_segment(args.original, start, end,
                               os.path.join(tmp, "segment.mp4"), args.pad)
        if not seg or not os.path.exists(seg):
            sys.exit("✗ 원본 구간 확보 실패.")

        # ── 3. 편집 ──────────────────────────────────────────────────────────
        print("\n" + "━" * 54)
        print("  [3/3] 편집 (공백제거 + 배속 + 피치 + 자막)")
        print("━" * 54)
        es.process(seg, args.output, speed=args.speed, pitch=args.pitch,
                   pause_threshold=args.pause_threshold, keep_pad=args.keep_pad,
                   silence_db=args.silence_db, subs=not args.no_subs,
                   whisper_model=args.whisper_model, font_size=args.font_size,
                   font_color=args.font_color, sub_margin=args.sub_margin,
                   font=args.font)

        print("\n" + "═" * 54)
        print(f"  ✓ 완성 → {args.output}")
        print(f"    원본 {ft.fmt(start)}~{ft.fmt(end)} → "
              f"{args.speed}x / 피치 {args.pitch}"
              f"{' / 자막' if not args.no_subs else ''}")
        print("═" * 54 + "\n")


if __name__ == "__main__":
    main()
