#!/usr/bin/env python3
"""
YouTube Shorts 타임스탬프 찾기
 
알고리즘: n-gram 대응점 → 로버스트 선형 피팅
  공통 n-gram으로 (숏츠시각 s, 원본시각 o) 대응점(anchor)들을 만든다.
  한 클립 안에서는  o = C + speed × s  (직선)이 성립한다.
    - speed : 배속 (연속값으로 추정 — 1.1x, 1.35x 등도 정확히 잡음)
    - C     : 숏츠 t=0 이 원본의 어디에 해당하는지 (오프셋)
  RANSAC 방식으로 가장 많은 대응점을 지나는 직선을 찾아 최소제곱으로 정밀화하고,
  그 인라이어를 떼어낸 뒤 반복 → 컷편집된 각 클립을 하나씩 분리한다.
  클립마다 배속이 달라도 되고, 중간이 잘려나가도 각 구간이 독립적으로 잡힌다.

  이전 방식(이산 배속 후보 + 오프셋 버킷 투표)의 한계를 해결:
    · 배속이 후보값(1.0/1.5/2.0…)과 다르면 오프셋이 드리프트해 부정확
    · 모든 클립에 단일 배속을 강제 → 컷별 배속 변화 처리 불가
    · 끝점을 shorts_dur×speed로 단순 계산 → 컷으로 빠진 구간 반영 못 함

  ※ 오디오 보정은 제거 (BGM 추가된 Shorts에서 NCC가 잘못된 피크를 찾는 문제)

사용법:
  python3 find_timestamp.py <shorts_url> <original_url_or_path>
  python3 find_timestamp.py <shorts_url> <original_url_or_path> --whisper-model small
"""
import os, sys, re, json, glob, subprocess, tempfile, argparse


SAMPLE_RATE       = 16000
N_GRAM            = 3       # 대응점 생성용 n-gram 크기 (부족하면 2로 자동 완화)
BUCKET_SEC        = 0.5     # 거친 탐색 단계의 오프셋 버킷 크기 (초)
INLIER_TOL        = 1.0     # 직선에서 이 이내(초)면 같은 클립 대응점으로 인정
                            #   (더 키우면 배속 다른 인접 클립이 잘못된 속도로 병합됨)
CONT_TOL          = 3.0     # 인접 클립 경계에서 두 직선의 원본시각 차가 이 이하면 '연속'으로
                            #   보고 병합, 크면 '컷'으로 분리 (RMS 평균은 점 많은 쪽에 희석돼서 X)
MIN_INLIERS       = 3       # 클립으로 인정하는 최소 대응점 수
                            #   (짧은 컷 조각도 잡히도록 낮게. 직선은 2점이면 결정)
MAX_CLIPS         = 8       # 최대 클립(컷) 개수
MIN_DENSITY       = 0.4     # 초당 앵커 수가 이 미만이면 노이즈로 제거
                            #   (긴/짧은 클립을 raw 표수로 비교하지 않고 '촘촘함'으로 판단.
                            #    긴 메인 클립에 밀려 짧은 컷 조각이 버려지는 문제 해결)
GAP_MIN           = 3.0     # 숏츠에서 이 이상(초) 안 덮인 구간이 있으면 그 구간만 재탐색
COVER_GAP         = 2.0     # 숏츠 앞/뒤 이 이상(초)이 매칭 안 되면 외삽 경고
SPEED_MIN         = 0.4     # 배속 탐색 하한
SPEED_MAX         = 3.5     # 배속 탐색 상한
SPEED_GRID_STEP   = 0.05    # 거친 배속 탐색 간격 (이후 회귀로 연속값 정밀화)

# ── 오디오 정밀 보정 (--refine-audio) ──
REFINE_SNIP       = 4.0     # 경계에서 사용할 오디오 스니펫 길이 (숏츠 기준, 초)
REFINE_MARGIN     = 8.0     # 예측 지점 주변 탐색 여유 (초)
REFINE_FPS        = 100     # 에너지 엔벨로프 프레임레이트 (Hz)
REFINE_MIN_CORR   = 0.30    # 이 상관계수 미만이면 보정 신뢰 못 함 → 외삽값 유지
REFINE_MAX_SHIFT  = 5.0     # 텍스트 예측과 이 이상(초) 벌어지면 오보정으로 보고 무시


# ─── 자막 다운로드 ────────────────────────────────────────────────────────────

def download_captions(url: str, stem: str) -> str | None:
    cmd = [
        "yt-dlp", "--no-playlist",
        "--write-auto-subs", "--sub-langs", "ko,ko-KR,en",
        "--sub-format", "json3/vtt", "--skip-download",
        "-o", stem, url,
    ]
    subprocess.run(cmd, capture_output=True)
    files = glob.glob(f"{stem}.*.json3") + glob.glob(f"{stem}.*.vtt")
    if not files:
        return None
    # ko 우선, en 그 다음 (알파벳 정렬하면 en이 ko보다 먼저라 언어 불일치 발생)
    def _lang_pref(f: str) -> int:
        if ".ko" in f: return 0
        if ".en" in f: return 2
        return 1
    return sorted(files, key=_lang_pref)[0]


# ─── 자막 파싱 ────────────────────────────────────────────────────────────────

