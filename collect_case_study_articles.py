"""
collect_case_study_articles.py
============================================================
SaaS製品の事例ハブページ一覧（product_name, google_search_url, 事例ページ）を読み込み、
各サイトを巡回して個別記事のURL・公開日を収集する。AIコストなし。

CSVのproduct_name列は検索に使った名前であり表記がズレている可能性があるため使わない。
サイト名は各サイトのHTMLから (1) og:site_name (2) <title> (3) ドメイン名 の優先順で
1サイトにつき1回だけ取得し、そのサイトの全記事に使い回す。

出力は3列のみ:
    site_name, article_url, published_date

使い方:
    python collect_case_study_articles.py --input saas_products.csv --output articles.csv

途中再開:
    --append   既存の出力ファイルにある article_url をスキップして再開する
    --limit N  テスト用に処理するサイト数を制限
============================================================
"""

import argparse
import csv
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from urllib.parse import urljoin, urlparse

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}
REQUEST_TIMEOUT = 8
LISTING_THRESHOLD = 5
SAFETY_MAX_PAGES = 3000
MIN_ARTICLE_TEXT_LEN = 50

LINK_RE = re.compile(r'<a\s+[^>]*href=["\']([^"\']+)["\']', re.IGNORECASE)
OG_SITE_NAME_RE = re.compile(
    r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE
)
TITLE_TAG_RE = re.compile(r"<title[^>]*>([^<]+)</title>", re.IGNORECASE)
META_DATE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:article:published_time|article:modified_time|'
    r'og:article:published_time|datePublished|publish[-_]?date)["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
TIME_TAG_RE = re.compile(r'<time[^>]+datetime=["\']([^"\']+)["\']', re.IGNORECASE)
JP_DATE_RE = re.compile(r"(20\d{2})[年/\-.](\d{1,2})[月/\-.](\d{1,2})[日]?")

# 非記事ファイル（画像・PDF・Excel・ZIP等）の拡張子。これらは「記事」として扱わない。
NON_ARTICLE_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".bmp", ".ico",
    ".pdf", ".zip", ".css", ".js", ".xml", ".json",
    ".mp4", ".mp3", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
)


