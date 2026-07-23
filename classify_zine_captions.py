"""
classify_zine_captions.py
============================================================
collect_zine_articles.py で集めた記事URL一覧を読み込み、各記事本文から
「〜氏」で終わるキャプション行（登場人物の紹介文）を抽出し、

  ・regex_ok  : 会社名(法人格あり)＋部署役職＋氏名＋氏 の定型パターンで
                機械的に会社名/部署役職/氏名を分解できるもの
  ・needs_ai  : 文章の一部になっている、会社名に法人格が付かない、
                ふりがな入りなど、正規表現では安全に分解できないもの

に分類する。AIコストなしで処理できる件数と、Claude APIが必要な件数を
それぞれ集計する。

使い方:
    python classify_zine_captions.py --input zine_articles.csv \
        --output zine_captions.csv --limit 200
============================================================
"""

import argparse
import csv
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}
REQUEST_TIMEOUT = 20

COMPANY_SUFFIX_RE = re.compile(r"(株式会社|合同会社|合資会社|有限会社|Inc\.?|Ltd\.?|Corporation|LLC)")
INSTITUTION_SUFFIX_RE = re.compile(r"(大学院|大学|研究所|研究科|協会|財団|機構|省|庁)")
ENTITY_SUFFIX_RE = re.compile(f"({COMPANY_SUFFIX_RE.pattern}|{INSTITUTION_SUFFIX_RE.pattern})")

# 行末が「氏」で終わり、直前が「姓 名」のように半角/全角スペースで区切られた
# 2〜6文字の漢字・かな塊であるものを名前候補とみなす。
# ふりがなや「の」を挟むパターン(文章的キャプション)は別途弾く。
NAME_TAIL_RE = re.compile(
    r"(?<![一-龥々ヶA-Za-z0-9])(?P<name>[一-龥々ヶ]{1,4}[ \u3000][一-龥々ヶぁ-んー]{1,6})氏$"
)
SINGLE_NAME_RE = re.compile(r"(?P<name>[一-龥々ヶぁ-んー]{2,6})氏$")

# 「姓 名氏」の直前が実は役職語だった場合（例:「教授 江崎浩氏」）に
# 氏名を誤って役職ごと取り込まないよう、役職語リストと照合する
ROLE_WORDS = {
    "教授", "准教授", "講師", "助教", "特任教授",
    "部長", "課長", "次長", "係長", "主任", "室長", "所長",
    "学長", "副学長", "学部長", "研究科長",
    "理事", "理事長", "会長", "副会長", "社長", "副社長",
    "専務", "常務", "執行役員", "代表", "代表取締役", "取締役",
    "顧問", "参与", "本部長", "支社長", "支店長", "工場長", "部門長",
    "マネージャー", "ゼネラルマネージャー", "マネジャー", "ディレクター",
    "リーダー", "グループマネージャー",
    "CEO", "CTO", "CFO", "COO", "CPO", "CMO", "CIO", "VP",
}

# 直前に「の」がある、または全角/半角括弧（ふりがな等）が氏の直前にあるものは
# 文章的キャプション（needs_ai）とみなす
NARRATIVE_SIGNAL_RE = re.compile(r"(の|[（(][^）)]*[）)])氏$")

MIN_CAPTION_LEN = 6
MAX_CAPTION_LEN = 120

SINCE_DATE = None  # main()内で --since から設定される（YYYY-MM-DD文字列として比較）

# 明らかにキャプションでない「〜氏」の誤検出を除外するための語
FALSE_POSITIVE_TERMS = {"同氏", "両氏", "各氏", "前述の氏", "氏"}


def fetch_html(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return resp.text


def extract_publish_date(html: str) -> str:
    """
    記事ヘッダーの <time datetime="YYYY/MM/DD HH:MM" itemprop="datepublished"> から
    公開日を取得する（実際の生HTMLで確認済みの構造。以前試したcxenseparse系metaタグは
    実在しなかったため廃止）。datetime/itempropの属性順どちらにも対応する。
    """
    m = re.search(
        r'<time\b[^>]*itemprop=["\']datepublished["\'][^>]*datetime=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    )
    if not m:
        m = re.search(
            r'<time\b[^>]*datetime=["\']([^"\']+)["\'][^>]*itemprop=["\']datepublished["\']',
            html, re.IGNORECASE,
        )
    if not m:
        return ""
    raw = m.group(1)
    dm = re.match(r"(\d{4})/(\d{2})/(\d{2})", raw)
    if dm:
        return f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}"
    dm2 = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)  # ISO形式で来た場合の保険
    if dm2:
        return f"{dm2.group(1)}-{dm2.group(2)}-{dm2.group(3)}"
    return ""


