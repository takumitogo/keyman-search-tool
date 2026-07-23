"""
submit_keyman_batch.py
============================================================
extract_keyman_from_articles.py と同じ抽出ロジックを、
Message Batches API（50%割引）向けに書き直したもの。フェーズ1（準備・送信）。

流れ:
    1. 入力CSVを読み、既に処理済み（--outputに存在する）article_urlをスキップ
    2. 各記事を取得し、公開日推定・「株式会社」の記載チェックを行う（ここまでAIコストなし）
    3. 対象外（取得失敗・株式会社なし等）は即座に最終出力CSVに書き込む
    4. Claude APIに送る必要がある記事は、バッチリクエストとして貯める
    5. 最大10,000件ごとにバッチを作成・送信し、バッチIDと記事情報の対応表を保存する

このスクリプト単体では結果は返ってこない（最大24時間かかる）。
送信後は collect_keyman_batch.py で結果を回収する。

使い方:
    set ANTHROPIC_API_KEY=sk-ant-xxxxx
    python submit_keyman_batch.py --input articles_for_keyman_with_category_v3.csv \
        --output keyman_candidates_final.csv --batch-state batch_state.jsonl
============================================================
"""

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages/batches"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
MAX_REQUESTS_PER_BATCH = 10000

HEADERS_BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}
REQUEST_TIMEOUT = 15

EXCLUDE_TITLE_KEYWORDS = [
    "代表取締役", "代表", "取締役", "社長", "会長",
    "CEO", "COO", "CTO", "CFO", "執行役員", "監査役", "オーナー", "店主",
]

META_DATE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:article:published_time|article:modified_time|'
    r'og:article:published_time|datePublished|publish[-_]?date)["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
TIME_TAG_RE = re.compile(r'<time[^>]+datetime=["\']([^"\']+)["\']', re.IGNORECASE)
JP_DATE_RE = re.compile(r"(20\d{2})[年/\-.](\d{1,2})[月/\-.](\d{1,2})[日]?")


def extract_date_from_html(raw_html: str) -> str:
    m = META_DATE_RE.search(raw_html)
    if m:
        return m.group(1).strip()[:10]
    m = TIME_TAG_RE.search(raw_html)
    if m:
        return m.group(1).strip()[:10]
    m = JP_DATE_RE.search(raw_html)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return ""


