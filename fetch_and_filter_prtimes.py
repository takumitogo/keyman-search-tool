"""
fetch_and_filter_prtimes.py
============================================================
collect_prtimes_articles.py で集めたPR TIMES記事(company_name/title/
publish_date/article_urlは収集済み)の本文を取得し、本文中に「株式会社」
を含まない記事を除外する。

使い方:
    python fetch_and_filter_prtimes.py --input prtimes_articles.csv \
        --output prtimes_filtered.csv
============================================================
"""

import argparse
import csv
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


def fetch_html(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return resp.text


def extract_body_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def process_article(row):
    url = row["article_url"]
    try:
        html = fetch_html(url)
    except requests.exceptions.RequestException as e:
        return row, "", False, f"取得失敗: {e}"

    body_text = extract_body_text(html)
    has_company = "株式会社" in body_text
    return row, body_text, has_company, "OK"


def main():
    parser = argparse.ArgumentParser(description="PR TIMES記事本文を取得し、株式会社を含む記事だけ残す")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--append", action="store_true",
                         help="既存の出力ファイルがあれば処理済みURLをスキップして続きから再開する")
    args = parser.parse_args()

    with open(args.input, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if args.limit is not None:
        rows = rows[: args.limit]

    done_urls = set()
    file_exists = False
    if args.append:
        try:
            with open(args.output, encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    done_urls.add(r.get("article_url", ""))
            file_exists = True
            print(f"処理済み(再開分): {len(done_urls):,}件をスキップ")
        except FileNotFoundError:
            pass

    targets = [r for r in rows if r["article_url"] not in done_urls]
    print(f"対象記事数: {len(rows):,}（うち今回処理: {len(targets):,}）")
    rows = targets

    fieldnames = ["keyword", "company_name", "title", "publish_date", "article_url", "body_text"]
    write_lock = Lock()
    counter_lock = Lock()
    processed = 0
    kept = 0
    start_time = time.time()

    file_mode = "a" if args.append else "w"
    write_header = not (args.append and file_exists)

    with open(args.output, file_mode, newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
            f.flush()

        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = [executor.submit(process_article, row) for row in rows]
            for fut in as_completed(futures):
                row, body_text, has_company, status = fut.result()
                with counter_lock:
                    processed += 1
                if status == "OK" and has_company:
                    out_row = {k: row.get(k, "") for k in ["keyword", "company_name", "title", "publish_date", "article_url"]}
                    out_row["body_text"] = body_text
                    with write_lock:
                        writer.writerow(out_row)
                        f.flush()
                    with counter_lock:
                        kept += 1
                with counter_lock:
                    elapsed = time.time() - start_time
                    print(f"[{processed:,}/{len(rows):,}] {status} 株式会社あり={has_company} "
                          f"採用累計={kept:,} 経過{elapsed:.0f}秒")
                time.sleep(0.3)

    print(f"\n完了。{len(rows):,}件中 {kept:,}件が「株式会社」を含む記事として残りました。")
    print(f"結果を {args.output} に書き出しました。")


if __name__ == "__main__":
    main()
