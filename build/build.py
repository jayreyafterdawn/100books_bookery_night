#!/usr/bin/env python3
"""백북스 〈취향이 만나는 책밤〉 아카이브 빌더 (CI/로컬 겸용).

노션 공개 페이지에서 모임·책 데이터를 읽어 ../index.html을 생성한다.
- 모임 목록(회차·날짜·주제)은 노션 '모임' DB에서 동적으로 읽음 → 새 회차 자동 반영
- 책 표지(각 책 페이지 본문의 첫 이미지)와 알라딘 ItemId는 build/*.json에 캐시,
  새 책만 추가 수집
- 표지 축소: Pillow(CI) 또는 sips(macOS) 사용
실행: python3 build/build.py   (의존성: fonttools, pillow[CI])
"""
import json, re, os, io, time, base64, html, subprocess, tempfile
import urllib.request, urllib.parse, urllib.error
from datetime import date, datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(ROOT, "..", "index.html"))

PAGE_ID = "30314bac-e300-80c7-bde1-c8a9c120d0eb"
SPACE_ID = "559d6673-4a03-4323-af0a-8812aaf0efeb"
MEET_COLL = "31514bac-e300-80bc-a43d-000b179e75ab"
MEET_VIEW = "31514bac-e300-8098-aa9e-000c3b1e8322"
BOOK_COLL = "31514bac-e300-80ea-abb5-000b1dd9d38d"
BOOK_VIEW = "31514bac-e300-8003-87c3-000c503be963"

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# ---------------------------------------------------------------- notion api
def notion_post(path, body):
    req = urllib.request.Request(
        f"https://www.notion.so/api/v3/{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **UA},
        method="POST")
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r)

def query_collection(coll_id, view_id):
    body = {
        "source": {"type": "collection", "id": coll_id, "spaceId": SPACE_ID},
        "collectionView": {"id": view_id, "spaceId": SPACE_ID},
        "loader": {"reducers": {"collection_group_results": {"type": "results", "limit": 500}},
                   "searchQuery": "", "userTimeZone": "Asia/Seoul"},
    }
    data = notion_post("queryCollection?src=initial_load", body)
    rm = data.get("recordMap", {})
    rows, schema = {}, {}
    for cid, wrap in rm.get("collection", {}).items():
        v = wrap.get("value", {}); v = v.get("value", v)
        if v and v.get("id") == coll_id:
            schema = v.get("schema", {})
    for bid, wrap in rm.get("block", {}).items():
        v = wrap.get("value", {}); v = v.get("value", v)
        if v and v.get("parent_id") == coll_id and v.get("alive"):
            rows[bid] = v
    return schema, rows

def load_chunk(page_id):
    return notion_post("loadPageChunk", {
        "pageId": page_id, "limit": 100, "cursor": {"stack": []},
        "chunkNumber": 0, "verticalColumns": False})

def rt(prop):
    if not prop:
        return ""
    parts = []
    for seg in prop:
        txt = seg[0]
        if txt == "‣" and len(seg) > 1:
            for fmt in seg[1]:
                if fmt[0] == "d":
                    txt = fmt[1].get("start_date", "")
                elif fmt[0] == "p":
                    txt = ""
        parts.append(txt)
    return "".join(parts).strip()

def props_by_name(row, schema):
    out = {}
    for k, spec in schema.items():
        v = rt(row.get("properties", {}).get(k))
        if v:
            out[spec["name"]] = v
    return out

def norm(t):
    return re.sub(r"[^0-9a-zA-Z가-힣]", "", t).lower()

def esc(s):
    return html.escape(s, quote=True)

def classify(b):
    """'선정' 값을 유연하게 분류 — 노션에서 옵션 이름이 바뀌어도 견디도록.
    (예: '선정도서 후보'→'후보', '참고목록'→'참고도서')"""
    v = b.get("선정", "")
    if "후보" in v:
        return "cand"
    if v.startswith("선정"):
        return "sel"
    return "ref"  # 참고목록/참고도서/미분류

# ---------------------------------------------------------------- fetch data
print("노션에서 데이터 읽는 중...")
meet_schema, meet_rows = query_collection(MEET_COLL, MEET_VIEW)
book_schema, book_rows = query_collection(BOOK_COLL, BOOK_VIEW)
print(f"모임 {len(meet_rows)}건, 책 {len(book_rows)}권")

books = []
for rid, row in book_rows.items():
    b = props_by_name(row, book_schema)
    if b.get("책 제목"):
        b["_rid"] = rid
        books.append(b)

# 모임 목록 → MEETINGS (회차 오름차순)
KNOWN_COLORS = {  # topic key → (light, dark)
    "서재": ("#1baf7a", "#199e70"),
    "인공지능": ("#2a78d6", "#3987e5"),
    "영화음악": ("#4a3aa7", "#9085e9"),
    "세기말빈": ("#c98500", "#eda100"),
    "여름": ("#008ba8", "#1d9aae"),
    "르네상스": ("#eb6834", "#d95926"),
}
PALETTE = [  # 새 주제용 (light, dark)
    ("#e34948", "#e66767"), ("#008300", "#33a133"), ("#b45309", "#d97706"),
    ("#0e7490", "#22d3ee"), ("#7c3aed", "#a78bfa"), ("#be185d", "#f472b6"),
    ("#4d7c0f", "#84cc16"), ("#1e40af", "#60a5fa"),
]
DISPLAY_NAMES = {"세기말빈": "세기말 빈"}

meetings = []
for rid, row in meet_rows.items():
    p = props_by_name(row, meet_schema)
    no_txt = p.get("회차", "")
    m = re.search(r"\d+", no_txt)
    if not m:
        continue
    no = int(m.group())
    meetings.append({
        "no": no,
        "date": p.get("모임 날짜", ""),
        "topic_key": p.get("주제", "").strip(),
        "selected_txt": p.get("선정도서", "").strip(),
        "notice": p.get("공지글", "").strip(),
    })
meetings.sort(key=lambda x: x["no"])

palette_i = 0
today = date.today().isoformat()
for mt in meetings:
    key = mt["topic_key"]
    if key in KNOWN_COLORS:
        mt["cl"], mt["cd"] = KNOWN_COLORS[key]
    else:
        mt["cl"], mt["cd"] = PALETTE[palette_i % len(PALETTE)]
        palette_i += 1
    mt["topic"] = DISPLAY_NAMES.get(key, key)
    mt_books = [b for b in books if re.sub(r"\D", "", b.get("회차", "")) == str(mt["no"])]
    mt["books"] = mt_books
    has_sel = any(classify(b) == "sel" for b in mt_books)
    mt["upcoming"] = (not has_sel) and (not mt["date"] or mt["date"] >= today)

