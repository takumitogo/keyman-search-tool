"""
collect_shoeisha_events.py
============================================================
MarkeZine Day（event.shoeisha.jp/mzday/archive）の開催回一覧から、
2020年以降のイベントページURLを収集する。AIコストなし。

使い方:
    python collect_shoeisha_events.py --output shoeisha_events.csv --min-year 2020
============================================================
"""

import argparse
import csv
import re

import requests
from bs4 import BeautifulSoup

ARCHIVE_URL = "https://event.shoeisha.jp/mzday/archive"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}
REQUEST_TIMEOUT = 15


def fetch_html(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return resp.text


def main():
    parser = argparse.ArgumentParser(description="MarkeZine Dayの開催回一覧を収集する")
    parser.add_argument("--output", required=True)
    parser.add_argument("--min-year", type=int, default=2020)
    args = parser.parse_args()

    html = fetch_html(ARCHIVE_URL)
    soup = BeautifulSoup(html, "html.parser")

    events = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"/mzday/(\d{8})$", href)
        if not m:
            continue
        date_str = m.group(1)  # YYYYMMDD
        year = int(date_str[:4])
        if year < args.min_year:
            continue

        full_url = href if href.startswith("http") else "https://event.shoeisha.jp" + href
        label = a.get_text(strip=True)
        if full_url in events:
            continue
        events[full_url] = {"label": label, "event_url": full_url, "date_from_url": date_str}

    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["label", "event_url", "date_from_url"])
        writer.writeheader()
        writer.writerows(events.values())

    print(f"完了。{len(events):,}件の開催回を {args.output} に書き出しました。")


if __name__ == "__main__":
    main()
