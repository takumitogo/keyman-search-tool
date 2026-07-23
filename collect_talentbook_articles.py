"""
collect_talentbook_articles.py
============================================================
TalentBook (talent-book.jp) の企業一覧ページから企業を収集し、各企業の
記事一覧（ノウハウ/ストーリー等）から記事URLを収集する。

企業一覧: https://www.talent-book.jp/companies
企業ページ内の記事一覧は「/{company_slug}/knowhows」等のパスにある。

使い方:
    python collect_talentbook_articles.py --output talentbook_articles.csv --max-companies 50
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

COMPANY_URL_RE = re.compile(r"^https?://www\.talent-book\.jp/([a-zA-Z0-9_-]+)/?$")
ARTICLE_URL_RE = re.compile(r"^https?://www\.talent-book\.jp/[a-zA-Z0-9_-]+/(knowhows|stories|contents)/\d+/?$")


def fetch_html(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return resp.text


def extract_company_links(html: str):
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("http"):
            href = "https://www.talent-book.jp" + href if href.startswith("/") else href
        m = COMPANY_URL_RE.match(href.split("?")[0])
        if m and m.group(1) not in ("companies", "categories", "contents", "feature", "faq", "login", "agreement"):
            links.add(href.rstrip("/"))
    return links


def extract_article_links(html: str):
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("http"):
            href = "https://www.talent-book.jp" + href if href.startswith("/") else href
        if ARTICLE_URL_RE.match(href.split("?")[0]):
            links.add(href.split("?")[0].rstrip("/"))
    return links


def collect_companies(max_pages: int, delay: float):
    companies = set()
    empty_streak = 0
    for page in range(1, max_pages + 1):
        url = f"https://www.talent-book.jp/companies?page={page}"
        try:
            html = fetch_html(url)
        except requests.exceptions.RequestException as e:
            print(f"  企業一覧 p={page} 取得失敗: {e}")
            break
        links = extract_company_links(html)
        new_links = links - companies
        print(f"  企業一覧 p={page}: {len(links)}件中 新規{len(new_links)}件")
        if not links:
            empty_streak += 1
        else:
            empty_streak = 0
        companies |= links
        if empty_streak >= 2:
            break
        time.sleep(delay)
    return companies


def collect_articles_for_company(company_url: str, max_pages: int, delay: float):
    slug = company_url.rstrip("/").rsplit("/", 1)[-1]
    all_links = set()
    # 記事一覧は /{slug}/contents?filter=story（ストーリー）と ?filter=knowhow（ノウハウ）
    for content_filter in ("story", "knowhow"):
        empty_streak = 0
        for page in range(1, max_pages + 1):
            url = f"https://www.talent-book.jp/{slug}/contents?filter={content_filter}&page={page}"
            try:
                html = fetch_html(url)
            except requests.exceptions.RequestException:
                break
            links = extract_article_links(html)
            new_links = links - all_links
            if not links:
                empty_streak += 1
            else:
                empty_streak = 0
            all_links |= links
            if empty_streak >= 2:
                break
            time.sleep(delay)
    return all_links


def main():
    parser = argparse.ArgumentParser(description="TalentBookの記事URLを収集する")
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-companies", type=int, default=None, help="収集対象の企業数上限(安全弁)")
    parser.add_argument("--max-company-pages", type=int, default=50)
    parser.add_argument("--max-article-pages", type=int, default=20)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--debug-save-html", type=str, default=None,
                         help="企業一覧1ページ目の生HTMLをこのパスに保存する(調査用)")
    args = parser.parse_args()

    if args.debug_save_html:
        try:
            debug_html = fetch_html("https://www.talent-book.jp/companies?page=1")
            with open(args.debug_save_html, "w", encoding="utf-8") as f:
                f.write(debug_html)
            print(f"デバッグ用に企業一覧1ページ目の生HTMLを {args.debug_save_html} に保存しました")
            links = extract_company_links(debug_html)
            print(f"抽出できた企業リンク数: {len(links)}")
            for l in list(links)[:5]:
                print("  ", l)
        except requests.exceptions.RequestException as e:
            print(f"デバッグ取得失敗: {e}")

    print("=== 企業一覧を収集 ===")
    companies = collect_companies(args.max_company_pages, args.delay)
    print(f"企業数: {len(companies):,}")

    company_list = sorted(companies)
    if args.max_companies is not None:
        company_list = company_list[: args.max_companies]

    rows = []
    for i, company_url in enumerate(company_list, 1):
        slug = company_url.rstrip("/").rsplit("/", 1)[-1]
        print(f"[{i}/{len(company_list)}] {slug}")
        articles = collect_articles_for_company(company_url, args.max_article_pages, args.delay)
        for a in articles:
            rows.append({"company_slug": slug, "article_url": a})
        print(f"  → {len(articles):,}件")

    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["company_slug", "article_url"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n完了。{len(rows):,} 件の記事URLを {args.output} に書き出しました。")


if __name__ == "__main__":
    main()
