#!/usr/bin/env python3
"""GeekNews Morning Brief - 독립 실행 스크립트 (Aside 비의존).

4개 공개 피드를 직접 받아 밝은 테마 한국어 HTML 1장으로 생성한다.
영어 항목 번역은 ANTHROPIC_API_KEY가 있으면 Claude(haiku)로, 없으면 원문 유지.
표준 라이브러리만 사용. 사용법:  python3 build.py   /   python3 build.py --selftest
"""
import html, json, os, re, signal, subprocess, sys, tempfile, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "geeknews-daily-brief.html")


def load_env():
    """같은 폴더의 .env(KEY=VALUE) 를 환경변수로 로드. cron은 셸 프로필을 안 읽으므로 필요."""
    p = os.path.join(HERE, ".env")
    if not os.path.exists(p):
        return
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


load_env()
KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (geeknews-brief)"}

# (라벨, URL, 영어여부, 최대항목수)
FEEDS = [
    ("GeekNews",            "https://news.hada.io/rss/news",        False, 15),
    ("Hacker News",         "https://hnrss.org/frontpage",          True,  12),
    ("Cloudflare Blog",     "https://blog.cloudflare.com/rss/",     True,  6),
    ("GitHub Engineering",  "https://github.blog/engineering.atom", True,  6),
]


def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def strip_ns(tag):
    return tag.split("}", 1)[-1]


def clean_text(s):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)          # HTML 태그 제거
    s = html.unescape(s)
    # HN 보일러플레이트 제거
    s = re.sub(r"(Article URL|Comments URL|Points|#\s*Comments)\s*:.*?(?=(Article URL|Comments URL|Points|#\s*Comments)\s*:|$)",
               " ", s, flags=re.I | re.S)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_feed(raw):
    """RSS(<item>)와 Atom(<entry>) 모두 처리. [{title, link, summary}] 반환."""
    root = ET.fromstring(raw)
    items = []
    nodes = root.iter()
    entries = [e for e in nodes if strip_ns(e.tag) in ("item", "entry")]
    for e in entries:
        title = link = summary = ""
        points = comments = 0
        for c in e:
            t = strip_ns(c.tag)
            if t == "title":
                title = (c.text or "").strip()
            elif t == "link":
                # RSS: text / Atom: href 속성
                link = (c.text or "").strip() or c.get("href", "")
            elif t in ("description", "summary", "content") and not summary:
                raw = c.text or ""
                mp = re.search(r"Points?:\s*(\d+)", raw)        # HN: 추천 점수 (clean 전 캡처)
                mc = re.search(r"#\s*Comments?:\s*(\d+)", raw)   # HN: 댓글 수
                if mp:
                    points = int(mp.group(1))
                if mc:
                    comments = int(mc.group(1))
                summary = clean_text(raw)
        items.append({"title": html.unescape(title), "link": link, "summary": summary,
                      "points": points, "comments": comments})
    return items


def fetch_geeknews(limit_html=40):
    """GeekNews 홈페이지를 스크래핑해 점수·댓글 포함 항목 반환 (RSS엔 점수가 없음)."""
    h = fetch("https://news.hada.io/").decode("utf-8", "replace")
    items = []
    for p in re.split(r"<div class=topictitle>", h)[1:]:
        tid = re.search(r"topic\?id=(\d+)", p)
        title = re.search(r"topic-title-heading'>(.*?)</h2>", p, re.S)
        if not (tid and title):
            continue
        desc = re.search(r"topicdesc'><a[^>]*>(.*?)</a>", p, re.S)
        pts = re.search(r"id='tp\d+'>(\d+)</span>\s*point", p)
        cmt = re.search(r"data-topic-comment-count='(\d+)'", p)
        items.append({
            "title": html.unescape(re.sub(r"<[^>]+>", "", title.group(1)).strip()),
            "link": f"https://news.hada.io/topic?id={tid.group(1)}",
            "summary": clean_text(desc.group(1)) if desc else "",
            "points": int(pts.group(1)) if pts else 0,
            "comments": int(cmt.group(1)) if cmt else 0,
        })
        if len(items) >= limit_html:
            break
    return items


