"""
collect_sbbit_events.py
============================================================
ビジネス+IT（sbbit.jp）のセミナー・イベント一覧を、ページネーションを辿って収集する。
AIコストなし。

使い方:
    python collect_sbbit_events.py --output sbbit_events.csv
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
REQUEST_TIMEOUT = 15
BASE_LIST_URL = "https://www.sbbit.jp/eventinfo/result"
MAX_PAGES = 30  # 安全上限


def fetch_html(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return resp.text


def parse_event_list(html: str):
    """1ページ分の一覧HTMLから (イベントURL, タイトル, 開催日) のリストを抽出する。"""
    soup = BeautifulSoup(html, "html.parser")
    events = {}

    for a in soup.find_all("a", href=True):
        m = re.search(r"/eventinfo/detail/(\d+)", a["href"])
        if not m:
            continue
        event_id = m.group(1)
        if event_id in events:
            continue

        full_url = a["href"]
        if full_url.startswith("/"):
            full_url = "https://www.sbbit.jp" + full_url

        text = a.get_text(separator="\n", strip=True)
        date_m = re.search(r"(\d{4}/\d{1,2}/\d{1,2})(?:-\d{1,2}/\d{1,2})?", text)
        date = date_m.group(1) if date_m else ""

        # タイトルらしき行（日付や地域名以外で、ある程度の長さがある行）を拾う
        title = ""
        for line in text.split("\n"):
            if re.match(r"^\d{4}/", line) or line in ("オンライン", "オンデマンド", "その他", "イベント・セミナー"):
                continue
            if len(line) >= 5:
                title = line
                break

        events[event_id] = {"event_id": event_id, "url": full_url, "title": title, "date_from_list": date}

    return list(events.values())


def detect_total_pages(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    max_page = 1
    for a in soup.find_all("a", href=True):
        m = re.search(r"[?&]page=(\d+)", a["href"])
        if m:
            max_page = max(max_page, int(m.group(1)))
    return min(max_page, MAX_PAGES)


def main():
    parser = argparse.ArgumentParser(description="sbbit.jpのセミナー一覧を収集する")
    parser.add_argument("--output", required=True)
    parser.add_argument("--status", default="ApNow", help="ステータス（デフォルト: 申込受付中）")
    args = parser.parse_args()

    first_url = f"{BASE_LIST_URL}?sts[0]={args.status}"
    print("1ページ目を取得中...")
    first_html = fetch_html(first_url)
    total_pages = detect_total_pages(first_html)
    print(f"総ページ数: {total_pages}")

    all_events = {}
    for e in parse_event_list(first_html):
        all_events[e["event_id"]] = e

    for page in range(2, total_pages + 1):
        page_url = f"{BASE_LIST_URL}?sts[0]={args.status}&page={page}"
        html = fetch_html(page_url)
        for e in parse_event_list(html):
            all_events[e["event_id"]] = e
        print(f"[{page}/{total_pages}] 累計{len(all_events):,}件")
        time.sleep(0.5)

    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["event_id", "url", "title", "date_from_list"])
        writer.writeheader()
        writer.writerows(all_events.values())

    print(f"完了。{len(all_events):,}件を {args.output} に書き出しました。")


if __name__ == "__main__":
    main()