meetings = [m for m in meetings if m["books"]]

# ---------------------------------------------------------------- covers
# 표지는 covers/<rid>.jpg 개별 파일로 저장 (HTML에 내장하지 않음 → 첫 로드 경량화).
# covers_meta.json에 원본 이미지 서명(블록id|첨부id)을 기록해 노션에서 표지를
# 교체하면 자동으로 다시 받는다.
COVERS_DIR = os.path.normpath(os.path.join(ROOT, "..", "covers"))
os.makedirs(COVERS_DIR, exist_ok=True)
META_CACHE = os.path.join(ROOT, "covers_meta.json")
covers_meta = json.load(open(META_CACHE)) if os.path.exists(META_CACHE) else {}

def resize_jpeg(raw):
    """이미지 바이트 → 높이 최대 360px JPEG. Pillow 우선, 없으면 sips(macOS)."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        im = im.convert("RGB")
        w, h = im.size
        scale = min(1.0, 360.0 / max(w, h))
        if scale < 1.0:
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=60)
        return buf.getvalue()
    except ImportError:
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as tf:
            tf.write(raw); tmp = tf.name
        out = tmp + ".jpg"
        try:
            subprocess.run(["sips", "-Z", "360", "-s", "format", "jpeg",
                            "-s", "formatOptions", "60", tmp, "--out", out],
                           capture_output=True, check=True)
            return open(out, "rb").read()
        finally:
            for pth in (tmp, out):
                if os.path.exists(pth):
                    os.unlink(pth)

def first_image_block(page_id):
    d = load_chunk(page_id)
    blocks = {}
    for bid, wrap in d.get("recordMap", {}).get("block", {}).items():
        v = wrap.get("value", {}); v = v.get("value", v)
        if v:
            blocks[bid] = v
    def dfs(bid):
        b = blocks.get(bid)
        if not b:
            return None
        if b.get("type") == "image" and bid != page_id:
            return b
        for cid in b.get("content", []) or []:
            r = dfs(cid)
            if r:
                return r
        return None
    root = blocks.get(page_id)
    if not root:
        return None
    for cid in root.get("content", []) or []:
        r = dfs(cid)
        if r:
            return r
    return None

def notion_retry(fn, *args):
    """429 대비 백오프 재시도."""
    for attempt in range(3):
        try:
            return fn(*args)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(12 * (attempt + 1))
                continue
            raise

new_covers = replaced = 0
valid_rids = set()
for b in books:
    rid = b["_rid"]
    valid_rids.add(rid)
    path = os.path.join(COVERS_DIR, f"{rid}.jpg")
    try:
        img = notion_retry(first_image_block, rid)
        time.sleep(0.35)
        if not img:
            continue
        src = (img.get("properties", {}).get("source") or [[""]])[0][0]
        if not src:
            continue
        sig = img["id"] + "|" + src
        have = os.path.exists(path)
        known = covers_meta.get(rid)
        if have and known == sig:
            continue
        if have and known is None:
            covers_meta[rid] = sig  # 기존 파일을 현재 이미지로 채택
            continue
        url = ("https://www.notion.so/image/" + urllib.parse.quote(src, safe="")
               + "?table=block&id=" + img["id"] + "&cache=v2")
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
        with open(path, "wb") as f:
            f.write(resize_jpeg(raw))
        if have:
            replaced += 1
        else:
            new_covers += 1
        covers_meta[rid] = sig
        time.sleep(0.5)
    except Exception as e:
        print(f"  표지 실패: {b['책 제목']} — {str(e)[:60]}")

# 노션에서 사라진 책의 표지 파일·메타 정리
for fn in os.listdir(COVERS_DIR):
    rid = fn[:-4]
    if fn.endswith(".jpg") and rid not in valid_rids:
        os.unlink(os.path.join(COVERS_DIR, fn))
        covers_meta.pop(rid, None)
json.dump(covers_meta, open(META_CACHE, "w"))

def cover_path(rid):
    return f"covers/{rid}.jpg" if os.path.exists(os.path.join(COVERS_DIR, f"{rid}.jpg")) else None

n_files = len([f for f in os.listdir(COVERS_DIR) if f.endswith(".jpg")])
print(f"표지: 파일 {n_files}권 (신규 {new_covers}, 교체 {replaced})")

# ---------------------------------------------------------------- aladin
ALADIN_CACHE = os.path.join(ROOT, "aladin.json")
aladin = json.load(open(ALADIN_CACHE)) if os.path.exists(ALADIN_CACHE) else {}

def aladin_decode(raw):
    for enc in ("cp949", "utf-8"):
        try:
            txt = raw.decode(enc)
            if "알라딘" in txt:
                return txt
        except UnicodeDecodeError:
            continue
    return raw.decode("cp949", errors="replace")

def aladin_search(q_text, want_title):
    q = urllib.parse.quote(q_text)
    url = f"https://www.aladin.co.kr/search/wsearchresult.aspx?SearchTarget=Book&SearchWord={q}"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        txt = aladin_decode(r.read())
    pairs = re.findall(r'wproduct\.aspx\?ItemId=(\d+)"[^>]*class="bo3"[^>]*>(?:<b>)?([^<]{1,80})', txt)
    nt = norm(want_title)
    for iid, label in pairs:
        nl = norm(label)
        if nt and (nt in nl or nl in nt or nt[:10] in nl):
            return iid, "match"
    if pairs:
        return pairs[0][0], "first"
    return None, "none"

new_aladin = 0
for b in books:
    rid = b["_rid"]
    if rid in aladin:
        continue
    title, author = b["책 제목"].strip(), b.get("저자", "")
    try:
        iid, how = aladin_search(f"{title} {author}".strip(), title)
        if not iid:
            simple = re.split(r"[|—•-]", title)[0].strip()
            iid, how = aladin_search(simple, title)
        aladin[rid] = {"id": iid, "how": how, "title": title}
        new_aladin += 1
        time.sleep(0.7)
    except Exception as e:
        print(f"  알라딘 실패: {title} — {str(e)[:60]}")
if new_aladin:
    json.dump(aladin, open(ALADIN_CACHE, "w"), ensure_ascii=False)
print(f"알라딘: 캐시 {len(aladin)}권 (신규 {new_aladin})")

# ---------------------------------------------------------------- 본문 생성
WEEKDAYS = "월화수목금토일"
def kdate(iso):
    try:
        y, mth, d = map(int, iso.split("-"))
        return f"{y}.{mth:02d}.{d:02d} ({WEEKDAYS[date(y, mth, d).weekday()]})"
    except Exception:
        return iso

REL_ORDER = {"관련성 높음": 0, "": 0, "관련성 중간": 1, "관련성 낮음": 2}
DESCRIPTIONS = {
    norm("제국의 종말 지성의 탄생"): "오스트리아의 지성을 빚어낸 인물로 종주한 결정판",
    norm("비엔나 1900년 삶과 예술 그리고 문화"): "사진과 도판으로 펼쳐 보이는 세기말 빈",
    norm("빈에서는 인생이 아름다워진다"): "풍월당 박종호 선생의 빈 예술 기행",
    norm("1913년 세기의 여름"): "1차세계대전 직전의 한 해를 월별로 구성한 일상의 콜라주",
    norm("여름은 오래 그곳에 남아"): "노년의 건축가와 그의 건축을 존경하고 공감하는 젊은 건축가의 여름 별장에서의 일상을 담담하게 그린, 디테일이 아름다운 소설",
    norm("여름의 빌라"): "그럼에도 불구하고 벌어지는 인생의 면면들 속에서, 언제나 조금 더 사랑 쪽으로 분투하는 당신에게 건네는 조용한 위로",
    norm("두고 온 여름"): "“아무 것도 두고 온 게 없는데 무언가를 잃어버린 듯한 기분” — 한 시절의 여운을 통해, 잊고 지냈던 삶의 어느 계절에 뒤늦은 안부를 전하게 되는 소설",
    norm("첫 여름 완주"): "상처와 슬픔을 끝까지 안고 걸어간 여름, 그 끝에서 다시 관계를 이해하고 나를 회복하는 이야기",
}

groups_html = []
n_sel = n_cand = n_ref = 0
topic_counts = {}
DISPLAY = list(reversed(meetings))  # 최신 회차가 위로

for mt in DISPLAY:
    rows = mt["books"]
    topic_counts[mt["topic"]] = len(rows)
    sel = [b for b in rows if classify(b) == "sel"]
    cand = [b for b in rows if classify(b) == "cand"]
    refs = sorted([b for b in rows if classify(b) == "ref"],
                  key=lambda b: (REL_ORDER.get(b.get("주제 관련성", ""), 0), b["책 제목"]))
    n_sel += len(sel); n_cand += len(cand); n_ref += len(refs)

    def row_html(b, status):
        title = b["책 제목"].strip()
        author = b.get("저자", "")
        quote = b.get("한 줄 평") or DESCRIPTIONS.get(norm(title), "")
        if quote == "단편집":
            quote = ""
        rel = b.get("주제 관련성", "")
        relcls = " relmid" if rel == "관련성 중간" else (" rellow" if rel == "관련성 낮음" else "")
        tags = ""
        if b.get("전자책"):
            tags += f'<span class="tag">{esc(b["전자책"])}</span>'
        if rel in ("관련성 중간", "관련성 낮음"):
            tags += f'<span class="tag trel">{esc(rel[4:])}</span>'
        if b.get("구판 제목"):
            tags += f'<span class="tag told">{esc(b["구판 제목"])}</span>'
        qhtml = f'<p class="bquote">{esc(quote)}</p>' if quote else ""
        st_label = {"sel": "★ 선정", "cand": "후보", "ref": "참고"}[status]
        q_attr = esc(f"{title} {author} {quote}".lower())
        tip = f' title="{esc(quote)}"' if quote else ""
        item = (aladin.get(b["_rid"]) or {}).get("id")
        if item:
            link = f"https://www.aladin.co.kr/shop/wproduct.aspx?ItemId={item}"
        else:
            link = ("https://www.aladin.co.kr/search/wsearchresult.aspx?SearchTarget=Book&SearchWord="
                    + urllib.parse.quote(f"{title} {author}".strip()))
        uri = cover_path(b["_rid"])
        if uri:
            cover = (f'<a class="bcover has-img" href="{link}" target="_blank" rel="noopener" '
                     f'aria-label="{esc(title)} — 알라딘에서 보기"><img class="cv-img" src="{uri}" alt="" loading="lazy"></a>')
        else:
            cover = (f'<a class="bcover" href="{link}" target="_blank" rel="noopener" '
                     f'aria-label="{esc(title)} — 알라딘에서 보기"><span class="cv-rule"></span>'
                     f'<span class="cv-title">{esc(title)}</span><span class="cv-author">{esc(author)}</span></a>')
        return f"""
      <li class="brow {status}{relcls}" data-status="{status}" data-q="{q_attr}"{tip}>
        <span class="st st-{status}">{st_label}</span>
        {cover}
        <div class="binfo">
          <p class="bline"><a class="btitle" href="{link}" target="_blank" rel="noopener">{esc(title)}</a><span class="bauthor">{esc(author)}</span>{tags}</p>
          {qhtml}
        </div>
      </li>"""

    main_html = "".join([row_html(b, "sel") for b in sel] +
                        [row_html(b, "cand") for b in cand])
    refs_html = "".join(row_html(b, "ref") for b in refs)
    ref_block = ""
    if refs:
        ref_block = f"""
    <details class="refbox">
      <summary>참고도서 <span class="count">{len(refs)}권</span><span class="chev" aria-hidden="true"></span></summary>
      <ul class="books">{refs_html}
      </ul>
    </details>"""
    up = ""
    if mt["upcoming"]:
        up = f'<span class="m-up">다가오는 책밤 — 선정도서는 이날 밤에 정해집니다</span>'
    notice_html = ""
    groups_html.append(f"""
  <section class="mgroup" id="m{mt['no']}" data-topic="{esc(mt['topic'])}" style="--cl:{mt['cl']};--cd:{mt['cd']}">
    <header class="mhead">
      <span class="sw" aria-hidden="true"></span>
      <span class="m-badge">제{mt['no']}회</span>
      <h2 class="m-name">{esc(mt['topic'])}</h2>
      <time class="m-date" datetime="{mt['date']}">{kdate(mt['date'])}</time>
      <span class="m-n">{len(rows)}권</span>
      {up}
      {notice_html}
    </header>
    <ul class="books">{main_html}
    </ul>{ref_block}
  </section>""")

total_books = sum(topic_counts.values())

topic_chips = "".join(
    f'<button type="button" class="chip tchip" data-topic="{esc(mt["topic"])}" aria-pressed="true" style="--cl:{mt["cl"]};--cd:{mt["cd"]}">'
    f'<span class="sw"></span>{esc(mt["topic"])}<span class="n">{topic_counts[mt["topic"]]}</span></button>'
    for mt in DISPLAY) + (
    '<button type="button" class="chip reset" id="allTopicsOn">모두 선택</button>'
    '<button type="button" class="chip reset" id="allTopicsOff">모두 해제</button>')

jumps_html = '<span class="lbl">바로가기</span>' + "".join(
    f'<a class="jlink" href="#m{mt["no"]}" style="--cl:{mt["cl"]};--cd:{mt["cd"]}">'
    f'<span class="nav-dot" aria-hidden="true"></span>제{mt["no"]}회 {esc(mt["topic"])}</a>'
    for mt in DISPLAY)

status_chips = (
    f'<button type="button" class="chip gchip" data-status="sel" aria-pressed="true">★ 선정도서 <span class="n">{n_sel}</span></button>'
    f'<button type="button" class="chip gchip" data-status="cand" aria-pressed="true">후보 <span class="n">{n_cand}</span></button>'
    f'<button type="button" class="chip gchip" data-status="ref" aria-pressed="true">참고도서 <span class="n">{n_ref}</span></button>'
    '<button type="button" class="chip reset" id="allStatusOn">모두 선택</button>'
    '<button type="button" class="chip reset" id="allStatusOff">모두 해제</button>'
    f'<span class="count" id="countLbl"></span>')

# ---------------------------------------------------------------- 폰트 서브셋
from fontTools import subset as ftsubset
serif_text = "백북스 시즌4, 책밤 취향이 만나는" + "".join(mt["topic"] for mt in meetings)
chars = "".join(sorted(set(serif_text.replace(" ", ""))))
opts = ftsubset.Options()
opts.flavor = "woff"
font = ftsubset.load_font(os.path.join(ROOT, "GowunBatang-Bold.ttf"), opts)
ss = ftsubset.Subsetter(opts)
ss.populate(text=chars)
ss.subset(font)
buf = io.BytesIO()
font.save(buf)
font_b64 = base64.b64encode(buf.getvalue()).decode()

# ---------------------------------------------------------------- HTML
page = f"""<title>백북스 시즌4, 책밤</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%20100%20100'%3E%3Ctext%20y='0.9em'%20font-size='90'%3E%F0%9F%93%96%3C/text%3E%3C/svg%3E">
<style>
@font-face {{
  font-family: "GowunBatang";
  src: url(data:font/woff;base64,{font_b64}) format("woff");
  font-weight: 700;
  font-display: swap;
}}
:root {{
  --surface: #fcfcfb;
  --page: #f9f9f7;
  --ink: #0b0b0b;
  --ink2: #52514e;
  --muted: #898781;
  --grid: #e1e0d9;
  --border: rgba(11,11,11,0.10);
  --star: #b45309;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --surface: #1a1a19; --page: #0d0d0d; --ink: #ffffff; --ink2: #c3c2b7;
    --muted: #898781; --grid: #2c2c2a; --border: rgba(255,255,255,0.10);
    --star: #fbbf24;
  }}
  .mgroup, .tchip {{ --c: var(--cd); }}
}}
:root[data-theme="dark"] {{
  --surface: #1a1a19; --page: #0d0d0d; --ink: #ffffff; --ink2: #c3c2b7;
  --muted: #898781; --grid: #2c2c2a; --border: rgba(255,255,255,0.10);
  --star: #fbbf24;
}}
:root[data-theme="dark"] .mgroup, :root[data-theme="dark"] .tchip {{ --c: var(--cd); }}
:root[data-theme="light"] {{
  --surface: #fcfcfb; --page: #f9f9f7; --ink: #0b0b0b; --ink2: #52514e;
  --muted: #898781; --grid: #e1e0d9; --border: rgba(11,11,11,0.10);
  --star: #b45309;
}}
:root[data-theme="light"] .mgroup, :root[data-theme="light"] .tchip {{ --c: var(--cl); }}
.mgroup, .tchip {{ --c: var(--cl); }}