def parse_json3(path: str) -> list[dict]:
    """json3 → [{text, start, end}]. 단어 단위 tOffsetMs가 있으면 활용."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    segs = []
    for ev in data.get("events", []):
        t0  = ev.get("tStartMs", 0) / 1000.0
        dur = ev.get("dDurationMs", 0) / 1000.0
        raw = ev.get("segs", [])
        if not raw:
            continue
        has_word_ts = any("tOffsetMs" in s for s in raw)
        if has_word_ts:
            for seg in raw:
                word = seg.get("utf8", "").strip()
                if not word:
                    continue
                ws = t0 + seg.get("tOffsetMs", 0) / 1000.0
                segs.append({"text": word, "start": ws, "end": ws + 0.4})
        else:
            text = "".join(s.get("utf8", "") for s in raw).strip()
            if text:
                segs.append({"text": text, "start": t0, "end": t0 + dur})
    segs.sort(key=lambda x: x["start"])
    return segs


def _vtt2sec(ts: str) -> float:
    parts = ts.replace(",", ".").split(":")
    return (int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            if len(parts) == 3 else int(parts[0]) * 60 + float(parts[1]))


def parse_vtt(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        content = f.read()
    segs = []
    for m in re.finditer(
        r"(\d[\d:.,]+)\s*-->\s*(\d[\d:.,]+)[^\n]*\n((?:(?!-->)[^\n]+\n?)*)",
        content
    ):
        text = re.sub(r"<[^>]+>|\[.*?\]|&\w+;", " ", m.group(3)).strip()
        if text:
            segs.append({"text": text, "start": _vtt2sec(m.group(1)),
                         "end": _vtt2sec(m.group(2))})
    return segs


def parse_caption_file(path: str) -> list[dict]:
    return parse_json3(path) if path.endswith(".json3") else parse_vtt(path)


# ─── 텍스트 정규화 & 단어 리스트 ─────────────────────────────────────────────

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ",
                  re.sub(r"[^\w가-힣a-z0-9]", " ", text.lower())).strip()


def segs_to_wordlist(segs: list[dict]) -> list[tuple[str, float]]:
    """자막 세그먼트 → [(단어, 타임스탬프)] — 단어 타임스탬프는 세그먼트 내 균등 분배."""
    result = []
    for seg in segs:
        words = _norm(seg["text"]).split()
        n = len(words)
        if not n:
            continue
        dur = max(seg["end"] - seg["start"], 0.01)
        for i, w in enumerate(words):
            result.append((w, seg["start"] + (i / n) * dur))
    return result


# ─── 대응점 생성 & 로버스트 선형 피팅 ───────────────────────────────────────

def build_anchors(
    shorts_wl: list[tuple[str, float]],
    orig_wl:   list[tuple[str, float]],
    n: int,
) -> list[tuple[float, float]]:
    """공통 n-gram → 대응점 [(shorts_t, orig_t)].

    같은 n-gram이 원본에 여러 번 나오면 모든 후보를 대응점으로 넣는다.
    (틀린 대응은 이후 로버스트 피팅에서 아웃라이어로 걸러짐)
    """
    sw = [w for w, _ in shorts_wl]; st = [t for _, t in shorts_wl]
    ow = [w for w, _ in orig_wl];   ot = [t for _, t in orig_wl]

    orig_ng: dict[tuple, list[float]] = {}
    for j in range(len(ow) - n + 1):
        orig_ng.setdefault(tuple(ow[j:j + n]), []).append(ot[j])

    anchors: list[tuple[float, float]] = []
    for i in range(len(sw) - n + 1):
        for o_t in orig_ng.get(tuple(sw[i:i + n]), ()):
            anchors.append((st[i], o_t))
    anchors.sort()
    return anchors


def _regress(pts: list[tuple[float, float]]) -> tuple[float, float]:
    """최소제곱 직선  orig = C + speed × shorts  →  (speed, C)."""
    n = len(pts)
    Ss  = sum(s for s, _ in pts)
    So  = sum(o for _, o in pts)
    Sss = sum(s * s for s, _ in pts)
    Sso = sum(s * o for s, o in pts)
    denom = n * Sss - Ss * Ss
    speed = (n * Sso - Ss * So) / denom if denom else 1.0
    speed = min(max(speed, SPEED_MIN), SPEED_MAX)
    C = (So - speed * Ss) / n
    return speed, C


def _coarse_line(anchors: list[tuple[float, float]]) -> tuple[int, float, float]:
    """거친 그리드 탐색: 최다 대응점을 지나는 (speed, C) 근사치.

    배속을 SPEED_GRID_STEP 간격으로 훑으며 오프셋 adj = o - speed·s 를 버킷에 넣되,
    각 버킷의 표수를 ±INLIER_TOL 이웃까지 합산해서(윈도우) 센다. 이렇게 하면
    타임스탬프 흔들림으로 앵커가 버킷 경계에 흩어져도 하나의 클러스터로 잡힌다.
    반환: (윈도우 표수, speed, C=윈도우 내 adj 평균).
    """
    win = max(int(round(INLIER_TOL / BUCKET_SEC)), 1)
    best = (0, 1.0, 0.0)
    speed = SPEED_MIN
    while speed <= SPEED_MAX + 1e-9:
        buckets: dict[int, list[float]] = {}
        for s, o in anchors:
            adj = o - speed * s
            buckets.setdefault(round(adj / BUCKET_SEC), []).append(adj)
        for b in buckets:
            acc: list[float] = []
            for k in range(b - win, b + win + 1):
                if k in buckets:
                    acc.extend(buckets[k])
            if len(acc) > best[0]:
                best = (len(acc), speed, sum(acc) / len(acc))
        speed += SPEED_GRID_STEP
    return best


def _clip_from_pts(pts: list[tuple[float, float]]) -> dict:
    speed, C = _regress(pts)
    return {
        "speed":        speed,
        "C":            C,
        "shorts_start": min(s for s, _ in pts),
        "shorts_end":   max(s for s, _ in pts),
        "votes":        len(pts),
        "pts":          pts,
    }


def _peel(anchors: list[tuple[float, float]]) -> list[dict]:
    """RANSAC 반복: 최다 대응점 직선을 찾아 정밀화 → 인라이어 제거 → 반복."""
    remaining = list(anchors)
    clips: list[dict] = []
    for _ in range(MAX_CLIPS):
        votes, speed, C = _coarse_line(remaining)
        if votes < MIN_INLIERS:
            break
        inliers: list[tuple[float, float]] = []
        for _ in range(3):                     # 거친 직선 주변 인라이어 → 회귀 수렴
            cur = [(s, o) for s, o in remaining
                   if abs(o - (C + speed * s)) <= INLIER_TOL]
            if len(cur) < MIN_INLIERS:
                break
            inliers = cur
            speed, C = _regress(inliers)
        if len(inliers) < MIN_INLIERS:
            break
        clips.append(_clip_from_pts(inliers))
        remaining = [(s, o) for s, o in remaining
                     if abs(o - (C + speed * s)) > INLIER_TOL]
        if len(remaining) < MIN_INLIERS:
            break
    return clips


def _dedup_and_merge(clips: list[dict]) -> list[dict]:
    """과분할된 클립을 물리적 제약으로 정리한다.

    1) Shorts 시간이 겹치는 클립은 진짜 컷일 수 없다(한 순간=원본 한 지점).
       → 표수 많은 쪽만 남긴다.
    2) Shorts 순서상 인접한 두 클립이 경계에서 원본 시각이 이어지면(=한 직선의
       연장) 컷이 아니라 한 클립이므로 합친다. 진짜 컷은 경계에서 원본 시각이
       불연속으로 점프한다. 두 직선을 경계 시각에 각각 대입해 그 차이로 판단
       (RMS 평균은 점 많은 클립에 희석돼 짧은 조각을 잘못 흡수하므로 쓰지 않음).
    """
    # 1) 겹침 제거 (표 많은 순서로 채택)
    kept: list[dict] = []
    for c in sorted(clips, key=lambda x: x["votes"], reverse=True):
        conflict = False
        for k in kept:
            ov = (min(c["shorts_end"], k["shorts_end"])
                  - max(c["shorts_start"], k["shorts_start"]))
            span = min(c["shorts_end"] - c["shorts_start"],
                       k["shorts_end"] - k["shorts_start"])
            if ov > max(1.0, 0.3 * span):
                conflict = True
                break
        if not conflict:
            kept.append(c)

    # 2) 인접 클립 연속성 병합 (경계 불연속 검사)
    kept.sort(key=lambda c: c["shorts_start"])
    out: list[dict] = [kept[0]]
    for c in kept[1:]:
        a = out[-1]
        bnd = (a["shorts_end"] + c["shorts_start"]) / 2   # 두 클립 사이 경계 시각
        pa  = a["C"] + a["speed"] * bnd                   # A 직선의 경계 원본시각
        pb  = c["C"] + c["speed"] * bnd                   # B 직선의 경계 원본시각
        if abs(pa - pb) <= CONT_TOL:
            out[-1] = _clip_from_pts(a["pts"] + c["pts"])  # 이어짐 → 병합
        else:
            out.append(c)                                  # 점프 → 진짜 컷
    return out


def find_clips(
    shorts_wl: list[tuple[str, float]],
    orig_wl:   list[tuple[str, float]],
    shorts_total_dur: float,
    debug: bool = False,
) -> list[dict]:
    """RANSAC 방식으로 클립(컷)을 하나씩 분리한다.

    한 클립 안에서는 orig = C + speed·shorts 직선이 성립.
    가장 많은 대응점을 지나는 직선을 찾아 최소제곱으로 정밀화 → 인라이어 제거 →
    반복. 클립마다 배속이 달라도 되고, 컷으로 빠진 구간도 자연히 분리된다.
    """
    if not shorts_wl or not orig_wl:
        return []

    # 3-gram(정밀) + 2-gram(재현율)을 합쳐 대응점 밀도를 높인다.
    # 짧은 Shorts(단어 몇십 개)는 3-gram만으로는 컷 조각 상당수를 놓친다.
    # 자막 ASR이 원본과 미세하게 달라 3단어 연속 일치가 자주 깨지기 때문.
    # 노이즈가 늘어도 RANSAC 직선 피팅이 걸러내므로 재현율을 우선한다.
    anchors = sorted(set(build_anchors(shorts_wl, orig_wl, N_GRAM)
                         + build_anchors(shorts_wl, orig_wl, 2)))
    if len(anchors) < MIN_INLIERS:                     # 그래도 부족하면 1-gram까지
        anchors = sorted(set(anchors + build_anchors(shorts_wl, orig_wl, 1)))
    if len(anchors) < MIN_INLIERS:
        return []

    def _dbg(tag, cs):
        if not debug:
            return
        print(f"  [find_clips/{tag}] {len(cs)}개")
        for c in sorted(cs, key=lambda x: x["shorts_start"]):
            d = c["votes"] / max(c["shorts_end"] - c["shorts_start"], 1.0)
            print(f"      shorts {fmt(c['shorts_start'])}~{fmt(c['shorts_end'])}"
                  f"  원본 {fmt(c['C'] + c['speed']*c['shorts_start'])}"
                  f"~{fmt(c['C'] + c['speed']*c['shorts_end'])}"
                  f"  배속 {c['speed']:.2f}  표 {c['votes']}  밀도 {d:.2f}")

    def _dense(c):
        return c["votes"] / max(c["shorts_end"] - c["shorts_start"], 1.0)

    def _process(anch: list[tuple[float, float]]) -> list[dict]:
        """앵커 → 클립: peel → 겹침·연속성 정리 → 밀도 필터."""
        cs = _peel(anch)
        if not cs:
            return []
        cs = _dedup_and_merge(cs)
        return [c for c in cs if c["votes"] >= MIN_INLIERS and _dense(c) >= MIN_DENSITY]

    # 1차: 전체 앵커로 주요 클립(대개 가장 긴 메인)을 잡는다.
    clips = _process(anchors)
    if not clips:
        return []
    _dbg("1차", clips)

    # 빈 구간(shorts) 한정 재탐색:
    #   메인이 숏츠의 상당 부분을 덮으면, 아직 안 덮인 숏츠 구간(앞/뒤/사이)의
    #   앵커만 떼어 재탐색한다. 탐색을 숏츠 시간축으로 가두면 '숏츠 전체를
    #   가로지르는 넓은 대각선' 가짜 선이 안 생겨서, 짧은 컷 조각이 촘촘한
    #   클립으로 깨끗이 분리된다. (넓은 대각선이 메인과 겹쳐 몰살되던 문제 해결)
    for _ in range(MAX_CLIPS):
        clips.sort(key=lambda c: c["shorts_start"])
        gaps: list[tuple[float, float]] = []
        prev = 0.0
        for c in clips:
            if c["shorts_start"] - prev > GAP_MIN:
                gaps.append((prev, c["shorts_start"]))
            prev = max(prev, c["shorts_end"])
        if shorts_total_dur - prev > GAP_MIN:
            gaps.append((prev, shorts_total_dur))
        if not gaps:
            break

        # 이미 클립에 속한 앵커는 제외 (진짜 컷 틈에서 클립 가장자리 앵커로
        # 가짜 '다리'가 생겨 두 클립을 잇는 것을 방지)
        claimed = {p for c in clips for p in c["pts"]}
        added = False
        for a, b in gaps:
            sub = [(s, o) for s, o in anchors
                   if a - 0.5 <= s <= b + 0.5 and (s, o) not in claimed]
            if len(sub) < MIN_INLIERS:
                continue
            for f in _process(sub):
                # 이미 있는 클립과 shorts 시간이 겹치면 스킵 (한 순간=원본 한 지점)
                if any(min(f["shorts_end"], c["shorts_end"])
                       - max(f["shorts_start"], c["shorts_start"]) > 1.0
                       for c in clips):
                    continue
                clips.append(f)
                added = True
        if not added:
            break
    _dbg("빈구간채움", clips)

    clips = _dedup_and_merge(clips)
    clips = [c for c in clips if c["votes"] >= MIN_INLIERS and _dense(c) >= MIN_DENSITY]
    _dbg("정리", clips)
    if not clips:
        return []
    best_votes = max(c["votes"] for c in clips)

    # 파생 필드 채우기 (매칭 텍스트가 실제로 덮은 원본 구간)
    for c in clips:
        c["orig_start"] = c["C"] + c["speed"] * c["shorts_start"]
        c["orig_end"]   = c["C"] + c["speed"] * c["shorts_end"]
        c["confidence"] = c["votes"] / best_votes
        c.pop("pts", None)

    # Shorts 구성 순서(숏츠 시간 기준)로 정렬
    clips.sort(key=lambda c: c["shorts_start"])
    return clips


# ─── Whisper 폴백 ─────────────────────────────────────────────────────────────

def ensure_faster_whisper() -> None:
    try:
        import faster_whisper  # noqa
    except ImportError:
        print("  faster-whisper 설치 중...", flush=True)
        subprocess.run(
            [sys.executable, "-m", "pip", "install",
             "faster-whisper", "--break-system-packages", "-q"],
            check=True,
        )


def transcribe(audio_path: str, model_size: str = "base") -> list[dict]:
    ensure_faster_whisper()
    from faster_whisper import WhisperModel
    print(f"  Whisper({model_size}) 로딩... (처음엔 다운로드 발생)", flush=True)
    mdl = WhisperModel(model_size, device="cpu", compute_type="int8")
    segs, _ = mdl.transcribe(audio_path, language="ko", beam_size=3,
                              word_timestamps=True)
    result = []
    for s in segs:
        result.append({"text": s.text, "start": s.start, "end": s.end})
    return result


def download_audio(url: str, stem: str) -> str:
    cmd = [
        "yt-dlp", "--no-playlist",
        "-x", "--audio-format", "wav",
        "--postprocessor-args", f"ffmpeg:-ar {SAMPLE_RATE} -ac 1",
        "-o", stem + ".%(ext)s", url,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"오디오 다운로드 실패:\n{r.stderr}")
    wav = stem + ".wav"
    return wav if os.path.exists(wav) else (glob.glob(stem + ".*") or [wav])[0]


# ─── 오디오 정밀 경계 보정 ──────────────────────────────────────────────────
#
# 텍스트 매칭은 말소리가 있는 곳까지만 앵커가 있어 숏츠 양끝은 "외삽"이 된다.
# 배속의 미세 오차·정수절삭된 길이 때문에 끝점이 1~2초 어긋날 수 있다.
# 예측 지점 주변 좁은 창에서 오디오 에너지 엔벨로프를 상호상관해 초 단위로 맞춘다.
# (엔벨로프는 피치 불변 → 배속 리샘플/BGM에 강인, 좁은 창 → 오탐 위험 낮음)

def ensure_numpy():
    try:
        import numpy  # noqa
    except ImportError:
        print("  numpy 설치 중...", flush=True)
        subprocess.run([sys.executable, "-m", "pip", "install",
                        "numpy", "--break-system-packages", "-q"], check=True)


def _decode_audio(path: str, sr: int = SAMPLE_RATE):
    """오디오 파일 → mono float32 numpy 배열 (SR)."""
    import numpy as np
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(sr),
         "-f", "f32le", "-"],
        capture_output=True,
    )
    return np.frombuffer(r.stdout, dtype=np.float32)


def _envelope(x, sr: int = SAMPLE_RATE, fps: int = REFINE_FPS):
    """단시간 에너지 엔벨로프 (로그 압축)."""
    import numpy as np
    hop = max(sr // fps, 1)
    n = len(x) // hop
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    frames = x[:n * hop].reshape(n, hop)
    env = np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1) + 1e-9)
    return np.log1p(env).astype(np.float64)


def _stretch(env, factor: float):
    """엔벨로프를 factor배로 시간축 확대/축소 (배속 되돌리기)."""
    import numpy as np
    n = len(env)
    m = max(int(round(n * factor)), 1)
    return np.interp(np.linspace(0, n - 1, m), np.arange(n), env)


def _ncc(template, signal):
    """template를 signal 위로 슬라이드하며 정규화 상호상관. (best_lag, best_corr)."""
    import numpy as np
    L, N = len(template), len(signal)
    if L < 2 or N < L:
        return 0, -1.0
    t = template - template.mean()
    tn = np.sqrt((t ** 2).sum()) + 1e-9
    dots = np.correlate(signal, t, mode="valid")          # dot(window, t)
    cs  = np.concatenate([[0.0], np.cumsum(signal)])
    cs2 = np.concatenate([[0.0], np.cumsum(signal ** 2)])
    wsum = cs[L:] - cs[:-L]
    wsq  = cs2[L:] - cs2[:-L]
    wnorm = np.sqrt(np.maximum(wsq - wsum ** 2 / L, 1e-9))
    ncc = dots / (tn * wnorm)
    i = int(np.argmax(ncc))
    return i, float(ncc[i])


def download_orig_window(url_or_path: str, t0: float, t1: float, out_wav: str) -> str | None:
    """원본에서 [t0, t1] 구간만 오디오로 확보. 파일의 0초 = 정확히 t0이 되도록.

    URL: yt-dlp -g 로 오디오 스트림 URL만 얻은 뒤 ffmpeg 정밀 seek으로 자른다.
    (download-sections는 조각 단위라 시작이 t0보다 앞설 수 있어 시각이 밀림)
    """
    t0 = max(t0, 0.0)
    src = url_or_path
    if is_url(url_or_path):
        g = subprocess.run(
            ["yt-dlp", "--no-playlist", "--no-warnings", "-f", "bestaudio/best",
             "-g", url_or_path],
            capture_output=True, text=True,
        )
        urls = [u for u in g.stdout.strip().splitlines() if u.startswith("http")]
        if not urls:
            return None
        src = urls[-1]
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", str(t0), "-i", src,
         "-t", str(t1 - t0), "-ac", "1", "-ar", str(SAMPLE_RATE), out_wav],
        capture_output=True,
    )
    return out_wav if os.path.exists(out_wav) else None


def refine_boundary(shorts_env, s_time: float, orig_win_env, win_t0: float,
                    speed: float, side: str):
    """한 경계(숏츠 s_time)의 원본 시각을 오디오 상관으로 정밀화.

    side="start" → s_time부터 뒤로 스니펫, 경계 = 스니펫 시작.
    side="end"   → s_time에서 앞으로 스니펫, 경계 = 스니펫 끝.
    반환: (refined_orig_time 또는 None, corr).
    """
    fps = REFINE_FPS
    if side == "end":
        a = max(s_time - REFINE_SNIP, 0.0); b = s_time
    else:
        a = s_time; b = s_time + REFINE_SNIP
    fa, fb = int(a * fps), int(b * fps)
    snip = shorts_env[fa:fb]
    if len(snip) < fps:               # 스니펫이 너무 짧음
        return None, -1.0
    template = _stretch(snip, speed)   # 배속 되돌려 원본 템포로
    lag, corr = _ncc(template, orig_win_env)
    if corr < REFINE_MIN_CORR:
        return None, corr
    tmpl_start_t = win_t0 + lag / fps
    refined = tmpl_start_t + (len(template) / fps if side == "end" else 0.0)
    return refined, corr


def refine_endpoints(shorts_url, orig_ref, segs, shorts_dur, tmpdir):
    """전체 시작/종료 지점을 오디오로 정밀 보정. (start, end, info) 반환."""
    print("\n[오디오 정밀 보정] 숏츠 오디오 다운로드 중...", flush=True)
    s_wav = download_audio(shorts_url, os.path.join(tmpdir, "refine_s"))
    shorts_env = _envelope(_decode_audio(s_wav))

    first, last = segs[0], segs[-1]
    start_pred = first["C"] + first["speed"] * 0.0
    end_pred   = last["C"]  + last["speed"]  * shorts_dur

    results = {}
    for side, s_time, speed, pred in (
        ("start", 0.0,        first["speed"], start_pred),
        ("end",   shorts_dur, last["speed"],  end_pred),
    ):
        span = REFINE_SNIP * speed
        if side == "end":
            w0, w1 = pred - span - REFINE_MARGIN, pred + REFINE_MARGIN
        else:
            w0, w1 = pred - REFINE_MARGIN, pred + span + REFINE_MARGIN
        print(f"  원본 {fmt(w0)}~{fmt(w1)} 구간 확보 후 대조 중... ({side})", flush=True)
        win = download_orig_window(orig_ref, w0, w1,
                                   os.path.join(tmpdir, f"refine_o_{side}.wav"))
        if not win:
            results[side] = {"pred": pred, "refined": None, "corr": None,
                             "final": pred, "applied": False}
            continue
        orig_env = _envelope(_decode_audio(win))
        refined, corr = refine_boundary(shorts_env, s_time, orig_env,
                                        max(w0, 0.0), speed, side)
        # 안전장치: 상관 충분 + 텍스트 예측과 과도하게 벌어지지 않을 때만 채택
        applied = (refined is not None
                   and corr >= REFINE_MIN_CORR
                   and abs(refined - pred) <= REFINE_MAX_SHIFT)
        results[side] = {
            "pred":    pred,
            "refined": refined,
            "corr":    corr,
            "final":   refined if applied else pred,
            "applied": applied,
        }

    return results["start"]["final"], results["end"]["final"], results


# ─── 출력 ────────────────────────────────────────────────────────────────────

def fmt(sec: float) -> str:
    t = int(round(sec))
    h, m, s = t // 3600, (t % 3600) // 60, t % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def is_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


def get_video_duration(url_or_path: str) -> float | None:
    """영상 전체 길이(초) — URL이면 yt-dlp 메타데이터, 로컬이면 ffprobe."""
    if is_url(url_or_path):
        r = subprocess.run(
            ["yt-dlp", "--print", "duration", "--no-playlist", "--no-warnings",
             url_or_path],
            capture_output=True, text=True, timeout=30,
        )
        try:
            return float(r.stdout.strip())
        except Exception:
            return None
    else:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", url_or_path],
            capture_output=True, text=True,
        )
        try:
            return float(r.stdout.strip())
        except Exception:
            return None


def debug_match_dump(shorts_wl: list[tuple[str, float]],
                     orig_wl:   list[tuple[str, float]]) -> None:
    """숏츠 각 단어/2-gram이 원본 어디에 매칭되는지 덤프. 앞부분이 왜 안 잡히는지 진단."""
    from collections import defaultdict
    sw = [w for w, _ in shorts_wl]; st = [t for _, t in shorts_wl]
    ow = [w for w, _ in orig_wl];   ot = [t for _, t in orig_wl]

    uni = defaultdict(list)                       # 원본 단어 → [시각들]
    for w, t in zip(ow, ot):
        uni[w].append(t)
    bi = defaultdict(list)                         # 원본 2-gram → [시각들]
    for j in range(len(ow) - 1):
        bi[(ow[j], ow[j + 1])].append(ot[j])

    print("\n" + "─" * 60)
    print("[DEBUG] 숏츠 단어별 원본 매칭 (2-gram 우선, 없으면 단어 단독)")
    print("─" * 60)
    print(f"  {'숏츠시각':>7}  {'단어(+다음)':<22}  원본 매칭 위치")
    for i in range(len(sw)):
        pair_hits = bi.get((sw[i], sw[i + 1]), []) if i < len(sw) - 1 else []
        uni_hits  = uni.get(sw[i], [])
        if pair_hits:
            label = f"{sw[i]} {sw[i+1]}"
            hits  = pair_hits
            kind  = "2gram"
        else:
            label = sw[i]
            hits  = uni_hits
            kind  = "word "
        if hits:
            hstr = ", ".join(fmt(h) for h in hits[:6]) + ("…" if len(hits) > 6 else "")
        else:
            hstr = "✗ 원본에 없음"
        print(f"  {fmt(st[i]):>7}  [{kind}] {label:<15}  {hstr}")
    print("─" * 60)
    print("  → 앞부분 단어들이 '✗ 원본에 없음'이면: 그 구간 음성이 원본과 실제로 다름")
    print("     (또는 띄어쓰기/조사 차이로 어긋남 — 이 경우 단어 단독은 매칭될 수 있음)")
    print("  → 앞부분이 원본 7:4x~8:1x 근처에 매칭되는데도 결과에서 빠졌다면:")
    print("     짧은 컷 조각이 탐지 문턱에 걸린 것 → 문턱 완화로 해결 가능")
    print("─" * 60 + "\n")


def print_result(segments: list[dict], shorts_dur: float, best_votes: int,
                 whisper_used: bool = False) -> None:
    segs = sorted(segments, key=lambda x: x["shorts_start"])
    n    = len(segs)

    # 서로 다른 직선(클립)이 2개 이상이면 컷편집본 (find_clips에서 이미 병합·정리됨)
    is_multi = n > 1

    bar = "═" * 54

    # 매칭 커버리지: 숏츠 앞/뒤로 매칭 안 된 구간이 크면 그쪽 경계는 외삽이라 부정확
    matched_lo = min(s["shorts_start"] for s in segs)
    matched_hi = max(s["shorts_end"]   for s in segs)
    head_gap = matched_lo                       # 숏츠 앞쪽 미매칭 길이
    tail_gap = shorts_dur - matched_hi          # 숏츠 뒤쪽 미매칭 길이
    covered  = sum(s["shorts_end"] - s["shorts_start"] for s in segs)

    def _coverage_note():
        print(f"  ── 매칭 커버리지 ─────────────────────────────────")
        print(f"  숏츠 {fmt(shorts_dur)} 중 매칭 {covered:.0f}초 "
              f"({covered / max(shorts_dur, 0.01) * 100:.0f}%)  |  "
              f"매칭 범위 {fmt(matched_lo)}~{fmt(matched_hi)}")
        if head_gap > COVER_GAP:
            print(f"  ⚠ 시작: 숏츠 앞 {head_gap:.0f}초가 매칭 안 됨 → "
                  f"시작점은 외삽값이라 부정확할 수 있어요.")
            if whisper_used:
                print(f"     Whisper로도 앞부분이 안 잡힘 = 그 구간이 원본과 텍스트가")
                print(f"     다른 것(심한 배속으로 ASR이 딴 단어로 인식 / BGM·나레이션).")
                print(f"     → 텍스트로는 복구 불가. 시작점은 직접 확인이 필요합니다.")
            else:
                print(f"     (점프컷으로 앞 조각이 안 잡혔거나 자막이 원본과 다른 경우)")
                print(f"     → --force-whisper 로 양쪽을 같은 엔진으로 재인식해 보세요.")
        if tail_gap > COVER_GAP:
            print(f"  ⚠ 종료: 숏츠 뒤 {tail_gap:.0f}초가 매칭 안 됨 → "
                  f"종료점도 외삽값이라 부정확할 수 있어요.")

    if not is_multi:
        # 단일 클립: 회귀 직선을 Shorts 양끝(t=0, t=shorts_dur)으로 외삽
        primary = segs[0]
        C       = primary["C"]
        speed   = primary["speed"]
        print(f"\n{bar}")
        print(f"  [결과]  Shorts → 원본 구간")
        print(f"{bar}")
        print(f"  시작 : {fmt(C)}")
        print(f"  종료 : {fmt(C + shorts_dur * speed)}")
        print(f"  배속 : {speed:.2f}x  |  "
              f"Shorts {fmt(shorts_dur)}  →  원본 {fmt(shorts_dur * speed)}")
        print(f"{bar}")
        _coverage_note()
        print()
    else:
        # 멀티 클립: 클러스터 갭 중간을 컷 포인트로 추정
        # 전체 범위: 첫 클립을 숏츠 t=0으로, 끝 클립을 t=shorts_dur로 외삽
        first, last = segs[0], segs[-1]
        overall_start = first["C"] + first["speed"] * 0.0
        overall_end   = last["C"]  + last["speed"]  * shorts_dur
        print(f"\n{bar}")
        print(f"  [결과]  Shorts = {n}개 클립 편집본")
        print(f"{bar}")
        print(f"  전체 원본 구간 : {fmt(overall_start)} ~ {fmt(overall_end)}")
        print(f"  (숏츠 맨 앞 → 맨 끝 기준 외삽)")
        print(f"{bar}")
        for i, seg in enumerate(segs):
            C     = seg["C"]
            speed = seg["speed"]

            # 이 클립이 Shorts에서 차지하는 구간
            if i == 0:
                clip_s_start = 0.0
            else:
                clip_s_start = (segs[i-1]["shorts_end"] + seg["shorts_start"]) / 2

            if i == n - 1:
                clip_s_end = shorts_dur
            else:
                clip_s_end = (seg["shorts_end"] + segs[i+1]["shorts_start"]) / 2

            orig_start = C + clip_s_start * speed
            orig_end   = C + clip_s_end   * speed

            print(f"\n  클립 #{i+1}  배속 {speed:.2f}x"
                  f"  (Shorts {fmt(clip_s_start)} ~ {fmt(clip_s_end)})")
            print(f"    원본 시작 : {fmt(orig_start)}")
            print(f"    원본 종료 : {fmt(orig_end)}")
        print(f"{bar}")
        _coverage_note()
        print(f"{bar}\n")

    # 텍스트 매칭 상세 (디버그/검증용)
    print("[텍스트 매칭 상세]")
    for i, seg in enumerate(segs, 1):
        spd   = seg["speed"]
        conf  = seg["confidence"]
        s_dur = seg["shorts_end"] - seg["shorts_start"]
        o_dur = seg["orig_end"]   - seg["orig_start"]
        print(f"  #{i}  배속: {spd:.2f}x  신뢰도: {conf*100:.0f}%  ({seg['votes']}/{best_votes}표)")
        print(f"      Shorts: {fmt(seg['shorts_start'])} ~ {fmt(seg['shorts_end'])}"
              f"  ({s_dur:.1f}초)")
        print(f"      원본  : {fmt(seg['orig_start'])} ~ {fmt(seg['orig_end'])}"
              f"  ({o_dur:.1f}초)")


# ─── 분석 API (다른 스크립트에서 재사용) ─────────────────────────────────────

def analyze(shorts: str, original: str, tmpdir: str,
            whisper_model: str = "base", force_whisper: bool = False,
            debug: bool = False, verbose: bool = True) -> dict | None:
    """숏츠+원본 → 타임스탬프 분석 결과 dict 반환 (실패 시 None).

    반환: {segments, shorts_total_dur, method, start, end, whisper_used, best_votes}
      start/end = 숏츠 맨앞/맨끝에 해당하는 원본 시각(초).
    """
    def log(*a):
        if verbose:
            print(*a)

    orig_is_url = is_url(original)
    s_stem = os.path.join(tmpdir, "s")
    o_stem = os.path.join(tmpdir, "o")

    log("\n[1/3] YouTube 자막 다운로드 중...")
    s_cap = download_captions(shorts, s_stem + "_cap")
    o_cap = download_captions(original, o_stem + "_cap") if orig_is_url else None
    s_lang = os.path.basename(s_cap).split(".")[-2] if s_cap else "없음"
    o_lang = os.path.basename(o_cap).split(".")[-2] if o_cap else "없음"
    log(f"  Shorts: {s_lang} | 원본: {o_lang}")

    if force_whisper:
        log("  --force-whisper: 자막 무시하고 Whisper로 새로 인식합니다.")
        s_cap = o_cap = None

    if s_cap and o_cap:
        s_segs = parse_caption_file(s_cap)
        o_segs = parse_caption_file(o_cap)
        method = "YouTube 자막"
    else:
        log("  자막 없음 → Whisper 음성인식으로 전환")
        s_audio = download_audio(shorts, s_stem + "_aud")
        s_segs  = transcribe(s_audio, whisper_model)
        if o_cap:
            o_segs = parse_caption_file(o_cap)
            method = "Whisper(Shorts) + 자막(원본)"
        elif orig_is_url:
            log("  원본 오디오 다운로드 + 음성인식 중... (시간 걸림)")
            o_audio = download_audio(original, o_stem + "_aud")
            o_segs  = transcribe(o_audio, whisper_model)
            method  = "Whisper 양방향"
        else:
            wav = os.path.join(tmpdir, "orig.wav")
            subprocess.run(
                ["ffmpeg", "-y", "-i", original,
                 "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "wav", wav],
                capture_output=True,
            )
            o_segs = transcribe(wav, whisper_model)
            method = "Whisper 양방향"

    if not s_segs or not o_segs:
        log("  ✗ 자막 파싱 결과 없음.")
        return None

    log("\n[2/3] Shorts 영상 길이 조회 중...")
    shorts_video_dur = get_video_duration(shorts)
    if shorts_video_dur:
        log(f"  Shorts 길이: {fmt(shorts_video_dur)}")
    else:
        log("  ⚠ 길이 조회 실패 — 자막 구간으로 대체")
    shorts_total_dur = shorts_video_dur or (s_segs[-1]["end"] - s_segs[0]["start"])

    log(f"\n[3/3] 오프셋 투표 분석 중... ({method})")
    shorts_wl = segs_to_wordlist(s_segs)
    orig_wl   = segs_to_wordlist(o_segs)
    log(f"  Shorts 단어 수: {len(shorts_wl)} | 원본 단어 수: {len(orig_wl)}")

    if debug:
        debug_match_dump(shorts_wl, orig_wl)

    segments = find_clips(shorts_wl, orig_wl, shorts_total_dur, debug=debug)
    if not segments:
        log("  ✗ 오프셋 투표 결과 없음 (공통 n-gram 부족).")
        if verbose:
            print(f"  Shorts 샘플: {' '.join(w for w, _ in shorts_wl[:15])}")
            print(f"  원본   샘플: {' '.join(w for w, _ in orig_wl[:15])}")
        return None

    segs = sorted(segments, key=lambda x: x["shorts_start"])
    start = segs[0]["C"]
    end   = segs[-1]["C"] + segs[-1]["speed"] * shorts_total_dur
    return {
        "segments":         segments,
        "shorts_total_dur": shorts_total_dur,
        "method":           method,
        "start":            start,
        "end":              end,
        "whisper_used":     "Whisper" in method,
        "best_votes":       max(s["votes"] for s in segments),
    }


# ─── 메인 ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("shorts")
    parser.add_argument("original")
    parser.add_argument("--whisper-model", default="base",
                        choices=["tiny", "base", "small", "medium",
                                 "large-v3-turbo", "large-v3"])
    parser.add_argument("--refine-audio", action="store_true",
                        help="양끝 경계를 오디오 대조로 초 단위 정밀 보정 "
                             "(예측 지점 주변 원본 구간만 다운로드)")
    parser.add_argument("--force-whisper", action="store_true",
                        help="자막이 있어도 무시하고 양쪽을 Whisper로 새로 인식 "
                             "(단어를 같은 엔진으로 통일 → 자막 불일치로 매칭이 "
                             "빈약할 때. 원본이 길면 느립니다)")
    parser.add_argument("--debug", action="store_true",
                        help="숏츠 단어들이 원본 어디에 매칭되는지 덤프 (원인 진단용)")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmpdir:
        res = analyze(args.shorts, args.original, tmpdir,
                      whisper_model=args.whisper_model,
                      force_whisper=args.force_whisper, debug=args.debug)
        if res is None:
            print("  → 언어가 다르거나 단어가 안 겹치면 "
                  "--force-whisper / --whisper-model small 로 재시도하세요.")
            return

        segments        = res["segments"]
        shorts_total_dur = res["shorts_total_dur"]
        best_votes      = res["best_votes"]
        print(f"  ✓ {len(segments)}개 클러스터 감지 (최고 {best_votes}표)")
        print_result(segments, shorts_total_dur, best_votes,
                     whisper_used=res["whisper_used"])

        # ── 4. (옵션) 오디오 정밀 경계 보정 ──────────────────────────────────
        if args.refine_audio:
            ensure_numpy()
            _, _, info = refine_endpoints(
                args.shorts, args.original, segments, shorts_total_dur, tmpdir)
            bar = "═" * 54
            print(f"\n{bar}")
            print(f"  [오디오 정밀 보정 결과]")
            print(f"{bar}")
            for side, label in (("start", "시작"), ("end", "종료")):
                d = info[side]
                pred, refined, corr = d["pred"], d["refined"], d["corr"]
                print(f"  {label}  최종 : {fmt(d['final'])}")
                if refined is None:
                    reason = "원본 구간 확보 실패" if corr is None else "상관 너무 낮음"
                    print(f"        텍스트 {fmt(pred)} 유지  ({reason})")
                else:
                    shift = refined - pred
                    mark = "채택" if d["applied"] else "무시(오보정 의심)"
                    print(f"        텍스트 {fmt(pred)} → 오디오 {fmt(refined)} "
                          f"({shift:+.1f}s, 상관 {corr:.2f}) → {mark}")
            print(f"{bar}\n")


if __name__ == "__main__":
    main()