def is_excluded(label, title):
    t = title.strip()
    if label == "GeekNews" and re.match(r"^(Ask|Show)\s+GN:", t, re.I):
        return True
    if label == "Hacker News" and re.search(r"\bhiring\b|who is hiring|freelancer\?|seeking freelancer", t, re.I):
        return True
    return False


def norm(title):
    return re.sub(r"[^a-z0-9가-힣]", "", title.lower())


# (카테고리, 키워드) — 위에서부터 첫 매칭 우선. 토큰 없이 제목 기반 분류.
CATEGORIES = [
    ("Security",     r"security|vuln|cve|exploit|malware|breach|phishing|ransomware|auth|encrypt|보안|취약점|해킹|암호|인증"),
    ("AI",           r"\bai\b|\bml\b|llm|gpt|chatgpt|openai|claude|gemini|neural|model|agent|인공지능|머신러닝|딥러닝|모델|신경망|에이전트"),
    ("Frontend",     r"frontend|react|vue|svelte|\bcss\b|browser|tailwind|webgl|wasm|ui|ux|프론트|브라우저|웹"),
    ("Backend",      r"backend|database|\bsql\b|postgres|server|kubernetes|docker|cloud|api|distributed|kafka|백엔드|서버|데이터베이스|클라우드|분산"),
    ("Productivity", r"productivity|workflow|tool|editor|terminal|생산성|워크플로|도구|협업"),
]


def categorize(title):
    t = title.lower()
    for name, pat in CATEGORIES:
        if re.search(pat, t):
            return name
    return "General"


GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


def translate(items):
    """영어 항목 리스트 -> [{ko_title, ko_summary}]. 키 없으면 None. (Google Gemini)"""
    key = os.environ.get("GEMINI_API_KEY")
    if not key or not items:
        return None
    payload = [{"i": i, "title": it["title"], "summary": it["summary"][:500]} for i, it in enumerate(items)]
    prompt = (
        "다음 영어 기술 뉴스 항목들을 한국어로 변환해줘. 각 항목마다 자연스러운 한국어 제목(ko_title)과 "
        "한두 문장의 간결한 한국어 요약(ko_summary)을 만들어줘. 원문 메타데이터를 그대로 옮기지 말고 핵심만 자연스럽게.\n"
        "JSON 배열로만 답해. 형식: [{\"i\":0,\"ko_title\":\"...\",\"ko_summary\":\"...\"}, ...]\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"},
    }).encode()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={key}"
    req = urllib.request.Request(url, data=body, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read())
        txt = data["candidates"][0]["content"]["parts"][0]["text"]
        txt = re.sub(r"^```(json)?|```$", "", txt.strip(), flags=re.M).strip()
        arr = json.loads(txt)
        out = {d["i"]: d for d in arr}
        return [out.get(i, {}) for i in range(len(items))]
    except Exception as e:
        print(f"[warn] 번역 실패, 원문 유지: {e}", file=sys.stderr)
        return None


def esc(s):
    return html.escape(s or "")


def interleave(sources, n):
    """소스별 라운드로빈으로 균형 있게 n개 선정 (각 소스는 이미 정렬된 상태)."""
    cols = [list(its) for _, its in sources]
    out = []
    while len(out) < n and any(cols):
        for c in cols:
            if c:
                out.append(c.pop(0))
                if len(out) >= n:
                    break
    return out