* {{ box-sizing: border-box; }}
html, body {{ margin: 0; }}
html {{ scroll-behavior: smooth; }}
@media (prefers-reduced-motion: reduce) {{ html {{ scroll-behavior: auto; }} }}
body {{
  background: var(--page); color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", "Apple SD Gothic Neo", "Malgun Gothic", sans-serif;
  font-size: 14px; line-height: 1.5;
}}
.wrap {{ max-width: 1080px; margin: 0 auto; padding: 24px 24px 56px; position: relative; }}

header.page-head {{ margin-bottom: 12px; }}
.top-actions {{
  position: absolute; top: 20px; right: 24px; z-index: 5;
  display: flex; gap: 8px; align-items: center;
}}
#searchBox {{
  border: 1px solid var(--border); background: var(--surface); color: var(--ink);
  border-radius: 999px; padding: 5px 13px; font-size: 12.5px; font-family: inherit;
  width: 150px;
}}
#searchBox:focus-visible {{ outline: 2px solid var(--star); outline-offset: 1px; }}
.eyebrow {{
  font-size: 12px; letter-spacing: 0.12em; text-transform: uppercase;
  color: var(--muted); margin: 0 0 6px;
}}
h1 {{
  font-family: "GowunBatang", "Noto Serif KR", "Nanum Myeongjo", "AppleMyungjo", Georgia, serif;
  letter-spacing: 0.015em;
  font-size: clamp(30px, 4.6vw, 46px);
  font-weight: 700; margin: 0 0 8px; text-wrap: balance; line-height: 1.22;
}}
.sub {{ color: var(--ink2); font-size: 13.5px; margin: 0; max-width: 72ch; }}
.view-switch {{ display: inline-flex; gap: 4px; }}
.vchip[aria-pressed="true"] {{
  color: var(--ink); font-weight: 600;
  background: color-mix(in srgb, var(--ink) 8%, var(--surface));
}}
.vchip[aria-pressed="false"] {{ color: var(--ink2); opacity: 1; }}
.chip.reset {{ color: var(--ink2); }}

