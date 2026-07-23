"""
collect_prtimes_keyman_batch.py
============================================================
submit_prtimes_keyman_batch.py で送信したBatch APIの結果を回収し、
最終的なキーマンCSV（会社名・部署・役職・担当者名・担当テーマ・
記事タイトル・公開日・URL）を書き出す。

何度でも再実行可（終わっているバッチだけ回収する）。

使い方:
    python collect_prtimes_keyman_batch.py --batch-id msgbatch_xxx \
        --meta-file prtimes_batch_requests_meta.json \
        --output prtimes_keyman.csv
============================================================
"""

import argparse
import csv
import json

import anthropic


def parse_extraction(text: str):
    """モデル応答（JSON配列のはず）をパースする。前後に余計な文字が付いた場合も救済する。"""
    text = text.strip()
    # ```json ... ``` のようなコードブロックで囲まれている場合を除去
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # JSON配列部分だけを正規表現的に切り出す最後の手段
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return None
        return None


def main():
    parser = argparse.ArgumentParser(description="PR TIMES Batch API結果を回収する")
    parser.add_argument("--batch-id", action="append", default=None,
                         help="バッチID（複数回指定可: --batch-id id1 --batch-id id2）")
    parser.add_argument("--batch-id-file", default=None,
                         help="バッチIDが1行ずつ書かれたテキストファイル（分割送信時に使用）")
    parser.add_argument("--meta-file", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    batch_ids = list(args.batch_id) if args.batch_id else []
    if args.batch_id_file:
        with open(args.batch_id_file, encoding="utf-8") as f:
            batch_ids += [line.strip() for line in f if line.strip()]

    if not batch_ids:
        print("エラー: --batch-id または --batch-id-file のいずれかを指定してください。")
        return

    print(f"対象バッチ数: {len(batch_ids)}")

    with open(args.meta_file, encoding="utf-8") as f:
        meta = json.load(f)

    client = anthropic.Anthropic()

    rows = []
    parse_failures = 0
    total_people = 0

    for batch_id in batch_ids:
        batch = client.messages.batches.retrieve(batch_id)
        print(f"\n[{batch_id}] ステータス: {batch.processing_status}")

        if batch.processing_status != "ended":
            print("  まだ処理が終わっていません。このバッチはスキップします（終わってから再実行してください）。")
            continue

        for result in client.messages.batches.results(batch_id):
            custom_id = result.custom_id
            info = meta.get(custom_id, {})

            if result.result.type != "succeeded":
                print(f"  [{custom_id}] 失敗: {result.result.type}")
                continue

            message = result.result.message
            text = "".join(block.text for block in message.content if block.type == "text")
            people = parse_extraction(text)

            if people is None:
                parse_failures += 1
                print(f"  [{custom_id}] JSONパース失敗、スキップ")
                continue

            for person in people:
                rows.append({
                    "company": person.get("company", ""),
                    "department": person.get("department", ""),
                    "title": person.get("title", ""),
                    "name": person.get("name", ""),
                    "theme": person.get("theme", ""),
                    "article_title": info.get("title", ""),
                    "publish_date": info.get("publish_date", ""),
                    "article_url": info.get("article_url", ""),
                    "keyword": info.get("keyword", ""),
                })
                total_people += 1

    fieldnames = ["company", "department", "title", "name", "theme",
                  "article_title", "publish_date", "article_url", "keyword"]
    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n完了。{total_people:,}人分のキーマン情報を抽出しました。")
    print(f"JSONパース失敗: {parse_failures:,}件")
    print(f"結果を {args.output} に書き出しました。")


if __name__ == "__main__":
    main()