def is_non_article_file(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(NON_ARTICLE_EXTENSIONS)


# クローラートラップ検出: 同じパスセグメントが連続して繰り返されるURL
# （例: /works/works/works/.../peach-john/ のような無限再帰パス）
TRAP_PATTERN = re.compile(r"/([\w\-]+)/\1(/|$)")


def is_trap_url(url: str) -> bool:
    return bool(TRAP_PATTERN.search(url))

# タイトルからサイト名を切り出す際の区切り文字候補
TITLE_SEPARATORS = ["｜", "|", "-", "–", "：", ":", "―"]


def get_base_prefix(url: str) -> str:
    p = urlparse(url)
    path = p.path
    if not path.endswith("/"):
        path = path.rsplit("/", 1)[0] + "/"
    first_seg = path.split("/")[1] if len(path.split("/")) > 1 else ""
    prefix_path = f"/{first_seg}/" if first_seg else "/"
    return f"{p.scheme}://{p.netloc}{prefix_path}"


def fetch(url: str, session: requests.Session):
    try:
        resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code >= 400:
            return None
        resp.encoding = resp.apparent_encoding
        return resp.text
    except Exception:
        return None


def extract_site_name(html: str, url: str) -> str:
    """og:site_name → titleタグの一部 → ドメイン名 の優先順でサイト名を決定する。"""
    m = OG_SITE_NAME_RE.search(html)
    if m:
        name = m.group(1).strip()
        if name:
            return name

    domain = urlparse(url).netloc
    domain_normalized = re.sub(r"^www\.", "", domain).lower()

    m = TITLE_TAG_RE.search(html)
    if m:
        title = m.group(1).strip()
        for sep in TITLE_SEPARATORS:
            if sep in title:
                parts = [p.strip() for p in title.split(sep) if p.strip()]
                if len(parts) >= 2:
                    # ドメイン名と一致する（またはドメインに含まれる）セグメントがあれば、それを優先する
                    # （「記事タイトル | サイト名」「ページ名 - ブランド名」のどちらの並びでも対応できる）
                    for part in parts:
                        part_normalized = re.sub(r"[\s　]", "", part).lower()
                        if part_normalized and (
                            part_normalized in domain_normalized or domain_normalized.split(".")[0] in part_normalized
                        ):
                            return part
                    # 一致しない場合は、慣習的にサイト名が来ることが多い最後のセグメントを採用
                    if 1 <= len(parts[-1]) <= 30:
                        return parts[-1]
        if title and len(title) <= 30:
            return title

    return domain_normalized.split(".")[0]


def extract_links(html: str, base_url: str, prefix: str):
    links = set()
    for href in LINK_RE.findall(html):
        if href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        try:
            resolved = urljoin(base_url, href)
        except Exception:
            continue
        if is_non_article_file(resolved):
            continue
        if is_trap_url(resolved):
            continue
        if resolved.startswith(prefix):
            links.add(resolved.split("#")[0])
    return links


def extract_publish_date(html: str) -> str:
    m = META_DATE_RE.search(html)
    if m:
        return m.group(1).strip()[:10]
    m = TIME_TAG_RE.search(html)
    if m:
        return m.group(1).strip()[:10]
    m = JP_DATE_RE.search(html)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return ""


def crawl_site(hub_url: str, concurrency: int, session: requests.Session, time_budget: float = 45.0):
    """
    1サイト分をクロールする。
    戻り値: (site_name, [(article_url, published_date), ...], truncated)
    truncated は time_budget 超過により打ち切られた場合 True になる
    （SAFETY_MAX_PAGES到達や、キューが自然に尽きた場合はFalse）。

    time_budget秒を超えたら、そのサイトの巡回を打ち切ってその時点までの結果を返す
    （巨大サイト1つが並列枠を長時間占有して全体の速度を落とすのを防ぐため）。
    """
    start = time.time()

    first_html = fetch(hub_url, session)
    if first_html is None:
        return "", [], False

    site_name = extract_site_name(first_html, hub_url)
    prefix = get_base_prefix(hub_url)

    visited = set()
    queue = [hub_url]
    articles = []
    truncated = False

    while queue and len(visited) < SAFETY_MAX_PAGES:
        if time.time() - start > time_budget:
            truncated = True
            break  # 時間切れ。ここまでの結果を返す（そのサイトを諦めるのではなく部分的な結果として活かす）

        batch = queue[:concurrency]
        queue = queue[len(batch):]
        batch = [u for u in batch if u not in visited]
        if not batch:
            continue

        with ThreadPoolExecutor(max_workers=len(batch)) as ex:
            futures = {ex.submit(fetch, u, session): u for u in batch}
            for fut in as_completed(futures):
                url = futures[fut]
                html = fut.result()
                visited.add(url)
                if html is None:
                    continue

                links = extract_links(html, url, prefix)
                queue.extend(l for l in links if l not in visited)

                if is_non_article_file(url):
                    continue  # 念のための二重チェック（extract_linksで弾き漏れた場合の保険）
                if is_trap_url(url):
                    continue  # クローラートラップの二重チェック

                if len(links) < LISTING_THRESHOLD and len(html) > MIN_ARTICLE_TEXT_LEN:
                    pub_date = extract_publish_date(html)
                    articles.append((url, pub_date))

    if queue and len(visited) >= SAFETY_MAX_PAGES:
        truncated = True  # ページ数上限到達も「打ち切り」扱いにする

    return site_name, articles, truncated


def main():
    parser = argparse.ArgumentParser(description="事例ハブページから記事URL・公開日・サイト名を収集する")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--hub-col", default="事例ページ")
    parser.add_argument("--site-concurrency", type=int, default=20, help="サイトをまたいだ同時処理数")
    parser.add_argument("--page-concurrency", type=int, default=5, help="1サイト内でのページ同時取得数")
    parser.add_argument("--max-site-seconds", type=float, default=45.0, help="1サイトあたりの最大クロール時間（秒）。超えたら打ち切って次へ")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--append", action="store_true")
    args = parser.parse_args()

    with open(args.input, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    hub_urls = []
    seen_hub = set()
    for row in rows:
        hub = row.get(args.hub_col, "").strip()
        if not hub or hub == "なし" or hub in seen_hub:
            continue
        seen_hub.add(hub)
        hub_urls.append(hub)

    print(f"対象サイト数（重複除去後）: {len(hub_urls):,}")

    fieldnames = ["hub_url", "site_name", "article_url", "published_date", "truncated"]

    done_hub_urls = set()
    file_exists = False
    if args.append:
        try:
            with open(args.output, encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    hub = r.get("hub_url", "")
                    if hub:
                        done_hub_urls.add(hub)
            file_exists = True
            print(f"処理済み(再開分): {len(done_hub_urls):,}サイトをスキップ")
        except FileNotFoundError:
            pass

    hub_urls = [u for u in hub_urls if u not in done_hub_urls]

    targets = hub_urls[: args.limit] if args.limit else hub_urls
    print(f"今回処理するサイト数: {len(targets):,}（同時実行数: {args.site_concurrency}）")

    file_mode = "a" if args.append else "w"
    write_header = not (args.append and file_exists)

    write_lock = Lock()
    counter_lock = Lock()
    processed = 0
    total_articles = 0
    start_time = time.time()

    with open(args.output, file_mode, newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
            f.flush()

        session = requests.Session()

        def process_one(hub_url):
            site_name, articles, truncated = crawl_site(hub_url, args.page_concurrency, session, time_budget=args.max_site_seconds)
            return hub_url, site_name, articles, truncated

        with ThreadPoolExecutor(max_workers=args.site_concurrency) as executor:
            futures = [executor.submit(process_one, u) for u in targets]

            for fut in as_completed(futures):
                hub_url, site_name, articles, truncated = fut.result()

                rows_to_write = [
                    {"hub_url": hub_url, "site_name": site_name, "article_url": url,
                     "published_date": date, "truncated": truncated}
                    for url, date in articles
                ]
                if not rows_to_write:
                    # 記事0件でも「処理済み」として記録しておく（再開時に無限リトライしないため）
                    rows_to_write = [{"hub_url": hub_url, "site_name": site_name, "article_url": "",
                                       "published_date": "", "truncated": truncated}]

                with write_lock:
                    writer.writerows(rows_to_write)
                    f.flush()

                with counter_lock:
                    processed += 1
                    total_articles += len(rows_to_write)
                    elapsed = time.time() - start_time
                    rate = processed / elapsed if elapsed > 0 else 0
                    remaining = (len(targets) - processed) / rate if rate > 0 else 0
                    print(
                        f"[{processed:,}/{len(targets):,}] {site_name or hub_url} "
                        f"→ 記事{len(rows_to_write)}件 (累計{total_articles:,}件, "
                        f"経過{elapsed:.0f}秒, 残り約{remaining:.0f}秒)"
                    )

    print(f"完了。結果を {args.output} に書き出しました。")
    print("途中で止める場合は、同じコマンドに --append を付けて再実行すれば続きから再開します。")


if __name__ == "__main__":
    main()
