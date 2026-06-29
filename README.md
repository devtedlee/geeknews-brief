# GeekNews Morning Brief

GeekNews · Hacker News · Cloudflare Blog · GitHub Engineering 피드를 매일 모아 한국어 브리핑 HTML 한 장으로 생성한다. 표준 라이브러리만 사용(의존성 0).

## 실행

```bash
python3 build.py            # index.html + 공유 텍스트 생성
python3 build.py --export   # 추가로 PDF/PNG 내보내기 (Chrome 필요)
python3 build.py --selftest # 자체 테스트
```

## 환경

- `GEMINI_API_KEY` — 영어 항목 한국어 번역(Google Gemini). 없으면 원문 유지. `.env`(커밋 안 됨)에 저장.
- `SHARE_N` — 공유 다이제스트 항목 수 (기본 8).

## 순위

각 소스 내에서 **점수 + 댓글 수**로 정렬, 주요 항목은 소스 라운드로빈으로 균형 선정.

배포: GitHub Pages (`index.html`).
