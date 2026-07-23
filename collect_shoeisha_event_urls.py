"""
collect_shoeisha_event_urls.py
============================================================
翔泳社が運営する各種イベント（MarkeZine Day以外）のハブ/開催実績ページから
個別イベントのURLを収集する。

対象イベントは、URLパスが「/{媒体略称}/{8桁の日付}」という共通規則に
なっていることを利用し、正規表現でハブページ内の該当リンクを全て拾う。
1つのハブページに複数の兄弟イベント（例: ezdayハブにはezday/soday/datatech/
dboday等が同居）が載っているケースにも対応する。

対象ハブページ:
    EnterpriseZine系  https://event.shoeisha.jp/ezday
    MarkeZine Day     https://event.shoeisha.jp/mzday/archive
    Biz/Zine Day      https://event.shoeisha.jp/bizzday/archive
    HRzine Day        https://event.shoeisha.jp/hrzday/archive
    Developers Summit https://event.shoeisha.jp/devsumi
    AX Day            https://event.shoeisha.jp/axday

使い方:
    python collect_shoeisha_event_urls.py --output shoeisha_events_all.csv --since 2020-01-01
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

HUB_PAGES = [
    ("EnterpriseZine系", "https://event.shoeisha.jp/ezday"),
    ("MarkeZine Day", "https://event.shoeisha.jp/mzday/archive"),
    ("Biz/Zine Day", "https://event.shoeisha.jp/bizzday/archive"),
    ("HRzine Day", "https://event.shoeisha.jp/hrzday/archive"),
    ("Developers Summit", "https://event.shoeisha.jp/devsumi"),
    ("AX Day", "https://event.shoeisha.jp/axday"),
    ("ECzine Day", "https://event.shoeisha.jp/eczday"),
]

# /{alias}/{8桁の日付}（末尾に /timetable 等が続く場合や ? パラメータが付く場合もある）
# 日付部分（8桁）の後ろに "online" 等の接尾辞が付くURL（例: /eczday/20200304online）にも対応するため、
# 8桁の数字で始まる限り、後続の英数字も含めてスラッグ全体をグループ2として捕捉する
EVENT_URL_RE = re.compile(r"^https?://event\.shoeisha\.jp/([a-z0-9]+)/(\d{8}[a-z0-9]*)(?:/|\?|$)")


def fetch_html(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return resp.text


def extract_event_urls(html: str, since: date = None):
    soup = BeautifulSoup(html, "html.parser")
    results = []  # (alias, date_str, event_url)
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("http"):
            href = "https://event.shoeisha.jp" + href if href.startswith("/") else href
        m = EVENT_URL_RE.match(href)
        if not m:
            continue
        alias, slug = m.groups()
        date_str = slug[:8]  # 先頭8桁が日付。"20200304online"のような接尾辞付きスラッグにも対応
        try:
            d = date(int(date_str[0:4]), int(date_str[4:6]), int(date_str[6:8]))
        except ValueError:
            continue
        if since is not None and d < since:
            continue
        event_url = f"https://event.shoeisha.jp/{alias}/{slug}"
        if event_url in seen:
            continue
        seen.add(event_url)
        results.append((alias, date_str, event_url))
    return results


def main():
    parser = argparse.ArgumentParser(description="翔泳社イベント（MarkeZine Day以外）のイベントURLを収集する")
    parser.add_argument("--output", required=True)
    parser.add_argument("--since", type=str, default=None, help="この日付(YYYY-MM-DD)より前のイベントは除外する")
    parser.add_argument("--delay", type=float, default=1.0)
    args = parser.parse_args()

    since_date = None
    if args.since:
        y, mo, d = (int(x) for x in args.since.split("-"))
        since_date = date(y, mo, d)

    rows = []
    for hub_name, hub_url in HUB_PAGES:
        print(f"=== {hub_name} ({hub_url}) ===")
        try:
            html = fetch_html(hub_url)
        except requests.exceptions.RequestException as e:
            print(f"  取得失敗: {e}")
            continue

        events = extract_event_urls(html, since=since_date)
        print(f"  → {len(events):,} 件のイベントURL（エイリアス種別: {sorted(set(e[0] for e in events))}）")
        for alias, date_str, event_url in events:
            rows.append({"hub": hub_name, "alias": alias, "event_date": date_str, "event_url": event_url})
        time.sleep(args.delay)

    # 重複除去（複数ハブに同じイベントが載っているケースがあるため）
    dedup = {}
    for r in rows:
        dedup[r["event_url"]] = r
    rows = list(dedup.values())
    rows.sort(key=lambda r: r["event_date"], reverse=True)

    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["hub", "alias", "event_date", "event_url"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n完了。{len(rows):,} 件のユニークイベントURLを {args.output} に書き出しました。")


if __name__ == "__main__":
    main()
