"""
extract_onecareer_speakers.py
============================================================
ワンキャリア(onecareer.jp)のDeep Interview記事から、インタビュイーの
会社名・氏名・部署役職を抽出する。AIコストなし。

構造（実際のページで確認済み）:
    水谷 優香（みずたに ゆか）：株式会社リブ・コンサルティング
    エンタープライズ事業本部 マネージャー  東京大学法学部卒業後、2020年に...(経歴本文)

「氏名（ふりがな）：会社名」という行を目印に、直後の行から部署役職
部分（経歴本文が始まる前まで）を取り出す。

使い方:
    python extract_onecareer_speakers.py --input onecareer_articles.csv \
        --output onecareer_speakers.csv
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

# 「氏名（ふりがな）：会社名」または「氏名(ふりがな)：会社名」形式。
# ふりがな部分はひらがな中心のはず、という制約を付けて誤検出（サイドバーの
# 【】付き見出し等）を減らす。
NAME_LINE_RE = re.compile(
    r"^(?P<name>[^\s（(：:]{2,12}\s?[^\s（(：:]{0,12})[（(](?P<furigana>[ぁ-んー\s　]{2,20})[）)]\s*[:：]\s*(?P<rest>.+)$"
)

DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
SPONSOR_RE = re.compile(r"sponsored by\s*(.+)")
DEPT_LIKE_SUFFIX_RE = re.compile(r"(本部|事業部|部門|部|課|室|センター|グループ)$")


def extract_sponsor(text: str) -> str:
    m = SPONSOR_RE.search(text)
    return m.group(1).strip() if m else ""

COMPANY_SUFFIX_RE = re.compile(r"(株式会社|合同会社|合資会社|有限会社|Inc\.?|Ltd\.?|Corporation|LLC)")
INSTITUTION_SUFFIX_RE = re.compile(r"(大学院|大学|研究所|研究科|協会|財団|機構|省|庁)")
ENTITY_SUFFIX_RE = re.compile(f"({COMPANY_SUFFIX_RE.pattern}|{INSTITUTION_SUFFIX_RE.pattern})")


def split_company_and_dept(info: str):
    """「会社名 部署 役職」が1行に連結している場合に会社名とそれ以外を分割する。"""
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


def fetch_html(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return resp.text


def extract_publish_date(text: str) -> str:
    m = DATE_RE.search(text)
    if m:
        y, mo, d = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    return ""


def extract_dept_title(line: str) -> str:
    """次の行(部署役職+経歴本文が連結)から、部署役職部分だけを切り出す。"""
    # 2つ以上連続する空白がある場合はそこで区切る(部署役職と経歴本文の境目のことが多い)
    m = re.search(r"\s{2,}", line)
    if m:
        candidate = line[: m.start()].strip()
        if candidate:
            return candidate
    # 句点があれば、その手前までのうち短い場合のみ部署役職とみなす
    if "。" in line:
        candidate = line.split("。", 1)[0].strip()
        if len(candidate) <= 40:
            return candidate
        return ""
    # 全体が短ければそのまま部署役職とみなす
    if len(line) <= 40:
        return line.strip()
    return ""


def extract_speakers(html: str):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]

    publish_date = extract_publish_date(text)
    sponsor = extract_sponsor(text)

    speakers = []
    seen = set()
    for i, line in enumerate(lines):
        m = NAME_LINE_RE.match(line)
        if not m:
            continue
        name = m.group("name").replace("\u3000", " ").strip()
        rest = m.group("rest").strip()
        if not name or not rest:
            continue
        if len(name) > 12 or len(rest) > 80:
            continue  # 明らかに人名・会社名らしくない場合は誤検出とみなしスキップ
        if "【" in rest or "】" in rest or "【" in name or "】" in name:
            continue  # サイドバー見出し等の誤検出除去

        # 「会社名」だけの場合と「会社名 部署 役職」が1行に連結している場合の両方に対応
        first_token = re.split(r"[ \u3000]+", rest.strip())[0]
        if not ENTITY_SUFFIX_RE.search(rest) and DEPT_LIKE_SUFFIX_RE.search(first_token):
            # 会社名が書かれておらず、部署役職だけが書かれているパターン
            # →sponsored by タグの会社名で補う
            company = sponsor
            dept_title = rest
        else:
            company, dept_title = split_company_and_dept(rest)
            if not company:
                company = rest

        if not dept_title and i + 1 < len(lines):
            # 会社名だけしか無かった場合は、次の行から部署役職を切り出す
            dept_title = extract_dept_title(lines[i + 1])
        dept_title = dept_title.replace("\u3000", " ").rstrip("。").strip()

        if not company:
            continue  # 会社名が結局分からない場合はスキップ

        key = (company, name)
        if key in seen:
            continue
        seen.add(key)
        speakers.append({"company": company, "name": name, "dept_title": dept_title})

    return speakers, publish_date


def process_article(row):
    url = row["article_url"]
    try:
        html = fetch_html(url)
    except requests.exceptions.RequestException as e:
        return row, [], "", f"取得失敗: {e}"

    speakers, publish_date = extract_speakers(html)
    for sp in speakers:
        sp["publish_date"] = publish_date
        sp["article_url"] = url

    status = "OK" if speakers else "登場人物情報なし"
    return row, speakers, publish_date, status


def main():
    parser = argparse.ArgumentParser(description="ワンキャリアDeep Interview記事から登場人物情報を抽出する")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    with open(args.input, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if args.limit is not None:
        rows = rows[: args.limit]

    print(f"対象記事数: {len(rows):,}")

    fieldnames = ["company", "name", "dept_title", "publish_date", "article_url", "status"]
    write_lock = Lock()
    counter_lock = Lock()
    processed = 0
    total_speakers = 0
    start_time = time.time()

    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        f.flush()

        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = [executor.submit(process_article, row) for row in rows]
            for fut in as_completed(futures):
                row, speakers, publish_date, status = fut.result()
                rows_to_write = []
                if speakers:
                    for sp in speakers:
                        rows_to_write.append({
                            "company": sp["company"], "name": sp["name"], "dept_title": sp["dept_title"],
                            "publish_date": sp["publish_date"], "article_url": sp["article_url"], "status": status,
                        })
                else:
                    rows_to_write.append({
                        "company": "", "name": "", "dept_title": "", "publish_date": publish_date,
                        "article_url": row["article_url"], "status": status,
                    })
                with write_lock:
                    writer.writerows(rows_to_write)
                    f.flush()
                with counter_lock:
                    processed += 1
                    total_speakers += len(speakers)
                    elapsed = time.time() - start_time
                    print(f"[{processed:,}/{len(rows):,}] {status} ({len(speakers)}名) "
                          f"累計{total_speakers:,}名 経過{elapsed:.0f}秒")
                time.sleep(0.3)

    print(f"\n完了。累計{total_speakers:,}名の情報を取得しました。")
    print(f"結果を {args.output} に書き出しました。")


if __name__ == "__main__":
    main()