details.about {{ margin: 10px 0 0; font-size: 13px; color: var(--ink2); max-width: 74ch; }}
details.about summary {{ cursor: pointer; color: var(--muted); user-select: none; }}
details.about p {{ margin: 8px 0; }}

.controls {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 14px 0 8px; }}
.chip {{
  display: inline-flex; align-items: center; gap: 7px;
  border: 1px solid var(--border); background: var(--surface);
  color: var(--ink); border-radius: 999px; padding: 4px 10px 4px 8px;
  font-size: 12.5px; cursor: pointer; font-family: inherit;
}}
.chip .sw {{ width: 10px; height: 10px; border-radius: 3px; background: var(--c); }}
.chip .n {{ color: var(--muted); font-variant-numeric: tabular-nums; }}
.chip[aria-pressed="false"] {{ opacity: 0.38; }}
.chip:hover {{ border-color: var(--muted); }}
.chip:focus-visible {{ outline: 2px solid var(--star); outline-offset: 2px; }}
.controls.status-row {{ margin-top: 0; margin-bottom: 14px; gap: 6px; }}
.gchip {{ padding: 4px 9px; }}
.gchip[aria-pressed="false"] {{ opacity: 0.38; }}
.lbl {{ font-size: 12px; color: var(--muted); margin-right: 2px; }}
.count {{ font-size: 12px; color: var(--muted); margin-left: auto; font-variant-numeric: tabular-nums; }}

