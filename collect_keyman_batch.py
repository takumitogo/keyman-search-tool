"""
collect_keyman_batch.py
============================================================
submit_keyman_batch.py で送信したバッチの結果を回収する（フェーズ2）。
バッチの処理が終わっていなければ「まだ処理中」と表示するだけで終了する。
何度でも再実行できる（終わっているバッチだけ回収し、既に回収済みのものはスキップする）。

使い方:
    set ANTHROPIC_API_KEY=sk-ant-xxxxx
    python collect_keyman_batch.py --batch-state batch_state.jsonl --output keyman_candidates_final.csv
============================================================
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import defaultdict

import requests

ANTHROPIC_VERSION = "2023-06-01"
PRICE_PER_MILLION_INPUT_USD = 1.0
PRICE_PER_MILLION_OUTPUT_USD = 5.0

EXCLUDE_TITLE_KEYWORDS = [
    "代表取締役", "代表", "取締役", "社長", "会長",
    "CEO", "COO", "CTO", "CFO", "執行役員", "監査役", "オーナー", "店主",
]
PLACEHOLDER_NAME_KEYWORDS = ["担当の方", "担当者", "ご担当者", "の方", "様（", "スタッフ", "メンバー", "関係者"]


def is_excluded_title(title: str) -> bool:
    if not title:
        return False
    return any(kw in title for kw in EXCLUDE_TITLE_KEYWORDS)


def is_placeholder_name(name: str) -> bool:
    if not name:
        return True
    stripped = name.strip()
    parts = [p for p in re.split(r"[.\s　]+", stripped) if p]
    if parts and all(len(p) <= 2 and re.fullmatch(r"[A-Za-zＡ-Ｚａ-ｚ]+", p) for p in parts):
        if len(parts) >= 2 or len(stripped) <= 2:
            return True
    if any(kw in stripped for kw in PLACEHOLDER_NAME_KEYWORDS):
        return True
    return False


def normalize(s: str) -> str:
    return re.sub(r"[\s　]", "", str(s or ""))


def verify_name_in_text(name: str, article_text: str) -> str:
    if not name:
        return "氏名なし"
    return "一致確認OK" if normalize(name) in normalize(article_text) else "不一致（要確認・削除推奨）"


def parse_json_response(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"```json", "", text, flags=re.IGNORECASE).replace("```", "").strip()
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace == -1 or last_brace == -1:
        raise RuntimeError(f"JSONを抽出できませんでした: {text[:200]}")
    text = text[first_brace: last_brace + 1]
    parsed = json.loads(text)
    if not isinstance(parsed.get("candidates"), list):
        parsed["candidates"] = []
    return parsed


def get_batch_status(batch_id: str, api_key: str):
    headers = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}
    resp = requests.get(f"https://api.anthropic.com/v1/messages/batches/{batch_id}", headers=headers, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"バッチ状態取得エラー (HTTP {resp.status_code}): {resp.text[:300]}")
    return resp.json()


def get_batch_results(results_url: str, api_key: str):
    """結果はJSONL形式（1行1レコード）で返ってくる。"""
    headers = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}
    resp = requests.get(results_url, headers=headers, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"バッチ結果取得エラー (HTTP {resp.status_code}): {resp.text[:300]}")
    lines = resp.text.strip().split("\n")
    return [json.loads(ln) for ln in lines if ln.strip()]


def main():
    parser = argparse.ArgumentParser(description="バッチAPIの結果を回収する（フェーズ2）")
    parser.add_argument("--batch-state", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("エラー: 環境変数 ANTHROPIC_API_KEY が設定されていません。")
        sys.exit(1)

    # 状態ファイルを読み込み、バッチID単位でグループ化
    by_batch = defaultdict(dict)  # batch_id -> {custom_id: meta}
    with open(args.batch_state, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            by_batch[rec["batch_id"]][rec["custom_id"]] = rec

    # 既に最終出力にあるarticle_urlは回収済みとみなしてスキップ
    done_urls = set()
    output_exists = os.path.exists(args.output)
    if output_exists:
        with open(args.output, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r.get("batch_custom_id"):
                    done_urls.add(r.get("article_url", ""))

    fieldnames = [
        "corporate_number", "company_name", "article_url", "published_date",
        "date_confidence", "date_method",
        "matched_product_name", "large_category", "medium_category", "matched_small_category",
        "featured_company_name", "department_matched", "name", "title", "contact",
        "verified", "status", "extracted_at", "batch_custom_id",
    ]

    out_f = open(args.output, "a", newline="", encoding="utf-8-sig")
    writer = csv.DictWriter(out_f, fieldnames=fieldnames)
    if not output_exists:
        writer.writeheader()

    total_input_tokens = 0
    total_output_tokens = 0
    collected_count = 0

    for batch_id, meta_map in by_batch.items():
        print(f"バッチ {batch_id}: {len(meta_map):,}件 の状態を確認中...")
        status = get_batch_status(batch_id, api_key)
        processing_status = status.get("processing_status", "unknown")

        if processing_status != "ended":
            counts = status.get("request_counts", {})
            print(f"  まだ処理中です（状態: {processing_status}）。内訳: {counts}")
            print("  時間を置いてから再実行してください。")
            continue

        results_url = status.get("results_url")
        if not results_url:
            print("  結果URLが見つかりません。スキップします。")
            continue

        results = get_batch_results(results_url, api_key)
        print(f"  結果 {len(results):,}件を取得しました。")

        for r in results:
            custom_id = r.get("custom_id")
            meta = meta_map.get(custom_id)
            if meta is None:
                continue
            if meta["article_url"] in done_urls:
                continue  # 既に処理済み（再実行時の重複防止）

            now = time.strftime("%Y-%m-%dT%H:%M:%S")
            common = {k: meta[k] for k in [
                "corporate_number", "company_name", "article_url", "published_date",
                "date_confidence", "date_method", "matched_product_name",
                "large_category", "medium_category", "matched_small_category",
            ]}

            result_body = r.get("result", {})
            result_type = result_body.get("type")

            if result_type != "succeeded":
                writer.writerow({**common, "featured_company_name": "", "department_matched": "",
                                  "name": "", "title": "", "contact": "", "verified": "",
                                  "status": f"バッチエラー: {result_type}", "extracted_at": now,
                                  "batch_custom_id": custom_id})
                collected_count += 1
                continue

            message = result_body.get("message", {})
            usage = message.get("usage", {})
            total_input_tokens += usage.get("input_tokens", 0)
            total_output_tokens += usage.get("output_tokens", 0)

            content_blocks = message.get("content", [])
            text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")

            try:
                parsed = parse_json_response(text)
            except Exception as e:
                writer.writerow({**common, "featured_company_name": "", "department_matched": "",
                                  "name": "", "title": "", "contact": "", "verified": "",
                                  "status": f"抽出エラー: {e}", "extracted_at": now,
                                  "batch_custom_id": custom_id})
                collected_count += 1
                continue

            article_text = meta.get("article_text", "")
            candidates = parsed.get("candidates", [])
            filtered = []
            seen_signatures = set()
            for c in candidates:
                if is_excluded_title(c.get("title", "")):
                    continue
                if is_placeholder_name(c.get("name", "")):
                    continue
                # モデルが同じ候補を繰り返し出力してしまうことがあるため、
                # 氏名・役職・部署・顧客企業が全て一致するものは1件にまとめる
                signature = (
                    normalize(c.get("name", "")), normalize(c.get("title", "")),
                    normalize(c.get("department_matched", "")), normalize(c.get("featured_company_name", "")),
                )
                if signature in seen_signatures:
                    continue
                seen_signatures.add(signature)
                c["verified"] = verify_name_in_text(c.get("name", ""), article_text)
                filtered.append(c)

            if not filtered:
                writer.writerow({**common, "featured_company_name": "", "department_matched": "",
                                  "name": "", "title": "", "contact": "", "verified": "",
                                  "status": "該当なし", "extracted_at": now, "batch_custom_id": custom_id})
                collected_count += 1
            else:
                for c in filtered:
                    writer.writerow({
                        **common,
                        "featured_company_name": c.get("featured_company_name", ""),
                        "department_matched": c.get("department_matched", ""),
                        "name": c.get("name", ""),
                        "title": c.get("title", ""),
                        "contact": c.get("contact", ""),
                        "verified": c.get("verified", ""),
                        "status": "OK",
                        "extracted_at": now,
                        "batch_custom_id": custom_id,
                    })
                    collected_count += 1

        out_f.flush()

    out_f.close()

    cost = (total_input_tokens / 1_000_000) * PRICE_PER_MILLION_INPUT_USD / 2 \
        + (total_output_tokens / 1_000_000) * PRICE_PER_MILLION_OUTPUT_USD / 2
    print()
    print(f"今回回収した行数: {collected_count:,}")
    print(f"入力トークン: {total_input_tokens:,} / 出力トークン: {total_output_tokens:,}")
    print(f"推定コスト（バッチ50%割引適用後）: 約${cost:.2f}")


if __name__ == "__main__":
    main()
