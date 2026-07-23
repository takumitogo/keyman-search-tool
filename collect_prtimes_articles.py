"""
collect_prtimes_articles.py
============================================================
PR TIMES (prtimes.jp) の内部検索JSON APIを使って、指定キーワード
（部長・課長・マネージャー・マネジャー）にヒットする記事情報を収集する。

APIエンドポイント:
    https://prtimes.jp/api/keyword_search.php/search?keyword={キーワード}&page={N}&limit=40

レスポンスに company_name（会社名）・title（記事タイトル）・released_at
（公開日時）・release_url が直接含まれるため、本文を読まなくても
これらの情報が取得できる。

使い方:
    python collect_prtimes_articles.py --output prtimes_articles.csv --max-pages 50
============================================================
"""

import argparse
import csv
import re
import time
from urllib.parse import quote

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}
REQUEST_TIMEOUT = 20
API_BASE = "https://prtimes.jp/api/keyword_search.php/search"

KEYWORDS = ["部長", "課長", "マネージャー", "マネジャー"]

DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2})時(\d{1,2})分")


def parse_released_at(s: str) -> str:
    m = DATE_RE.search(s or "")
    if not m:
        return ""
    y, mo, d = m.groups()[:3]
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"


def fetch_page(keyword: str, page: int, limit: int = 40):
    url = f"{API_BASE}?keyword={quote(keyword)}&page={page}&limit={limit}"
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def search_keyword(keyword: str, max_pages: int, delay: float, since: str = None):
    releases = []
    page = 1
    while page <= max_pages:
        try:
            data = fetch_page(keyword, page)
        except (requests.exceptions.RequestException, ValueError) as e:
            print(f"  [{keyword}] p={page} 取得失敗: {e}")
            break

        payload = data.get("data", {})
        release_list = payload.get("release_list", [])
        last_page = payload.get("last_page", page)

        if not release_list:
            print(f"  [{keyword}] p={page}: 結果なし、終了")
            break

        stop = False
        for r in release_list:
            released_date = parse_released_at(r.get("released_at", ""))
            if since is not None and released_date and released_date < since:
                stop = True
                break
            releases.append({
                "keyword": keyword,
                "company_name": r.get("company_name", ""),
                "title": r.get("title", ""),
                "publish_date": released_date,
                "article_url": "https://prtimes.jp" + r.get("release_url", ""),
            })

        print(f"  [{keyword}] p={page}/{last_page}: 累計{len(releases):,}件"
              + (f" ({since}より前に到達、打ち切り)" if stop else ""))

        if stop or page >= last_page:
            break

        page += 1
        time.sleep(delay)

    return releases


def main():
    parser = argparse.ArgumentParser(description="PR TIMESの記事情報をキーワード検索APIで収集する")
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-pages", type=int, default=50, help="キーワードごとの最大ページ数(1ページ40件)")
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--since", type=str, default=None,
                         help="この日付(YYYY-MM-DD)より前の記事に到達したら打ち切る")
    args = parser.parse_args()

    rows = []
    seen_urls = set()
    for kw in KEYWORDS:
        print(f"=== キーワード: {kw} ===")
        releases = search_keyword(kw, args.max_pages, args.delay, since=args.since)
        new_count = 0
        for r in releases:
            if r["article_url"] in seen_urls:
                continue
            seen_urls.add(r["article_url"])
            rows.append(r)
            new_count += 1
        print(f"  → 新規{new_count:,}件(重複除去後)")

    print(f"\n合計(重複除去後): {len(rows):,} 件")

    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["keyword", "company_name", "title", "publish_date", "article_url"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"結果を {args.output} に書き出しました。")


if __name__ == "__main__":
    main()