/* ---- 바로가기 ---- */
.jumps {{ display: flex; flex-wrap: wrap; gap: 6px 16px; align-items: center; margin: 0 0 16px; }}
.jumps .lbl {{ font-size: 12px; color: var(--muted); }}
.jlink {{
  display: inline-flex; align-items: center; gap: 7px;
  color: var(--ink2); text-decoration: none; font-size: 12.5px; padding: 3px 2px;
  --c: var(--cl);
}}
.jlink:hover {{ color: var(--c); }}
.jlink:focus-visible {{ outline: 2px solid var(--c); outline-offset: 2px; border-radius: 2px; }}
.nav-dot {{ width: 9px; height: 9px; border-radius: 50%; background: var(--c); flex: none; }}
@media (prefers-color-scheme: dark) {{ .jlink {{ --c: var(--cd); }} }}
:root[data-theme="dark"] .jlink {{ --c: var(--cd); }}
:root[data-theme="light"] .jlink {{ --c: var(--cl); }}
.mgroup {{ scroll-margin-top: 16px; }}

/* ---- board ---- */
.mgroup {{
  background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
  border-top: 4px solid var(--c);
  margin-bottom: 30px; overflow: hidden;
}}
.mhead {{
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  padding: 15px 18px 13px;
  border-bottom: 1px solid var(--grid);
  background: color-mix(in srgb, var(--c) 11%, var(--surface));
}}
.mhead .sw {{ display: none; }}
.m-badge {{
  font-size: 12.5px; color: var(--surface); font-weight: 700; line-height: 1;
  background: var(--c); border-radius: 999px; padding: 5px 12px;
  font-variant-numeric: tabular-nums; letter-spacing: 0.04em;
}}
.m-name {{
  font-family: "GowunBatang", "Noto Serif KR", "Nanum Myeongjo", "AppleMyungjo", Georgia, serif;
  font-size: 25px; font-weight: 700; margin: 0; line-height: 1.2;
}}
.m-date {{ font-size: 13px; color: var(--ink2); font-variant-numeric: tabular-nums; }}
.m-n {{ font-size: 13px; color: var(--muted); font-variant-numeric: tabular-nums; margin-left: auto; }}
.m-up {{ font-size: 12.5px; color: var(--star); flex-basis: 100%; }}
.m-notice {{
  flex-basis: 100%; margin: 4px 0 0; font-size: 13px; color: var(--ink2);
  line-height: 1.65; white-space: pre-line; max-width: 74ch;
}}
details.m-noticebox {{ flex-basis: 100%; margin: 2px 0 0; }}
details.m-noticebox summary {{
  cursor: pointer; font-size: 12px; color: var(--muted); user-select: none;
}}
details.m-noticebox summary:hover {{ color: var(--ink2); }}

/* ---- 참고도서 접기 ---- */
details.refbox {{ border-top: 1px solid var(--grid); }}
details.refbox summary {{
  cursor: pointer; list-style: none; display: flex; align-items: center; gap: 8px;
  padding: 10px 16px; font-size: 12px; font-weight: 600; letter-spacing: 0.04em;
  color: var(--muted); user-select: none;
}}
details.refbox summary::-webkit-details-marker {{ display: none; }}
details.refbox summary:hover {{ color: var(--ink2); }}
details.refbox summary:focus-visible {{ outline: 2px solid var(--c); outline-offset: -2px; }}
details.refbox .count {{ margin-left: 0; }}
.chev {{
  width: 7px; height: 7px; margin-left: 2px;
  border-right: 1.5px solid currentColor; border-bottom: 1.5px solid currentColor;
  transform: rotate(45deg) translateY(-1px);
}}
details.refbox[open] .chev {{ transform: rotate(-135deg) translateY(1px); }}
details.refbox > .books {{ border-top: 1px solid color-mix(in srgb, var(--grid) 55%, transparent); }}

.books {{ list-style: none; margin: 0; padding: 0; }}
.brow {{
  display: flex; gap: 12px; align-items: flex-start;
  padding: 8px 16px;
  border-bottom: 1px solid color-mix(in srgb, var(--grid) 55%, transparent);
}}
.brow:last-child {{ border-bottom: none; }}
.brow:hover {{ background: color-mix(in srgb, var(--grid) 25%, transparent); }}
.st {{
  flex: 0 0 58px; text-align: right; font-size: 11px; padding-top: 2px;
  color: var(--muted); font-weight: 600; letter-spacing: 0.03em; white-space: nowrap;
}}
.st-sel {{ color: var(--star); }}
.st-cand {{ color: var(--c); }}
.brow.sel {{
  background: color-mix(in srgb, var(--c) 8%, var(--surface));
  box-shadow: inset 3px 0 0 var(--c);
}}
.brow.sel .btitle {{ font-size: 14.5px; }}
.binfo {{ min-width: 0; }}
.bline {{ margin: 0; display: flex; flex-wrap: wrap; align-items: baseline; gap: 3px 8px; }}
.btitle {{ font-size: 13.5px; font-weight: 600; color: inherit; text-decoration: none; }}
.btitle:hover {{ text-decoration: underline; text-underline-offset: 3px; color: var(--c); }}
.btitle:focus-visible {{ outline: 2px solid var(--c); outline-offset: 2px; border-radius: 2px; }}
.bcover:focus-visible {{ outline: 2px solid var(--c); outline-offset: 3px; }}
.bauthor {{ font-size: 12px; color: var(--ink2); }}
.tag {{
  font-size: 10.5px; color: var(--muted); border: 1px solid var(--border);
  border-radius: 999px; padding: 0 7px; white-space: nowrap;
}}
.tag.told {{ border: none; padding: 0; }}
.bquote {{ margin: 2px 0 0; font-size: 12.5px; color: var(--ink2); line-height: 1.55; max-width: 68ch; }}
.brow.relmid {{ opacity: 0.78; }}
.brow.rellow {{ opacity: 0.55; }}
.brow.hit {{ opacity: 1; }}
.brow.hit .binfo {{
  box-shadow: inset 3px 0 0 var(--c);
  padding-left: 9px; margin-left: -12px;
}}
.brow.dim {{ opacity: 0.25; }}
.mgroup.empty {{ display: none; }}

/* ---- 표지 (목록 뷰에서는 숨김) ---- */
.bcover {{ display: none; }}

