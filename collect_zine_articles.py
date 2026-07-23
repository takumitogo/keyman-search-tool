"""
collect_zine_articles.py
============================================================
翔泳社が運営する各種Zineメディアの記事一覧ページを巡回し、
記事詳細URLを重複なく収集する。

対象メディア（媒体名, 記事一覧URL）:
    MarkeZine        https://markezine.jp/article
    EnterpriseZine   https://enterprisezine.jp/article
    ECzine           https://markezine.jp/commercezine/article
    Biz/Zine         https://bizzine.jp/article
    SalesZine        https://markezine.jp/saleszine/article
    HRzine           https://hrzine.jp/article
    ProductZine      https://codezine.jp/article?channel_type=pz
    AIdiver          https://aidiver.jp/article
    DeveloperZine    https://codezine.jp/article?channel_type=dz

使い方:
    python collect_zine_articles.py --output zine_articles.csv --max-pages 50
============================================================
"""

import argparse
import csv
import re
import time
from datetime import date
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}
REQUEST_TIMEOUT = 20

MEDIA_LISTS = [
    ("MarkeZine", "https://markezine.jp/article"),
    ("EnterpriseZine", "https://enterprisezine.jp/article"),
    ("ECzine", "https://markezine.jp/commercezine/article"),
    ("Biz/Zine", "https://bizzine.jp/article"),
    ("SalesZine", "https://markezine.jp/saleszine/article"),
    ("HRzine", "https://hrzine.jp/article"),
    ("ProductZine", "https://codezine.jp/article?channel_type=pz"),
    ("AIdiver", "https://aidiver.jp/article"),
    ("DeveloperZine", "https://codezine.jp/article?channel_type=dz"),
]

ARTICLE_URL_RE = re.compile(r"/article/detail/\d+")
# 記事一覧ページの日付見出し（例: 2026年07月15日(水)）を拾う
LIST_DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")


def extract_min_date_on_page(html: str):
    """一覧ページに含まれる日付見出しの中で最も古い日付を返す（見つからなければNone）。"""
    dates = []
    for m in LIST_DATE_RE.finditer(html):
        y, mo, d = (int(x) for x in m.groups())
        try:
            dates.append(date(y, mo, d))
        except ValueError:
            continue
    return min(dates) if dates else None


def build_page_url(base_url: str, page: int) -> str:
    """base_urlの既存クエリを保持したまま p=page を付与/上書きする。"""
    parsed = urlparse(base_url)
    qs = parse_qs(parsed.query)
    qs["p"] = [str(page)]
    new_query = urlencode(qs, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def fetch_html(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return resp.text


def extract_article_links(html: str, base_domain: str):
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not ARTICLE_URL_RE.search(href):
            continue
        if href.startswith("http"):
            full = href
        else:
            full = base_domain.rstrip("/") + "/" + href.lstrip("/")
        links.add(full.split("?")[0])  # クエリは除去して正規化
    return links


def collect_media(media_name: str, base_url: str, max_pages: int, delay: float, since: date = None):
    parsed = urlparse(base_url)
    base_domain = f"{parsed.scheme}://{parsed.netloc}"

    all_links = set()
    page = 1
    empty_streak = 0
    while page <= max_pages:
        page_url = build_page_url(base_url, page)
        try:
            html = fetch_html(page_url)
        except requests.exceptions.RequestException as e:
            print(f"  [{media_name}] p={page} 取得失敗: {e}")
            break

        links = extract_article_links(html, base_domain)
        new_links = links - all_links
        min_date_on_page = extract_min_date_on_page(html)
        date_note = f" (最古の日付見出し: {min_date_on_page})" if min_date_on_page else ""
        print(f"  [{media_name}] p={page}: {len(links)}件中 新規{len(new_links)}件{date_note}")

        if not links:
            empty_streak += 1
        else:
            empty_streak = 0

        all_links |= links

        # 2ページ連続で記事が見つからなければ終端とみなす
        if empty_streak >= 2:
            break

        # 指定日より前のページに到達したら、このページの記事までは収集して終了
        # （classify側でpublish_dateにより最終フィルタするので、境界ページの
        # 少し新しめの記事まで多めに拾っても後段で除外される）
        if since is not None and min_date_on_page is not None and min_date_on_page < since:
            print(f"  [{media_name}] {since}より前のページに到達したため打ち切り")
            break

        page += 1
        time.sleep(delay)

    return all_links


def main():
    parser = argparse.ArgumentParser(description="翔泳社Zineメディアの記事URLを収集する")
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-pages", type=int, default=50, help="媒体ごとの最大ページ数（安全弁）")
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--since", type=str, default=None,
                         help="この日付(YYYY-MM-DD)より前のページに到達したら巡回を打ち切る")
    args = parser.parse_args()

    since_date = None
    if args.since:
        y, mo, d = (int(x) for x in args.since.split("-"))
        since_date = date(y, mo, d)

    rows = []
    for media_name, base_url in MEDIA_LISTS:
        print(f"=== {media_name} ({base_url}) ===")
        links = collect_media(media_name, base_url, args.max_pages, args.delay, since=since_date)
        print(f"  → 合計 {len(links):,} 件のユニーク記事URL")
        for link in sorted(links):
            rows.append({"media": media_name, "article_url": link})

    # 媒体をまたいだ重複記事（同じarticle_urlが複数媒体一覧に出てくる場合）を確認
    seen_urls = {}
    dedup_rows = []
    for r in rows:
        url = r["article_url"]
        if url in seen_urls:
            seen_urls[url].append(r["media"])
            continue
        seen_urls[url] = [r["media"]]
        dedup_rows.append(r)

    cross_media_dupes = {u: ms for u, ms in seen_urls.items() if len(ms) > 1}
    if cross_media_dupes:
        print(f"\n媒体をまたいだ重複記事: {len(cross_media_dupes):,} 件")

    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["media", "article_url"])
        writer.writeheader()
        writer.writerows(dedup_rows)

    print(f"\n完了。{len(dedup_rows):,} 件のユニーク記事URLを {args.output} に書き出しました。")


if __name__ == "__main__":
    main()