def share_text(top, now):
    """상위 항목을 휴대성 좋은 텍스트로. 메신저·메일 어디든 붙여넣기 가능."""
    lines = [f"📰 Developer Morning Brief — {now.strftime('%Y-%m-%d')} (Asia/Seoul)", ""]
    for i, it in enumerate(top, 1):
        badge = []
        if it.get("points"):
            badge.append(f"▲{it['points']}")
        if it.get("comments"):
            badge.append(f"💬{it['comments']}")
        b = (" " + " ".join(badge)) if badge else ""
        lines.append(f"{i}. [{it['src']}{b}] {it['disp_title']}")
        if it.get("summary"):
            lines.append(f"   {it['summary']}")
        lines.append(f"   {it['link']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def share_html(top, now):
    """공유용 플랫 카드 HTML (탭 없음 → PDF/PNG로 떠도 전부 보임)."""
    rows = ""
    for i, it in enumerate(top, 1):
        badges = []
        if it.get("points"):
            badges.append(f'▲{it["points"]}')
        if it.get("comments"):
            badges.append(f'💬{it["comments"]}')
        meta = " · ".join([esc(it["src"])] + badges)
        summ = f'<p class="s">{esc(it["summary"])}</p>' if it.get("summary") else ""
        rows += (f'<div class="it"><div class="m">{i}. {meta}</div>'
                 f'<div class="t">{esc(it["disp_title"])}</div>{summ}'
                 f'<div class="u">{esc(it["link"])}</div></div>')
    return ('<!doctype html><html lang="ko"><head><meta charset="utf-8"><style>'
            'body{width:720px;margin:0 auto;padding:28px;background:#fff;color:#1f2937;'
            "font-family:-apple-system,'Apple SD Gothic Neo','Segoe UI',sans-serif;line-height:1.55}"
            'h1{font-size:26px;margin:0 0 4px;letter-spacing:-.02em}.d{color:#6b7280;margin:0 0 18px;font-size:13px}'
            '.it{border-top:1px solid #e5e7eb;padding:14px 0}.it:first-of-type{border-top:0}'
            '.m{color:#6b7280;font-size:12px;margin-bottom:4px}.t{font-size:17px;font-weight:700;line-height:1.4}'
            '.s{color:#374151;font-size:14px;margin:5px 0}.u{color:#9ca3af;font-size:11px;word-break:break-all}'
            f'</style></head><body><h1>Developer Morning Brief</h1>'
            f'<p class="d">{now.strftime("%Y-%m-%d")} · Asia/Seoul · 상위 {len(top)}건</p>{rows}</body></html>')


def find_chrome():
    for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              "/Applications/Chromium.app/Contents/MacOS/Chromium",
              "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
              "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
              "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",      # Linux (GitHub 러너 포함)
              "/usr/bin/chromium-browser", "/usr/bin/chromium"):
        if os.path.exists(p):
            return p
    return None