def extract_client_name(html: str) -> str:
    """
    タイアップ記事のクライアント企業名を取得する試み。
    以前想定していたcxenseparse:sho-clientnameというmetaタグは実際のHTMLには
    存在しないことが判明したため、現状は取得しない（空文字を返す）。
    """
    return ""


PHOTO_DIRECTION_RE = re.compile(r"^[（(](写真)?(右から|左から|右|左|中央|前列|後列)[^）)]*[）)]\s*")


def split_company_and_dept(info: str):
    """「会社名 部署 役職」の文字列を会社名とそれ以外に分割する。"""
    info = PHOTO_DIRECTION_RE.sub("", info.strip())
    parts = re.split(r"[ \u3000]+", info.strip())
    parts = [p for p in parts if p]
    if not parts:
        return "", ""
    company_end_idx = 0
    for i, p in enumerate(parts[:4]):
        if ENTITY_SUFFIX_RE.search(p):
            company_end_idx = i
            break
    company = " ".join(parts[: company_end_idx + 1])
    dept_title = " ".join(parts[company_end_idx + 1:])
    return company, dept_title


# 本文の開始・終了の目印。ページ全体には「アクセスランキング」「新着記事」等の
# サイドバー/フッター要素があり、他記事の見出しが「〜氏」で終わることがあるため、
# 本文範囲だけに絞ってキャプションを探す（サイドバー混入によるノイズ防止）。
BODY_START_RE = re.compile(r"^\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}$")
BODY_END_MARKERS = [
    "この記事は参考になりましたか",
    "この記事の著者",
    "関連リンク",
    "会員登録",
]


def find_caption_lines(html: str):
    """本文テキストから「〜氏」で終わる行を抽出する（本文範囲のみ、サイドバー等は除外）。"""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]

    # 本文開始位置: 記事タイトル直後に出る公開日時行（YYYY/MM/DD HH:MM）を探す
    start_idx = 0
    for i, ln in enumerate(lines):
        if BODY_START_RE.match(ln):
            start_idx = i + 1
            break

    # 本文終了位置: フッター系マーカーのうち最初に現れるもの
    end_idx = len(lines)
    for marker in BODY_END_MARKERS:
        for i in range(start_idx, len(lines)):
            if marker in lines[i]:
                end_idx = min(end_idx, i)
                break

    body_lines = lines[start_idx:end_idx]

    captions = []
    for ln in body_lines:
        if not ln.endswith("氏"):
            continue
        if ln in FALSE_POSITIVE_TERMS:
            continue
        if len(ln) < MIN_CAPTION_LEN or len(ln) > MAX_CAPTION_LEN:
            continue
        captions.append(ln)
    return captions


def classify_caption(caption: str):
    """
    キャプション1行を分類する。
    戻り値: (classification, company, dept_title, name)
      classification: "regex_ok" または "needs_ai"
    """
    if NARRATIVE_SIGNAL_RE.search(caption):
        return "needs_ai", "", "", ""

    m = NAME_TAIL_RE.search(caption)
    if m:
        first_word, _, second_word = m.group("name").replace("\u3000", " ").partition(" ")
        # 完全一致だけでなく「副本部長」「客員教授」のような派生形も拾うため語尾一致で判定
        if first_word in ROLE_WORDS or any(first_word.endswith(rw) for rw in ROLE_WORDS):
            # 「役職語 + 氏名」を「姓 + 名」と誤認していたケース→単語1つの氏名として取り直す
            m = None

    if not m:
        m = SINGLE_NAME_RE.search(caption)
        if not m:
            return "needs_ai", "", "", ""

    name = m.group("name").replace("\u3000", " ").strip()
    prefix = caption[: m.start()].strip()

    if not ENTITY_SUFFIX_RE.search(prefix):
        # 会社名に法人格・機関名が見当たらない（略称ブランド名など）→ 安全に分解できない
        return "needs_ai", "", "", ""

    company, dept_title = split_company_and_dept(prefix)
    if not company:
        return "needs_ai", "", "", ""

    return "regex_ok", company, dept_title, name


