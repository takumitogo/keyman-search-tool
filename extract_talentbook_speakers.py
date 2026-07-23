"""
extract_talentbook_speakers.py
============================================================
TalentBook (talent-book.jp) の記事ページから、登場人物（社員）の
会社名・氏名・部署役職を抽出する。AIコストなし。

構造（実際のページで確認済み）:
    *宮下 海里グループIT本部 情報セキュリティ部サイバーセキュリティ室 シニアコンサルタント*  ← 氏名+部署役職(連結、区切りなし)
    *宮下 海里みやした かいり*                                                          ← 氏名+ふりがな(連結)
    新卒                                                                              ← 入社区分バッジ(新卒/中途/日付)
    グループIT本部 情報セキュリティ部サイバーセキュリティ室 シニアコンサルタント              ← 部署役職(単体、氏名混入なし)
    (経歴本文)

戦略:
    バッジ行（「新卒」「中途」または「YYYY年M月 /新卒」等）を目印に、
    その直後の行を「部署役職」として確定し、2行前の「氏名+部署役職連結」行
    から末尾の部署役職文字列を取り除くことで、正確な氏名だけを取り出す。

使い方:
    python extract_talentbook_speakers.py --input talentbook_articles.csv \
        --output talentbook_speakers.csv
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
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Referer": "https://www.talent-book.jp/",
    "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="124", "Google Chrome";v="124"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
}
REQUEST_TIMEOUT = 20

BADGE_RE = re.compile(r"^(\d{4}年\d{1,2}月\s*/\s*)?(新卒|中途)$")
DATE_RE = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})")


def fetch_html(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return resp.text


def extract_company_name(html: str, text: str) -> str:
    """og:title は「記事タイトル | 会社名」形式なのでそこから取得する。"""
    m = re.search(
        r'<meta[^>]*?(?:property|name)=["\']og:title["\'][^>]*?content=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    )
    if not m:
        m = re.search(
            r'<meta[^>]*?content=["\']([^"\']+)["\'][^>]*?(?:property|name)=["\']og:title["\']',
            html, re.IGNORECASE,
        )
    if m and "|" in m.group(1):
        return m.group(1).rsplit("|", 1)[-1].strip()
    return ""


def extract_publish_date(text: str) -> str:
    m = DATE_RE.search(text)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{mo}-{d}"
    return ""


def extract_speakers(html: str):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]

    company = extract_company_name(html, text)
    publish_date = extract_publish_date(text)

    speakers = []
    seen_names = set()
    for i, line in enumerate(lines):
        if not BADGE_RE.match(line):
            continue
        if i + 1 >= len(lines) or i < 2:
            continue

        dept_title = lines[i + 1]
        name_and_dept = lines[i - 2]  # 氏名+部署役職(連結)行

        if not name_and_dept.endswith(dept_title):
            continue  # 想定パターンに合わないので安全のためスキップ

        name = name_and_dept[: -len(dept_title)].strip()
        if not name or len(name) > 12:
            continue
        if name in seen_names:
            continue
        seen_names.add(name)

        speakers.append({"company": company, "name": name, "dept_title": dept_title})

    return speakers, company, publish_date


def process_article(row):
    url = row["article_url"]
    try:
        html = fetch_html(url)
    except requests.exceptions.RequestException as e:
        return row, [], "", f"取得失敗: {e}"

    speakers, company, publish_date = extract_speakers(html)
    for sp in speakers:
        sp["publish_date"] = publish_date
        sp["article_url"] = url

    status = "OK" if speakers else "登場人物情報なし"
    return row, speakers, company, status


def main():
    parser = argparse.ArgumentParser(description="TalentBookの登場人物情報を抽出する")
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
                row, speakers, company, status = fut.result()
                rows_to_write = []
                if speakers:
                    for sp in speakers:
                        rows_to_write.append({
                            "company": sp["company"], "name": sp["name"], "dept_title": sp["dept_title"],
                            "publish_date": sp["publish_date"], "article_url": sp["article_url"], "status": status,
                        })
                else:
                    rows_to_write.append({
                        "company": company, "name": "", "dept_title": "", "publish_date": "",
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