def chrome_capture(chrome, out_file, out_args, url, wait=25):
    """Chrome 헤드리스로 파일 생성. 작업 후 종료 안 하는 경우가 있어 파일이 생기면 직접 종료."""
    base = [chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--disable-extensions", "--disable-background-networking",
            f"--user-data-dir={tempfile.mkdtemp()}"]
    if os.path.exists(out_file):
        os.remove(out_file)
    p = subprocess.Popen(base + out_args + [url], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    ok = False
    for _ in range(wait * 10):
        if os.path.exists(out_file) and os.path.getsize(out_file) > 0:
            ok = True
            break
        if p.poll() is not None:
            break
        time.sleep(0.1)
    time.sleep(0.5)  # 마무리 쓰기 여유
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        p.wait(timeout=5)
    except Exception:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except Exception:
            pass
    return ok and os.path.exists(out_file) and os.path.getsize(out_file) > 0


def og_html(top, now, total, nsrc):
    """1200×630 OG 미리보기 카드 (이모지 없이 — 리눅스 폰트 호환)."""
    items = ""
    for it in top[:3]:
        badge = f'<span class="b">▲{it["points"]}</span>' if it.get("points") else ""
        items += (f'<div class="i"><span class="src">{esc(it["src"])}</span>{badge}'
                  f'<div class="t">{esc(it["disp_title"])}</div></div>')
    return ('<!doctype html><meta charset="utf-8"><style>'
            'html,body{margin:0}body{position:relative;width:1200px;height:630px;box-sizing:border-box;'
            "padding:64px 72px;background:linear-gradient(135deg,#eef4ff,#f7f7f3);color:#1f2937;"
            "font-family:-apple-system,'Apple SD Gothic Neo','Noto Sans CJK KR','Segoe UI',sans-serif;overflow:hidden}"
            '.h{font-size:58px;font-weight:800;letter-spacing:-.02em;margin:0}'
            '.d{color:#2563eb;font-size:24px;font-weight:700;margin:8px 0 30px}'
            '.i{margin:0 0 18px}.src{font-size:16px;color:#6b7280;font-weight:700}'
            '.b{font-size:16px;color:#2563eb;margin-left:8px;font-weight:700}'
            '.t{font-size:30px;font-weight:700;line-height:1.3;margin-top:2px;'
            'white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
            '.f{position:absolute;bottom:48px;left:72px;color:#6b7280;font-size:20px}'
            f'</style><body><p class="h">Developer Morning Brief</p>'
            f'<p class="d">{now.strftime("%Y-%m-%d")} · Asia/Seoul</p>{items}'
            f'<div class="f">오늘 {nsrc}개 소스 · 총 {total}건</div></body>')


def og_filename(now):
    return f"og-{now.strftime('%Y-%m-%d')}.png"


def generate_og(top, now, total, nsrc):
    """OG 카드 HTML → og-YYYY-MM-DD.png (1200×630, 2x). Chrome 없으면 건너뜀."""
    ogh = os.path.join(HERE, "og.html")
    with open(ogh, "w", encoding="utf-8") as f:
        f.write(og_html(top, now, total, nsrc))
    name = og_filename(now)
    for f in os.listdir(HERE):          # 지난 날짜 og 파일 정리 (오늘 것만 유지)
        if f.startswith("og-") and f.endswith(".png") and f != name:
            try:
                os.remove(os.path.join(HERE, f))
            except OSError:
                pass
    chrome = find_chrome()
    if not chrome:
        print(f"[warn] Chrome 미발견 — {name} 생략", file=sys.stderr)
        return
    png = os.path.join(HERE, name)
    args = ["--screenshot=" + png, "--window-size=1200,630", "--hide-scrollbars", "--force-device-scale-factor=2"]
    print(f"OG: {png}" if chrome_capture(chrome, png, args, "file://" + ogh) else f"[warn] {name} 생성 실패")


def export_assets(top, now):
    """공유 카드 HTML을 PDF·PNG로 내보내기 (설치된 Chrome 헤드리스 사용)."""
    sh = os.path.join(HERE, "geeknews-brief-share.html")
    with open(sh, "w", encoding="utf-8") as f:
        f.write(share_html(top, now))
    chrome = find_chrome()
    if not chrome:
        print("[warn] Chrome 미발견 — share.html만 생성, PDF/PNG 건너뜀", file=sys.stderr)
        return
    pdf = os.path.join(HERE, "geeknews-brief.pdf")
    png = os.path.join(HERE, "geeknews-brief.png")
    url = "file://" + sh
    got = []
    if chrome_capture(chrome, pdf, [f"--print-to-pdf={pdf}", "--no-pdf-header-footer"], url):
        got.append(f"PDF: {pdf}")
    if chrome_capture(chrome, png, [f"--screenshot={png}", "--window-size=760,2400", "--hide-scrollbars",
                                    "--force-device-scale-factor=2"], url):
        got.append(f"PNG: {png}")
    print("\n".join(got) if got else "[warn] PDF/PNG 내보내기 실패")


def render(sources, now, total, translated_count):
    chips = "".join(f'<div class="chip">{esc(lbl)} · {len(its)}건</div>' for lbl, its in sources)
    top = interleave(sources, 8)
    notable = top[:3]
    b1 = f"오늘 {len(sources)}개 소스에서 총 {total}건을 모았습니다 (영어 {translated_count}건 번역)."
    b2 = "분야: 개발/기술/스타트업 뉴스와 엔지니어링 블로그 중심."
    b3 = "주목: " + " · ".join(esc(it["disp_title"]) for it in notable) if notable else "주목할 항목 없음."

    def article(it, lead=True):
        cls = "lead" if lead else "list-item"
        tcls = "title" if lead else "title small"
        orig = f'<div class="orig-title">{esc(it["orig_title"])}</div>' if it.get("orig_title") else ""
        summ = f'<div class="summary">{esc(it["summary"])}</div>' if it.get("summary") else ""
        pts = f'<span class="source-tag">▲ {it["points"]}</span>' if it.get("points") else ""
        cmt = f'<span class="source-tag">💬 {it["comments"]}</span>' if it.get("comments") else ""
        return (f'<article class="{cls}"><div class="meta"><span class="source-tag">{esc(it["src"])}</span>{pts}{cmt}</div>'
                f'<div class="{tcls}"><a href="{esc(it["link"])}" target="_blank" rel="noreferrer">{esc(it["disp_title"])}</a></div>'
                f'{orig}{summ}</article>')

    top_html = "".join(article(it, True) for it in top)
    # 소스별: CSS-only 라디오 탭 (JS 없음)
    radios = labels = panels = tabcss = ""
    for i, (lbl, its) in enumerate(sources):
        radios += f'<input class="tabradio" type="radio" name="srctab" id="srctab{i}"{" checked" if i == 0 else ""}>'
        labels += f'<label for="srctab{i}">{esc(lbl)} ({len(its)})</label>'
        rows = "".join(article(it, False) for it in its)
        panels += f'<div class="tabpanel">{rows}</div>'
        tabcss += (f'#srctab{i}:checked~.tablabels>label:nth-child({i + 1}){{background:var(--accent);color:#fff;border-color:var(--accent)}}'
                   f'#srctab{i}:checked~.tabpanels>.tabpanel:nth-child({i + 1}){{display:block}}')
    blocks = f'<div class="tabwrap">{radios}<div class="tablabels">{labels}</div><div class="tabpanels">{panels}</div></div>'

    # 카테고리별 모아보기 (키워드 분류) — 소스별과 동일한 CSS-only 탭
    all_items = [it for _, its in sources for it in its]
    order = ["AI", "Security", "Frontend", "Backend", "Productivity", "General"]
    groups = [(cat, [it for it in all_items if it.get("cat") == cat]) for cat in order]
    groups = [(c, g) for c, g in groups if g]
    cradios = clabels = cpanels = ""
    for i, (cat, group) in enumerate(groups):
        cradios += f'<input class="tabradio" type="radio" name="cattab" id="cattab{i}"{" checked" if i == 0 else ""}>'
        clabels += f'<label for="cattab{i}">{esc(cat)} ({len(group)})</label>'
        rows = "".join(article(it, False) for it in group)
        cpanels += f'<div class="tabpanel">{rows}</div>'
        tabcss += (f'#cattab{i}:checked~.tablabels>label:nth-child({i + 1}){{background:var(--accent);color:#fff;border-color:var(--accent)}}'
                   f'#cattab{i}:checked~.tabpanels>.tabpanel:nth-child({i + 1}){{display:block}}')
    cat_blocks = f'<div class="tabwrap">{cradios}<div class="tablabels">{clabels}</div><div class="tabpanels">{cpanels}</div></div>'

    css = (":root{--bg:#f7f7f3;--paper:#fff;--ink:#1f2937;--muted:#6b7280;--line:#e5e7eb;--accent:#2563eb;--soft:#eef4ff;--tag:#f3f4f6}"
           "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo','Segoe UI',sans-serif;line-height:1.6}"
           ".wrap{max-width:1120px;margin:0 auto;padding:32px 20px 60px}.hero,.panel{background:var(--paper);border:1px solid var(--line);border-radius:20px}.hero{padding:28px}.panel{padding:24px}"
           ".section{margin-top:22px}.grid{display:grid;grid-template-columns:1.3fr .9fr;gap:20px}h1{margin:0 0 10px;font-size:38px;letter-spacing:-.02em}h2{margin:0 0 16px;font-size:24px}"
           ".desc,.meta,.foot,.orig-title{color:var(--muted)}.briefing{margin-top:18px;padding:16px 18px;background:var(--soft);border-radius:16px;color:#1e3a8a}.briefing p{margin:0 0 8px}.briefing p:last-child{margin-bottom:0}"
           ".chips{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}.chip,.source-tag,.cat-name{display:inline-block;border-radius:999px}.chip{background:var(--tag);border:1px solid var(--line);padding:8px 12px;font-size:14px}"
           ".lead,.list-item{border-top:1px solid var(--line)}.lead{padding:16px 0}.lead:first-of-type,.list-item:first-child{border-top:0;padding-top:0}.meta{font-size:13px;margin-bottom:6px}"
           ".title{font-size:20px;font-weight:700;line-height:1.45;margin:0 0 4px}.title.small{font-size:16px;margin-bottom:2px}.orig-title{font-size:13px;margin-bottom:8px}.summary{color:#374151;font-size:15px}.list-item{padding:12px 0}"
           ".source-tag{background:#f9fafb;border:1px solid var(--line);padding:3px 8px;font-size:12px;margin-right:6px;color:#4b5563}.cat-block,.source-block{margin-top:18px}"
           ".cat-name{background:var(--soft);color:var(--accent);padding:6px 10px;font-size:13px;font-weight:700;margin-bottom:10px}a{color:inherit;text-decoration:none}a:hover{color:var(--accent)}"
           ".tabradio{display:none}.tablabels{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}"
           ".tablabels label{cursor:pointer;border:1px solid var(--line);background:var(--tag);border-radius:999px;padding:6px 12px;font-size:13px;font-weight:600}.tabpanel{display:none}"
           "@media (max-width:900px){.wrap{padding:18px 14px 40px}.grid{grid-template-columns:1fr}h1{font-size:30px}.title{font-size:18px}}"
           + tabcss)

    site = os.environ.get("SITE_URL", "https://devtedlee.github.io/geeknews-brief").rstrip("/")
    ogimg = f"{site}/{og_filename(now)}"   # 날짜 파일명 → 매일 URL이 달라 SNS 캐시 우회
    ogdesc = f"오늘 {len(sources)}개 소스 · 총 {total}건" + (
        " — " + " / ".join(it["disp_title"] for it in top[:2]) if top else "")
    ogmeta = (f'<meta property="og:type" content="website"/>'
              f'<meta property="og:title" content="Developer Morning Brief"/>'
              f'<meta property="og:description" content="{esc(ogdesc)}"/>'
              f'<meta property="og:url" content="{site}/"/>'
              f'<meta property="og:image" content="{ogimg}"/>'
              f'<meta property="og:image:width" content="2400"/>'
              f'<meta property="og:image:height" content="1260"/>'
              f'<meta name="twitter:card" content="summary_large_image"/>'
              f'<meta name="twitter:title" content="Developer Morning Brief"/>'
              f'<meta name="twitter:description" content="{esc(ogdesc)}"/>'
              f'<meta name="twitter:image" content="{ogimg}"/>')
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"/>'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"/>'
            f'<title>Developer Morning Brief</title>{ogmeta}<style>{css}</style></head><body><div class="wrap">'
            f'<section class="hero"><h1>Developer Morning Brief</h1>'
            f'<div class="desc">{now.strftime("%Y년 %m월 %d일 %H:%M")} (Asia/Seoul) 기준</div>'
            f'<div class="briefing"><p>{esc(b1)}</p><p>{esc(b2)}</p><p>{b3}</p></div>'
            f'<div class="chips">{chips}</div></section>'
            f'<section class="section grid"><div class="panel"><h2>주요 항목</h2>{top_html}</div>'
            f'<div class="panel"><h2>소스별 보기</h2>{blocks}</div></section>'
            f'<section class="section"><div class="panel"><h2>카테고리별 모아보기</h2>{cat_blocks}</div></section>'
            f'<div class="foot section">생성: {now.strftime("%Y-%m-%d %H:%M:%S")} KST · build.py</div>'
            f'</div></body></html>')


