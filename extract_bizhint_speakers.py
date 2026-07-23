"""
extract_bizhint_speakers.py
============================================================
BizHint (bizhint.jp) のインタビュー記事から、会社名・氏名・役職を抽出する。
記事全文は会員登録(ログイン)が必要だが、冒頭のプロフィール部分
（会社名 / 役職＋氏名さん）はログイン無しで表示されるため、
その範囲だけを対象にする。AIコストなし。

構造（実際のページで確認済み）:
    株式会社オオクシ
    代表取締役社長 大串 哲史さん
    （経歴本文...この後、ペイウォールで本文が制限される）

「〜さん」で終わる行を目印に、その行から役職と氏名を分離し、
直前の行を会社名とする。

使い方:
    python extract_bizhint_speakers.py --input bizhint_articles.csv \
        --output bizhint_speakers.csv
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

# 「役職 氏名さん」形式。氏名は1〜2トークン(姓 名 or 姓名の連結)。
TITLE_NAME_RE = re.compile(
    r"^(?P<title>.*?)(?P<name>[^\s　]{1,6}(?:[ 　][^\s　]{1,6})?)さん$"
)

# 見出し等、プロフィール行として扱わない誤検出防止用
EXCLUDE_NAME_WORDS = {"皆さん", "お客さん", "みなさん"}


def fetch_html(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return resp.text


def find_meta_content(html: str, meta_name: str):
    tag_re = re.compile(
        r'<meta\s+[^>]*?(?:name|property)=["\']' + re.escape(meta_name) + r'["\'][^>]*?>',
        re.IGNORECASE,
    )
    tag_m = tag_re.search(html)
    if not tag_m:
        return None
    content_m = re.search(r'content=["\']([^"\']*)["\']', tag_m.group(0), re.IGNORECASE)
    return content_m.group(1) if content_m else None


def extract_publish_date(html: str) -> str:
    val = find_meta_content(html, "article:published_time")
    if val:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", val)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""


def extract_speaker(html: str):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]

    publish_date = extract_publish_date(html)

    # 記事冒頭付近(だいたい最初の30行以内)で最初に「〜さん」で終わる行を探す
    for i, line in enumerate(lines[:30]):
        if not line.endswith("さん"):
            continue
        if line in EXCLUDE_NAME_WORDS:
            continue
        m = TITLE_NAME_RE.match(line)
        if not m:
            continue
        title = m.group("title").strip()
        name = m.group("name").replace("\u3000", " ").strip()
        if len(name) < 2 or len(name) > 12:
            continue

        company = lines[i - 1].strip() if i > 0 else ""
        if len(company) > 40 or not company:
            continue  # 会社名らしくない場合は誤検出とみなす

        return {"company": company, "name": name, "dept_title": title, "publish_date": publish_date}

    return None


def process_article(row):
    url = row["article_url"]
    try:
        html = fetch_html(url)
    except requests.exceptions.RequestException as e:
        return row, None, f"取得失敗: {e}"

    speaker = extract_speaker(html)
    status = "OK" if speaker else "登場人物情報なし"
    return row, speaker, status


def main():
    parser = argparse.ArgumentParser(description="BizHint記事から会社名・氏名・役職を抽出する")
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
    found = 0
    start_time = time.time()

    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        f.flush()

        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = [executor.submit(process_article, row) for row in rows]
            for fut in as_completed(futures):
                row, speaker, status = fut.result()
                out_row = {
                    "company": speaker["company"] if speaker else "",
                    "name": speaker["name"] if speaker else "",
                    "dept_title": speaker["dept_title"] if speaker else "",
                    "publish_date": speaker["publish_date"] if speaker else "",
                    "article_url": row["article_url"],
                    "status": status,
                }
                with write_lock:
                    writer.writerow(out_row)
                    f.flush()
                with counter_lock:
                    processed += 1
                    if speaker:
                        found += 1
                    elapsed = time.time() - start_time
                    print(f"[{processed:,}/{len(rows):,}] {status} 累計取得={found:,} 経過{elapsed:.0f}秒")
                time.sleep(0.3)

    print(f"\n完了。{found:,}件の情報を取得しました。")
    print(f"結果を {args.output} に書き出しました。")


if __name__ == "__main__":
    main()