/* ---- 카드 뷰 ---- */
body[data-view="card"] .books {{
  display: grid; grid-template-columns: repeat(auto-fill, minmax(148px, 1fr));
  gap: 22px 16px; padding: 16px;
}}
body[data-view="card"] .brow {{
  flex-direction: column; gap: 8px; padding: 0;
  border-bottom: none; position: relative;
}}
body[data-view="card"] .brow:hover {{ background: transparent; }}
body[data-view="card"] .bcover {{
  display: block; position: relative; aspect-ratio: 0.65; width: 100%;
  border-radius: 3px 7px 7px 3px; overflow: hidden;
  background: linear-gradient(160deg,
    color-mix(in srgb, var(--c) 86%, var(--surface)),
    color-mix(in srgb, var(--c) 62%, #16161a));
  box-shadow: 0 2px 7px rgba(0,0,0,0.22);
}}
.cv-img {{ position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; display: block; }}
.cv-rule {{
  position: absolute; top: 13px; left: 12px; right: 12px; height: 4px;
  border-top: 1px solid rgba(255,255,255,0.55); border-bottom: 1px solid rgba(255,255,255,0.55);
}}
.cv-title {{
  position: absolute; top: 28px; left: 12px; right: 12px;
  font-family: "Noto Serif KR", "Nanum Myeongjo", "AppleMyungjo", serif;
  font-weight: 700; font-size: 13.5px; line-height: 1.45; color: #fdfcf8;
  overflow-wrap: anywhere;
}}
.cv-author {{
  position: absolute; bottom: 10px; left: 12px; right: 12px;
  font-size: 10.5px; color: rgba(253,252,248,0.85);
}}
body[data-view="card"] .st {{
  position: absolute; top: 7px; right: 7px; z-index: 1; flex: none;
  background: color-mix(in srgb, var(--surface) 88%, transparent);
  border-radius: 999px; padding: 2px 8px; font-size: 10.5px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.18);
}}
body[data-view="card"] .st-ref {{ display: none; }}
body[data-view="card"] .brow.sel {{ box-shadow: none; background: transparent; }}
body[data-view="card"] .brow.sel .bcover {{ outline: 2.5px solid var(--star); outline-offset: 2px; }}
body[data-view="card"] .bline {{ flex-direction: column; align-items: flex-start; gap: 1px; }}
body[data-view="card"] .btitle {{ font-size: 12.5px; line-height: 1.35; }}
body[data-view="card"] .bauthor {{ font-size: 11px; color: var(--muted); }}
body[data-view="card"] .bquote, body[data-view="card"] .tag {{ display: none; }}
body[data-view="card"] .brow.hit .binfo {{ box-shadow: none; padding-left: 0; margin-left: 0; }}
body[data-view="card"] .brow.hit .bcover {{ outline: 3px solid var(--c); outline-offset: 2px; }}

.upd-status {{
  display: block; min-height: 16px; margin: 2px 0 0;
  font-size: 12px; color: var(--star); text-align: right;
}}
#tokenDlg {{
  max-width: 420px; border: 1px solid var(--border); border-radius: 12px;
  background: var(--surface); color: var(--ink); padding: 20px 22px;
  font-size: 13px; line-height: 1.6;
}}
#tokenDlg::backdrop {{ background: rgba(0,0,0,0.45); }}
#tokenDlg h3 {{ margin: 0 0 8px; font-size: 15px; }}
#tokenDlg p {{ margin: 0 0 10px; color: var(--ink2); }}
#tokenDlg ol {{ margin: 0 0 12px; padding-left: 18px; color: var(--ink2); }}
#tokenDlg a {{ color: var(--star); }}
#tokenInput {{
  width: 100%; border: 1px solid var(--border); background: var(--page); color: var(--ink);
  border-radius: 8px; padding: 7px 11px; font-size: 12.5px; font-family: inherit;
  margin-bottom: 12px;
}}
.dlg-actions {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
.dlg-alt {{ font-size: 11.5px; margin-left: auto; }}
.credit {{
  margin: 18px 0 0; padding-top: 12px; font-size: 11.5px; color: var(--muted);
  border-top: 1px solid var(--grid); letter-spacing: 0.02em;
}}
.credit b {{ color: var(--ink2); font-weight: 600; }}
.credit-ig {{ color: var(--muted); text-decoration: none; }}
.credit-ig:hover {{ color: var(--ink2); text-decoration: underline; text-underline-offset: 3px; }}

@media (max-width: 640px) {{
  .wrap {{ padding: 14px 12px 40px; }}
  .top-actions {{ position: static; margin: 0 0 10px; display: flex; }}
  #searchBox {{ flex: 1 1 110px; min-width: 0; }}
  .controls, .jumps {{
    flex-wrap: nowrap; overflow-x: auto; -webkit-overflow-scrolling: touch;
    scrollbar-width: none;
  }}
  .controls::-webkit-scrollbar, .jumps::-webkit-scrollbar {{ display: none; }}
  .chip, .lbl, .jlink {{ white-space: nowrap; flex: 0 0 auto; }}
  .count {{ flex: 0 0 auto; margin-left: 8px; }}
  .st {{ flex-basis: 44px; }}
  .m-n {{ display: none; }}
}}
</style>

<div class="wrap">
  <header class="page-head">
    <div class="top-actions">
      <input type="search" id="searchBox" placeholder="책·저자 검색" autocomplete="off">
      <div class="view-switch" role="group" aria-label="보기 방식">
        <button type="button" class="chip vchip" id="viewList" aria-pressed="false">목록</button>
        <button type="button" class="chip vchip" id="viewCard" aria-pressed="true">카드</button>
      </div>
      <button type="button" class="chip" id="updBtn" title="노션에서 최신 내용 가져오기">↻ 업데이트</button>
      <button type="button" class="chip" id="themeBtn" title="테마">◐</button>
    </div>
    <span class="upd-status" id="updStatus" role="status"></span>
    <p class="eyebrow">100BOOKS · Season 4</p>
    <h1>백북스 시즌4, 책밤</h1>
    <p class="sub">{len(meetings)}번의 책밤에 오른 {total_books}권 — 선정도서 ★{n_sel}권, 후보 {n_cand}권, 참고도서 {n_ref}권.</p>
    <details class="about">
      <summary>백북스 시즌4 책밤을 기획하며</summary>
      <p>24년 전, 우리는 세상을 알기 위해 책을 폈습니다. 하지만 이제는 클릭 한 번이면 책 한 권이 세 줄로 요약되고 AI가 정답을 말하는 시대가 되었습니다. 이제 우리는 AI가 결코 가질 수 없는 것을 나누기 위해 모입니다. ‘무언가를 지극히 좋아하는 마음’과 ‘소소한 덕질’에 대해 이야기합니다.</p>
      <p>우리는 “책을 통해 취향을 나누는 사람들”입니다. “무엇을 읽었는가”보다 중요한 것은 “누가 읽었는가”입니다. 모임 전에 주제를 정하고, 후보를 최대 4권 테이블에 올린 뒤, 멤버들 모두 책밤 에디터가 되어 한 권의 선정도서를 정합니다. 당신의 취향이 우리의 세계가 됩니다.</p>
    </details>
  </header>

  <div class="controls" id="topicChips">{topic_chips}</div>
  <div class="controls status-row" id="statusChips">{status_chips}</div>
  <nav class="jumps" aria-label="회차 바로가기">{jumps_html}</nav>
{"".join(groups_html)}

  <p class="credit"><b>백북스 〈취향이 만나는 책밤〉 아카이브</b> · <a class="credit-ig" href="https://www.instagram.com/100books_bookery_night" target="_blank" rel="noopener">@100books_bookery_night</a></p>
</div>

<dialog id="tokenDlg">
  <h3>업데이트 권한 등록</h3>
  <p>노션의 최신 내용을 가져오려면 GitHub 저장소의 갱신 작업을 실행할 권한이 필요합니다.
  아래에서 만든 토큰을 한 번만 등록하면, 이후엔 버튼 한 번으로 업데이트됩니다.
  토큰은 이 브라우저에만 저장됩니다.</p>
  <ol>
    <li><a href="https://github.com/settings/personal-access-tokens/new" target="_blank" rel="noopener">GitHub에서 토큰 만들기</a>
    — Repository access: <b>100books_bookery_night</b>만 선택, Permissions에서 <b>Actions: Read and write</b></li>
    <li>생성된 토큰을 붙여넣기</li>
  </ol>
  <input type="password" id="tokenInput" placeholder="github_pat_..." autocomplete="off">
  <div class="dlg-actions">
    <button type="button" class="chip" id="tokenSave">저장하고 업데이트</button>
    <button type="button" class="chip" id="tokenCancel">취소</button>
    <a class="dlg-alt" href="https://github.com/jayreyafterdawn/100books_bookery_night/actions/workflows/update.yml" target="_blank" rel="noopener">토큰 없이 GitHub에서 직접 실행 →</a>
  </div>
</dialog>

<script>
(function () {{
  var rows = Array.prototype.slice.call(document.querySelectorAll('.brow'));
  var groups = Array.prototype.slice.call(document.querySelectorAll('.mgroup'));
  var tchips = Array.prototype.slice.call(document.querySelectorAll('.tchip'));
  var gchips = Array.prototype.slice.call(document.querySelectorAll('.gchip'));
  var box = document.getElementById('searchBox');
  var countLbl = document.getElementById('countLbl');

  function pressed(btn) {{ return btn.getAttribute('aria-pressed') === 'true'; }}

  function apply() {{
    var topicsOn = {{}};
    tchips.forEach(function (c) {{ topicsOn[c.dataset.topic] = pressed(c); }});
    var statusOn = {{}};
    gchips.forEach(function (c) {{ statusOn[c.dataset.status] = pressed(c); }});
    var q = box.value.trim().toLowerCase();
    var shown = 0, hits = 0;
    groups.forEach(function (g) {{
      var gOn = topicsOn[g.dataset.topic];
      var vis = 0, refVis = 0;
      Array.prototype.forEach.call(g.querySelectorAll('.brow'), function (r) {{
        var on = gOn && statusOn[r.dataset.status];
        var hit = q && r.dataset.q.indexOf(q) !== -1;
        var show = on && (!q || hit);
        r.style.display = show ? '' : 'none';
        r.classList.toggle('hit', !!(show && q));
        if (show) {{
          shown++; vis++;
          if (r.dataset.status === 'ref') {{ refVis++; }}
        }}
        if (on && hit) {{ hits++; }}
      }});
      var rb = g.querySelector('details.refbox');
      if (rb) {{
        rb.style.display = refVis > 0 ? '' : 'none';
        if (q) {{ rb.open = true; }}
      }}
      g.classList.toggle('empty', vis === 0);
    }});
    countLbl.textContent = q ? hits + '권 일치' : shown + '권 표시 중';
  }}

  tchips.concat(gchips).forEach(function (c) {{
    c.addEventListener('click', function () {{
      c.setAttribute('aria-pressed', pressed(c) ? 'false' : 'true');
      apply();
    }});
  }});
  box.addEventListener('input', apply);

  function setAll(list, on) {{
    list.forEach(function (c) {{ c.setAttribute('aria-pressed', on ? 'true' : 'false'); }});
    apply();
  }}
  document.getElementById('allTopicsOn').addEventListener('click', function () {{ setAll(tchips, true); }});
  document.getElementById('allTopicsOff').addEventListener('click', function () {{ setAll(tchips, false); }});
  document.getElementById('allStatusOn').addEventListener('click', function () {{ setAll(gchips, true); }});
  document.getElementById('allStatusOff').addEventListener('click', function () {{ setAll(gchips, false); }});

  var vList = document.getElementById('viewList');
  var vCard = document.getElementById('viewCard');
  function setView(v) {{
    document.body.setAttribute('data-view', v);
    vList.setAttribute('aria-pressed', v === 'list' ? 'true' : 'false');
    vCard.setAttribute('aria-pressed', v === 'card' ? 'true' : 'false');
  }}
  vList.addEventListener('click', function () {{ setView('list'); }});
  vCard.addEventListener('click', function () {{ setView('card'); }});
  setView('card');

  var themes = ['', 'light', 'dark'];
  var ti = 0;
  document.getElementById('themeBtn').addEventListener('click', function () {{
    ti = (ti + 1) % themes.length;
    var t = themes[ti];
    if (t) {{ document.documentElement.setAttribute('data-theme', t); }}
    else {{ document.documentElement.removeAttribute('data-theme'); }}
  }});

  // ---- 노션 → 웹페이지 수동 업데이트 (GitHub Actions 원격 실행) ----
  var REPO = 'jayreyafterdawn/100books_bookery_night';
  var WORKFLOW = 'update.yml';
  var updBtn = document.getElementById('updBtn');
  var updSt = document.getElementById('updStatus');
  var dlg = document.getElementById('tokenDlg');
  function say(msg) {{ updSt.textContent = msg; }}

  function pollRun(startedAt) {{
    var timer = setInterval(function () {{
      fetch('https://api.github.com/repos/' + REPO + '/actions/runs?per_page=1&event=workflow_dispatch')
        .then(function (r) {{ return r.json(); }})
        .then(function (d) {{
          var run = d.workflow_runs && d.workflow_runs[0];
          if (!run || new Date(run.created_at).getTime() < startedAt - 60000) {{ return; }}
          if (run.status === 'completed') {{
            clearInterval(timer);
            if (run.conclusion === 'success') {{
              say('갱신 완료 — 배포 반영을 기다렸다 새로고침합니다 (약 1분)');
              setTimeout(function () {{ location.reload(); }}, 60000);
            }} else {{
              say('실행 실패: ' + run.conclusion + ' — GitHub Actions 탭을 확인하세요');
            }}
          }} else {{
            say('노션에서 가져오는 중... (' + run.status + ')');
          }}
          if (Date.now() - startedAt > 10 * 60 * 1000) {{
            clearInterval(timer);
            say('시간 초과 — GitHub Actions 탭을 확인하세요');
          }}
        }})
        .catch(function () {{}});
    }}, 15000);
  }}

  function trigger(token) {{
    say('업데이트 요청 중...');
    updBtn.disabled = true;
    fetch('https://api.github.com/repos/' + REPO + '/actions/workflows/' + WORKFLOW + '/dispatches', {{
      method: 'POST',
      headers: {{
        'Authorization': 'Bearer ' + token,
        'Accept': 'application/vnd.github+json',
        'Content-Type': 'application/json'
      }},
      body: JSON.stringify({{ ref: 'main' }})
    }}).then(function (r) {{
      updBtn.disabled = false;
      if (r.status === 204) {{
        say('실행됨 — 노션에서 가져오는 중...');
        pollRun(Date.now());
      }} else if (r.status === 401 || r.status === 403) {{
        localStorage.removeItem('ghUpdateToken');
        say('토큰이 유효하지 않습니다 — 버튼을 다시 눌러 재등록하세요');
      }} else {{
        say('요청 실패: HTTP ' + r.status);
      }}
    }}).catch(function () {{
      updBtn.disabled = false;
      say('네트워크 오류 — 잠시 후 다시 시도하세요');
    }});
  }}

  updBtn.addEventListener('click', function () {{
    var tok = localStorage.getItem('ghUpdateToken');
    if (tok) {{ trigger(tok); }}
    else {{ dlg.showModal(); }}
  }});
  document.getElementById('tokenSave').addEventListener('click', function () {{
    var v = document.getElementById('tokenInput').value.trim();
    if (!v) {{ return; }}
    localStorage.setItem('ghUpdateToken', v);
    document.getElementById('tokenInput').value = '';
    dlg.close();
    trigger(v);
  }});
  document.getElementById('tokenCancel').addEventListener('click', function () {{ dlg.close(); }});

  apply();
}})();
</script>
"""

og = """<meta property="og:type" content="website">
<meta property="og:title" content="백북스 시즌4, 책밤">
<meta property="og:description" content="취향이 만나는 책밤 — 선정도서·후보·참고목록 아카이브. 주제·상태 필터와 검색, 실물 표지 카드 뷰.">
<meta property="og:url" content="https://jayreyafterdawn.github.io/100books_bookery_night/">
<meta property="og:image" content="https://jayreyafterdawn.github.io/100books_bookery_night/og-image.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:locale" content="ko_KR">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="백북스 시즌4, 책밤">
<meta name="twitter:image" content="https://jayreyafterdawn.github.io/100books_bookery_night/og-image.png">
"""
full = '<!doctype html>\n<html lang="ko">\n<head>\n<meta charset="utf-8">\n' + og + page + '\n</html>\n'
with open(OUT, "w") as f:
    f.write(full)
print(f"빌드 완료: {OUT} ({len(full)} bytes) — 모임 {len(meetings)}, 책 {total_books}권")

# ---------------------------------------------------------------- og-image
# SNS 공유 썸네일: 최신 회차 우선으로 선정도서 + 후보 표지 10권을 책장처럼 배치.
# 매 빌드마다 재생성하므로 새 회차가 쌓이면 썸네일도 함께 갱신된다.
def make_og_image():
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageFilter
    except ImportError:
        print("og-image: Pillow 없음 — 건너뜀 (기존 이미지 유지)")
        return
    W, H = 1200, 630
    BG = (13, 13, 13)
    INK = (233, 229, 216)
    MUTED = (137, 135, 129)
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    shelf_ids = []
    for status in ("sel", "cand"):
        for mt in DISPLAY:
            for b in mt["books"]:
                if classify(b) == status and cover_path(b["_rid"]):
                    shelf_ids.append(b["_rid"])
    shelf_ids = shelf_ids[:10]

    cw, ch = 150, 218
    gap = 14
    total_w = len(shelf_ids) * cw + (len(shelf_ids) - 1) * gap
    x = (W - total_w) // 2
    y = H - ch - 48
    for rid in shelf_ids:
        cv = Image.open(os.path.join(COVERS_DIR, f"{rid}.jpg")).convert("RGB")
        scale = max(cw / cv.width, ch / cv.height)
        cv = cv.resize((int(cv.width * scale) + 1, int(cv.height * scale) + 1), Image.LANCZOS)
        left = (cv.width - cw) // 2
        top = (cv.height - ch) // 2
        cv = cv.crop((left, top, left + cw, top + ch))
        sh = Image.new("RGBA", (cw + 24, ch + 24), (0, 0, 0, 0))
        ImageDraw.Draw(sh).rounded_rectangle([12, 14, cw + 12, ch + 16], 6, fill=(0, 0, 0, 160))
        sh = sh.filter(ImageFilter.GaussianBlur(7))
        img.paste(Image.new("RGB", sh.size, BG), (x - 12, y - 12), sh)
        img.paste(cv, (x, y))
        x += cw + gap

    draw.rectangle([60, H - 40, W - 60, H - 36], fill=(51, 55, 72))

    font_path = os.path.join(ROOT, "GowunBatang-Bold.ttf")
    font_title = ImageFont.truetype(font_path, 84)
    font_sub = ImageFont.truetype(font_path, 30)
    title = "백북스 시즌4, 책밤"
    tw = draw.textlength(title, font=font_title)
    draw.text(((W - tw) / 2, 96), title, font=font_title, fill=INK)
    sub = "취향이 만나는 책밤 — 선정도서 아카이브"
    sw = draw.textlength(sub, font=font_sub)
    draw.text(((W - sw) / 2, 214), sub, font=font_sub, fill=MUTED)

    colors = [(mt["cl"]) for mt in DISPLAY][:6]
    seg_w = 300 // max(1, len(colors))
    sx = (W - seg_w * len(colors)) // 2
    for i, c in enumerate(colors):
        rgb = tuple(int(c[j:j + 2], 16) for j in (1, 3, 5))
        draw.rectangle([sx + i * seg_w, 278, sx + (i + 1) * seg_w - 2, 283], fill=rgb)

    out_png = os.path.normpath(os.path.join(ROOT, "..", "og-image.png"))
    img.save(out_png, "PNG", optimize=True)
    print(f"og-image 갱신: {len(shelf_ids)}권 표지 사용")

make_og_image()