def fetch_article(url: str):
    """記事を取得し、(本文テキスト, 公開日) を返す。失敗時は (None, None)。"""
    try:
        resp = requests.get(url, headers=HEADERS_BROWSER, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding
    except requests.exceptions.RequestException:
        return None, None

    raw_html = resp.text
    date = extract_date_from_html(raw_html)

    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    return text[:8000], date


def build_prompt(company_name: str, article_text: str) -> str:
    exclude_list = "・".join(EXCLUDE_TITLE_KEYWORDS)
    return f"""あなたはB2B営業のためのリサーチアシスタントです。

以下の記事は、「{company_name}」が自社サイトに掲載している導入事例・活用事例記事です。
つまり「{company_name}」はサービスを提供しているベンダー側（インタビュアー側）であり、
記事の中では別の顧客企業が紹介・インタビューされています。

あなたのタスクは、**「{company_name}」自身の社員ではなく、記事内で紹介されている顧客企業側の担当者**を
抽出することです。顧客企業名は記事本文中から特定してください（{company_name}とは異なる社名のはずです）。

重要な絞り込み条件:
- 「{company_name}」（ベンダー側・インタビュアー側）の社員は、記事中に名前が出てきても candidates に含めないでください。
- 顧客企業は「株式会社」の法人のみを対象にしてください。「医療法人」「一般社団法人」等は対象外です。
- 顧客企業側の人物のみを対象にしてください。役職の有無・レベルは問いません。
- ただし顧客企業側の人物であっても「{exclude_list}」等の経営層・役員クラスは candidates に含めないでください。
- 記事本文に実際に明記されている情報のみを抽出してください。推測や補完は絶対にしないでください。
- name には実名のみを入れてください。「A」「S」のようなイニシャルだけの伏字や、「生産管理担当の方」
  のような人物を特定しない説明的な表現は含めないでください。
- 該当者がいない場合は candidates を空配列にしてください。

必ず以下のJSON形式のみで回答してください。前後に説明文やMarkdownのコードブロックは付けないでください。
{{
  "candidates": [
    {{
      "featured_company_name": "記事内で紹介されている顧客企業名",
      "department_matched": "部署名（不明な場合は空文字）",
      "name": "氏名",
      "title": "役職・肩書き（不明な場合は空文字）",
      "contact": "SNSやメールアドレス等。なければ空文字"
    }}
  ]
}}

---記事本文---
{article_text}
---本文ここまで---
"""


def make_custom_id(article_url: str) -> str:
    return hashlib.sha1(article_url.encode("utf-8")).hexdigest()[:40]


def submit_batch(requests_list, api_key: str):
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    resp = requests.post(ANTHROPIC_API_URL, headers=headers, json={"requests": requests_list}, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"バッチ送信エラー (HTTP {resp.status_code}): {resp.text[:500]}")
    return resp.json()


def main():
    parser = argparse.ArgumentParser(description="キーマン抽出をバッチAPI向けに準備・送信する（フェーズ1）")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True, help="最終結果CSV（該当なし/スキップ分はここに直接書き込まれる）")
    parser.add_argument("--batch-state", required=True, help="バッチIDと記事情報の対応表（JSONL）の保存先")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--concurrency", type=int, default=15, help="記事取得の同時実行数")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("エラー: 環境変数 ANTHROPIC_API_KEY が設定されていません。")
        sys.exit(1)

    with open(args.input, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    # 既に最終出力に存在するarticle_urlはスキップ（フェーズ1・2どちらの結果も混ざる出力ファイル）
    done_urls = set()
    output_fieldnames = [
        "corporate_number", "company_name", "article_url", "published_date",
        "date_confidence", "date_method",
        "matched_product_name", "large_category", "medium_category", "matched_small_category",
        "featured_company_name", "department_matched", "name", "title", "contact",
        "verified", "status", "extracted_at", "batch_custom_id",
    ]
    output_exists = os.path.exists(args.output)
    if output_exists:
        with open(args.output, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                done_urls.add(r.get("article_url", ""))
        print(f"処理済み(再開分): {len(done_urls):,}件をスキップ")

    targets = []
    for row in rows:
        url = row.get("article_url", "").strip()
        if not url or url in done_urls:
            continue
        targets.append(row)
        if args.limit is not None and len(targets) >= args.limit:
            break

    print(f"今回準備する記事数: {len(targets):,}")
    if not targets:
        print("対象がありません。")
        return

    out_f = open(args.output, "a", newline="", encoding="utf-8-sig")
    writer = csv.DictWriter(out_f, fieldnames=output_fieldnames)
    if not output_exists:
        writer.writeheader()

    state_f = open(args.batch_state, "a", encoding="utf-8")

    write_lock = Lock()
    counter_lock = Lock()
    processed = 0
    skipped = 0
    queued = 0
    start_time = time.time()

    batch_requests = []  # {"custom_id":..., "params": {...}}
    pending_meta = {}     # custom_id -> row情報+article_text(検証用)

    def process_one(row):
        article_url = row.get("article_url", "").strip()
        company_name = row.get("company_name") or row.get("site_name", "")
        text, extracted_date = fetch_article(article_url)
        return row, text, extracted_date

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [executor.submit(process_one, row) for row in targets]

        for fut in as_completed(futures):
            row, text, extracted_date = fut.result()
            article_url = row.get("article_url", "").strip()
            company_name = row.get("company_name") or row.get("site_name", "")
            published_date = row.get("published_date", "") or extracted_date or ""
            now = time.strftime("%Y-%m-%dT%H:%M:%S")

            common = {
                "corporate_number": row.get("corporate_number") or row.get("hub_url", ""),
                "company_name": company_name,
                "article_url": article_url,
                "published_date": published_date,
                "date_confidence": "確定" if extracted_date else ("既存" if row.get("published_date") else ""),
                "date_method": "HTMLメタタグ" if extracted_date else "",
                "matched_product_name": row.get("matched_product_name", ""),
                "large_category": row.get("large_category", ""),
                "medium_category": row.get("medium_category", ""),
                "matched_small_category": row.get("matched_small_category", ""),
            }

            with counter_lock:
                processed += 1

            if text is None:
                with write_lock:
                    writer.writerow({**common, "featured_company_name": "", "department_matched": "",
                                      "name": "", "title": "", "contact": "", "verified": "",
                                      "status": "記事取得失敗", "extracted_at": now, "batch_custom_id": ""})
                with counter_lock:
                    skipped += 1
                continue

            if len(text) < 50:
                with write_lock:
                    writer.writerow({**common, "featured_company_name": "", "department_matched": "",
                                      "name": "", "title": "", "contact": "", "verified": "",
                                      "status": "本文が短すぎる", "extracted_at": now, "batch_custom_id": ""})
                with counter_lock:
                    skipped += 1
                continue

            if "株式会社" not in text:
                with write_lock:
                    writer.writerow({**common, "featured_company_name": "", "department_matched": "",
                                      "name": "", "title": "", "contact": "", "verified": "",
                                      "status": "スキップ（株式会社の記載なし）", "extracted_at": now, "batch_custom_id": ""})
                with counter_lock:
                    skipped += 1
                continue

            # ここまで来た記事はClaude APIに送る対象
            custom_id = make_custom_id(article_url)
            prompt = build_prompt(company_name, text)
            with counter_lock:
                batch_requests.append({
                    "custom_id": custom_id,
                    "params": {
                        "model": args.model,
                        "max_tokens": 1024,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                })
                pending_meta[custom_id] = {**common, "article_text": text}
                queued += 1

            if processed % 50 == 0:
                elapsed = time.time() - start_time
                print(f"[{processed:,}/{len(targets):,}] 取得済み (スキップ{skipped:,}, 送信待ち{queued:,}) 経過{elapsed:.0f}秒")

    out_f.close()
    print(f"記事取得完了。スキップ{skipped:,}件、バッチ送信対象{queued:,}件。")

    if not batch_requests:
        print("バッチ送信対象がありません（全てスキップされました）。")
        state_f.close()
        return

    # 最大10,000件ごとに分割して送信
    batch_ids = []
    for i in range(0, len(batch_requests), MAX_REQUESTS_PER_BATCH):
        chunk = batch_requests[i:i + MAX_REQUESTS_PER_BATCH]
        result = submit_batch(chunk, api_key)
        batch_id = result.get("id")
        batch_ids.append(batch_id)
        print(f"バッチ送信完了: {batch_id}（{len(chunk):,}件）")

        # このバッチに含まれるcustom_idのメタ情報を状態ファイルに保存
        for req in chunk:
            cid = req["custom_id"]
            state_f.write(json.dumps({"batch_id": batch_id, "custom_id": cid, **pending_meta[cid]}, ensure_ascii=False) + "\n")

    state_f.close()
    print()
    print(f"送信したバッチID: {batch_ids}")
    print("結果は最大24時間程度で準備できます。以下で回収してください:")
    print(f"  python collect_keyman_batch.py --batch-state {args.batch_state} --output {args.output}")


if __name__ == "__main__":
    main()
