"""
collect_jinjibu_conferences.py
============================================================
日本の人事部「HRカンファレンス」の開催回一覧
（https://jinjibu.jp/hr-conference/eventarchives.php）から、
各回のプログラムページURLを収集する。AIコストなし。

使い方:
    python collect_jinjibu_conferences.py --output jinjibu_conferences.csv
============================================================
"""

import argparse
import csv
import re

import requests
from bs4 import BeautifulSoup

ARCHIVE_URL = "https://jinjibu.jp/hr-conference/eventarchives.php"
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
    parser = argparse.ArgumentParser(description="HRカンファレンスの開催回一覧を収集する")
    parser.add_argument("--output", required=True)
    parser.add_argument("--min-year", type=int, default=2020, help="この年以降の回のみ対象にする")
    args = parser.parse_args()

    html = fetch_html(ARCHIVE_URL)
    soup = BeautifulSoup(html, "html.parser")

    conferences = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        # 「プログラム」「東京プログラム」「大阪プログラム」等のリンクを対象にする
        if "プログラム" not in text or "満足度" in text:
            continue

        m = re.search(r"/hr-conference/(?:tokyo/|osaka/|tech/)?(\d+)/?$", href)
        if not m:
            continue

        id_str = m.group(1)
        # YYYYMM形式（6桁）なら先頭4桁が年。それ以外（01, 02等の古い連番）は対象外にする
        if len(id_str) == 6:
            year = int(id_str[:4])
            if year < args.min_year:
                continue
        else:
            continue  # 2012年以前の連番形式（tokyo/09等）は対象外

        full_url = href if href.startswith("http") else "https://jinjibu.jp" + href
        program_url = full_url.rstrip("/") + "/program.php"

        key = program_url
        if key in conferences:
            continue
        conferences[key] = {"label": text, "program_url": program_url}

    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["label", "program_url"])
        writer.writeheader()
        writer.writerows(conferences.values())

    print(f"完了。{len(conferences):,}件の開催回を {args.output} に書き出しました。")


if __name__ == "__main__":
    main()
