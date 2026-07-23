"""
collect_onecareer_articles.py
============================================================
ワンキャリア(onecareer.jp)の「Deep Interview」特集記事一覧から記事URLを
収集する。

一覧ページ: https://www.onecareer.jp/feature_articles/31?page=N
記事ページ: https://www.onecareer.jp/articles/{id}

使い方:
    python collect_onecareer_articles.py --output onecareer_articles.csv
============================================================
"""

import argparse
import csv
import re
import time

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}
REQUEST_TIMEOUT = 20

ARTICLE_URL_RE = re.compile(r"^https?://www\.onecareer\.jp/articles/\d+/?$")


def fetch_html(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return resp.text


def extract_article_links(html: str):
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("http"):
            href = "https://www.onecareer.jp" + href if href.startswith("/") else href
        if ARTICLE_URL_RE.match(href.split("?")[0]):
            links.add(href.split("?")[0])
    return links


def main():
    parser = argparse.ArgumentParser(description="ワンキャリアDeep Interview記事URLを収集する")
    parser.add_argument("--output", required=True)
    parser.add_argument("--feature-id", type=int, default=31, help="特集記事のID(Deep Interviewは31)")
    parser.add_argument("--max-pages", type=int, default=40)
    parser.add_argument("--delay", type=float, default=1.0)
    args = parser.parse_args()

    all_links = set()
    empty_streak = 0
    for page in range(1, args.max_pages + 1):
        url = f"https://www.onecareer.jp/feature_articles/{args.feature_id}?page={page}"
        try:
            html = fetch_html(url)
        except requests.exceptions.RequestException as e:
            print(f"p={page} 取得失敗: {e}")
            break
        links = extract_article_links(html)
        new_links = links - all_links
        print(f"p={page}: {len(links)}件中 新規{len(new_links)}件")
        if not links:
            empty_streak += 1
        else:
            empty_streak = 0
        all_links |= links
        if empty_streak >= 2:
            break
        time.sleep(args.delay)

    rows = [{"article_url": u} for u in sorted(all_links)]
    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["article_url"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n完了。{len(rows):,} 件の記事URLを {args.output} に書き出しました。")


if __name__ == "__main__":
    main()
