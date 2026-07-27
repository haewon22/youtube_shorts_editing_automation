# 숏츠 편집 스튜디오

유튜브 **숏츠 URL + 원본 영상 URL**만 넣으면
1. 숏츠가 원본의 어느 구간(타임스탬프)에서 왔는지 자동으로 찾고
2. 그 구간을 잘라와서 **공백 제거 · 배속 · 컷 편집 · 자막**까지
브라우저에서 단계별로 해주는 로컬 웹앱입니다.

자막은 영상에 굽지 않고 **`.srt`로 내보내서** 프리미어 프로 등에서 최종본에 그대로 얹을 수 있습니다.

---

## 1. 필요한 것

- **Python 3.10 이상**
- **ffmpeg / ffprobe** — 시스템에 설치 필요
  - macOS: `brew install ffmpeg`
  - Ubuntu/Debian: `sudo apt install ffmpeg`
  - Windows: [ffmpeg.org](https://ffmpeg.org/download.html) 에서 받아 PATH 등록

> 자막은 **굽지 않고 .srt로 내보내므로** 특별한 ffmpeg(libass) 빌드는 필요 없습니다. 일반 ffmpeg면 충분합니다.

## 2. 설치

```bash
git clone https://github.com/haewon22/youtube_shorts_editing_automation.git
cd youtube_shorts_editing_automation

# (권장) 가상환경
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

`requirements.txt` 로 `flask · faster-whisper · numpy · yt-dlp` 가 설치됩니다.

## 3. 실행

```bash
python3 app.py
```

터미널에 뜨는 주소를 브라우저로 열면 됩니다:

```
http://127.0.0.1:5000
```

> **첫 실행 시** 자막 인식 모델(Whisper `large-v3`, 약 3GB)이 한 번 다운로드됩니다.
> 오래 걸리면 화면 ⚙ **인식 정확도**에서 `turbo` 나 `small` 같은 가벼운 모델로 바꿔도 됩니다. (한 번 받으면 캐시되어 다음부턴 즉시)

## 4. 사용 흐름

1. **① 소재 추출·컷** — 숏츠 URL + 원본 URL 입력 → `타임스탬프 추출 & 컷`
   - 자주 쓰는 링크는 **별명으로 저장**해두고 드롭다운에서 불러올 수 있어요.
2. **② 편집** — 한 화면에서:
   - 타임라인 **드래그로 컷**, `공백 자동 감지`로 무음 제거 (재생하면 잘린 곳은 자동 건너뜀)
   - **클립 범위** 앞뒤 5초 여유를 핸들/버튼으로 늘리고 줄이기
   - **자막**을 아래에서 직접 확인·수정 (컷하면 자막도 같이 따라감)
   - **`↩ 되돌리기`** 로 컷·자막 무엇이든 한 단계씩 복구
3. **내보내기**
   - `⬇ 최종 영상 만들기` — 자막 없는 편집 영상 (프리미어 추가 작업용)
   - `⬇ 자막 (.srt)` — 최종 영상 타임라인에 맞춘 자막 파일
4. **⚡ 한 번에** — 설정만 정해두고 URL → 최종본까지 한 번에
5. 작업은 **자동 저장**되어 새로고침해도 이어집니다. 지우려면 우상단 **`↺ 초기화`**.

---

## 명령줄(CLI)로도 사용 가능

웹 없이 터미널에서 바로 쓰고 싶다면:

```bash
# 타임스탬프만 추출
python3 find_timestamp.py <숏츠_URL> <원본_URL>

# 전체 파이프라인 (추출 → 편집 → 최종본)
python3 run.py <숏츠_URL> <원본_URL> -o final.mp4

# 로컬 영상 편집만 (공백제거/배속/자막)
python3 edit_shorts.py <입력영상_또는_URL> -o out.mp4
```

각 스크립트는 `--help` 로 옵션을 볼 수 있습니다.

---

## 구성

| 파일 | 역할 |
|------|------|
| `app.py` | 웹 서버 (Flask) — 위 웹 UI 백엔드 |
| `index.html` | 웹 UI (단일 파일) |
| `find_timestamp.py` | 숏츠 ↔ 원본 타임스탬프 매칭 엔진 |
| `edit_shorts.py` | 편집 엔진 (공백제거 · 배속 · 피치 · 자막) |
| `run.py` | 추출 + 편집을 잇는 CLI 파이프라인 |

작업 중 생성되는 영상/자막은 `work/` 폴더에 저장되며 git에는 올라가지 않습니다.