def process_article(row):
    url = row["article_url"]
    media = row.get("media", "")
    try:
        html = fetch_html(url)
    except requests.exceptions.RequestException as e:
        return []

    publish_date = extract_publish_date(html)
    if SINCE_DATE and publish_date and publish_date < SINCE_DATE:
        return []  # 指定日より前の記事は除外
    client_name = extract_client_name(html)
    captions = find_caption_lines(html)

    results = []
    if not captions:
        results.append({
            "media": media, "article_url": url, "publish_date": publish_date,
            "client_name": client_name, "caption_raw": "", "classification": "no_caption",
            "company": "", "dept_title": "", "name": "",
        })
        return results

    for cap in captions:
        cls, company, dept_title, name = classify_caption(cap)
        results.append({
            "media": media, "article_url": url, "publish_date": publish_date,
            "client_name": client_name, "caption_raw": cap, "classification": cls,
            "company": company, "dept_title": dept_title, "name": name,
        })
    return results


def main():
    parser = argparse.ArgumentParser(description="Zine記事のキャプションをAIコストなし/AI要の2種に分類する")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--since", type=str, default=None,
                         help="この日付(YYYY-MM-DD)より前の公開日の記事は結果から除外する")
    parser.add_argument("--debug-first-html", type=str, default=None,
                         help="最初の1記事の生HTMLをこのパスに保存してから通常処理を続ける(調査用)")
    args = parser.parse_args()

    global SINCE_DATE
    SINCE_DATE = args.since

    with open(args.input, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    if args.limit is not None:
        rows = rows[: args.limit]

    if args.debug_first_html and rows:
        try:
            debug_html = fetch_html(rows[0]["article_url"])
            with open(args.debug_first_html, "w", encoding="utf-8") as f:
                f.write(debug_html)
            print(f"デバッグ用に {rows[0]['article_url']} の生HTMLを {args.debug_first_html} に保存しました")
            # meta系タグだけ抜き出して画面にも表示（見やすくするため）
            meta_lines = re.findall(r"<meta[^>]*>", debug_html, re.IGNORECASE)
            print(f"<meta>タグ数: {len(meta_lines)}")
            for ml in meta_lines:
                if "publish" in ml.lower() or "cxenseparse" in ml.lower():
                    print("  ", ml)
        except requests.exceptions.RequestException as e:
            print(f"デバッグ取得失敗: {e}")

    print(f"対象記事数: {len(rows):,}")

    fieldnames = [
        "media", "article_url", "publish_date", "client_name",
        "caption_raw", "classification", "company", "dept_title", "name",
    ]

    write_lock = Lock()
    counter_lock = Lock()
    processed = 0
    counts = {"regex_ok": 0, "needs_ai": 0, "no_caption": 0}
    start_time = time.time()

    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        f.flush()

        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = [executor.submit(process_article, row) for row in rows]

            for fut in as_completed(futures):
                results = fut.result()
                with write_lock:
                    writer.writerows(results)
                    f.flush()
                with counter_lock:
                    processed += 1
                    for r in results:
                        counts[r["classification"]] = counts.get(r["classification"], 0) + 1
                    elapsed = time.time() - start_time
                    rate = processed / elapsed if elapsed > 0 else 0
                    remaining = (len(rows) - processed) / rate if rate > 0 else 0
                    print(f"[{processed:,}/{len(rows):,}] 経過{elapsed:.0f}秒 残り約{remaining:.0f}秒 "
                          f"(regex_ok={counts.get('regex_ok',0):,} needs_ai={counts.get('needs_ai',0):,} "
                          f"no_caption={counts.get('no_caption',0):,})")
                time.sleep(0.2)

    print("\n=== 集計結果 ===")
    total_captions = counts.get("regex_ok", 0) + counts.get("needs_ai", 0)
    print(f"AIコストなしで抽出できる件数(regex_ok): {counts.get('regex_ok', 0):,}")
    print(f"Claude APIが必要な件数(needs_ai)      : {counts.get('needs_ai', 0):,}")
    print(f"キャプションが見つからなかった記事数    : {counts.get('no_caption', 0):,}")
    print(f"人物キャプション合計                    : {total_captions:,}")
    print(f"\n結果を {args.output} に書き出しました。")


if __name__ == "__main__":
    main()