def build():
    now = datetime.now(KST)
    seen, sources, total = set(), [], 0
    for lbl, url, is_en, limit in FEEDS:
        try:
            items = fetch_geeknews() if lbl == "GeekNews" else parse_feed(fetch(url))
        except Exception as e:
            print(f"[warn] {lbl} 수집 실패: {e}", file=sys.stderr)
            continue
        kept = []
        for it in items:
            if not it["title"] or is_excluded(lbl, it["title"]):
                continue
            k = norm(it["title"])
            if k in seen:
                continue
            seen.add(k)
            it["src"] = lbl
            it["is_en"] = is_en
            it["rank"] = it.get("points", 0) + it.get("comments", 0)   # 순위 = 점수 + 댓글
            kept.append(it)
            if len(kept) >= limit:
                break
        if any(it["rank"] for it in kept):              # 점수·댓글 신호 있으면 그 순으로 정렬
            kept.sort(key=lambda it: it["rank"], reverse=True)
        sources.append((lbl, kept))
        total += len(kept)

    # 영어 항목 번역
    en_items = [it for _, its in sources for it in its if it["is_en"]]
    tr = translate(en_items)
    tcount = 0
    if tr:
        for it, d in zip(en_items, tr):
            if d.get("ko_title"):
                it["orig_title"] = it["title"]
                it["disp_title"] = d["ko_title"]
                it["summary"] = d.get("ko_summary", it["summary"])
                tcount += 1
    for _, its in sources:
        for it in its:
            it.setdefault("disp_title", it["title"])
            it["cat"] = categorize(it["disp_title"] + " " + it["title"])

    html_out = render(sources, now, total, tcount)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html_out)
    # GitHub Pages 루트용 (Pages는 index.html을 기본 제공)
    with open(os.path.join(HERE, "index.html"), "w", encoding="utf-8") as f:
        f.write(html_out)
    assert os.path.exists(OUT)

    # 공유용 텍스트 다이제스트 (상위 항목만, 어디든 붙여넣기 가능)
    top = interleave(sources, int(os.environ.get("SHARE_N", "8")))
    txt = share_text(top, now)
    txt_path = os.path.join(HERE, "geeknews-brief.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(txt)
    nsrc = len([s for s in sources if s[1]])
    print(f"OK: {OUT} | 소스 {nsrc}개, 총 {total}건, 번역 {tcount}건")
    print(f"공유텍스트: {txt_path} (상위 {len(top)}건)")

    # OG 미리보기 이미지 (Chrome 있으면 항상 생성 → Pages 배포에 포함)
    generate_og(top, now, total, nsrc)

    # PDF/PNG 내보내기: --export 플래그 또는 EXPORT=1 일 때만
    if "--export" in sys.argv or os.environ.get("EXPORT") == "1":
        export_assets(top, now)


def selftest():
    rss = b"""<rss><channel>
      <item><title>Ask GN: foo</title><link>http://a</link><description>x</description></item>
      <item><title>Real Topic</title><link>http://b</link><description>desc &amp; more</description></item>
    </channel></rss>"""
    items = parse_feed(rss)
    assert len(items) == 2 and items[1]["title"] == "Real Topic", items
    assert is_excluded("GeekNews", "Ask GN: foo") and not is_excluded("GeekNews", "Real Topic")
    assert is_excluded("Hacker News", "Acme (YC) is hiring engineers")
    atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>T</title><link href="http://c"/><summary>s</summary></entry></feed>'
    a = parse_feed(atom)
    assert a[0]["link"] == "http://c", a
    hn = clean_text("Big news<p>Article URL: http://x Comments URL: http://y Points: 10 # Comments: 3</p>")
    assert "Article URL" not in hn and "Big news" in hn, hn
    assert norm("Hello, World!") == "helloworld"
    # 점수 파싱
    hnfeed = b"""<rss><channel>
      <item><title>Low</title><link>l</link><description>Points: 5 # Comments: 1</description></item>
      <item><title>High</title><link>h</link><description>Points: 99 # Comments: 9</description></item>
    </channel></rss>"""
    pi = parse_feed(hnfeed)
    assert pi[0]["points"] == 5 and pi[1]["points"] == 99, pi
    # HN 댓글 파싱
    hc = parse_feed(b"<rss><channel><item><title>T</title><link>l</link>"
                    b"<description>Points: 7 # Comments: 42</description></item></channel></rss>")
    assert hc[0]["comments"] == 42, hc
    # 라운드로빈 균형: 소스별로 번갈아
    src = [("A", [{"t": 1}, {"t": 2}]), ("B", [{"t": 3}, {"t": 4}])]
    inter = interleave(src, 3)
    assert inter == [{"t": 1}, {"t": 3}, {"t": 2}], inter
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        build()
