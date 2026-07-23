"""
collect_techplay_events.py
============================================================
TECH PLAY (techplay.jp) のイベント一覧をページネーションしながら巡回し、
イベント詳細URLを収集する。

一覧ページ: https://techplay.jp/event?page=N （通常のイベント一覧）
資料ページ: https://techplay.jp/event?report=1&page=N （資料公開済み＝終了済みイベント中心）

使い方:
    python collect_techplay_events.py --output techplay_events.csv --max-pages 40
============================================================
"""

import argparse
import csv
import re
import time
from datetime import date

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}
REQUEST_TIMEOUT = 20

EVENT_URL_RE = re.compile(r"^https?://techplay\.jp/event/(\d+)/?$")
# イベント一覧カードに表示される開催日（例: 2026/07/31）を拾う
LIST_DATE_RE = re.compile(r"(\d{4})/(\d{2})/(\d{2})")


def extract_min_date_on_page(html: str):
    dates = []
    for m in LIST_DATE_RE.finditer(html):
        y, mo, d = (int(x) for x in m.groups())
        try:
            dates.append(date(y, mo, d))
        except ValueError:
            continue
    return min(dates) if dates else None


def fetch_html(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return resp.text


def extract_event_links(html: str):
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("http"):
            href = "https://techplay.jp" + href if href.startswith("/") else href
        m = EVENT_URL_RE.match(href.split("?")[0])
        if m:
            links.add(f"https://techplay.jp/event/{m.group(1)}")
    return links


def collect_listing(base_query: str, max_pages: int, delay: float, label: str, since: date = None):
    all_links = set()
    empty_streak = 0
    for page in range(1, max_pages + 1):
        url = f"https://techplay.jp/event?{base_query}&page={page}" if base_query else f"https://techplay.jp/event?page={page}"
        try:
            html = fetch_html(url)
        except requests.exceptions.RequestException as e:
            print(f"  [{label}] p={page} 取得失敗: {e}")
            break
        links = extract_event_links(html)
        new_links = links - all_links
        min_date_on_page = extract_min_date_on_page(html)
        date_note = f" (最古の日付: {min_date_on_page})" if min_date_on_page else ""
        print(f"  [{label}] p={page}: {len(links)}件中 新規{len(new_links)}件{date_note}")
        if not links:
            empty_streak += 1
        else:
            empty_streak = 0
        all_links |= links
        if empty_streak >= 2:
            break
        if since is not None and min_date_on_page is not None and min_date_on_page < since:
            print(f"  [{label}] {since}より前のページに到達したため打ち切り")
            break
        time.sleep(delay)
    return all_links


def main():
    parser = argparse.ArgumentParser(description="TECH PLAYのイベントURLを収集する")
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-pages", type=int, default=40)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--include-report", action="store_true",
                         help="資料公開済み(report=1)一覧も合わせて収集する")
    parser.add_argument("--since", type=str, default=None,
                         help="この日付(YYYY-MM-DD)より前のページに到達したら巡回を打ち切る")
    args = parser.parse_args()

    since_date = None
    if args.since:
        y, mo, d = (int(x) for x in args.since.split("-"))
        since_date = date(y, mo, d)

    all_links = {}

    print("=== 通常のイベント一覧 ===")
    for link in collect_listing("", args.max_pages, args.delay, "event", since=since_date):
        all_links[link] = "event"

    if args.include_report:
        print("\n=== 資料公開済み(終了済み中心)一覧 ===")
        for link in collect_listing("report=1", args.max_pages, args.delay, "report", since=since_date):
            all_links.setdefault(link, "report")

    rows = [{"event_url": url, "source": src} for url, src in all_links.items()]
    rows.sort(key=lambda r: r["event_url"])

    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["event_url", "source"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n完了。{len(rows):,} 件のユニークイベントURLを {args.output} に書き出しました。")


if __name__ == "__main__":
    main()
