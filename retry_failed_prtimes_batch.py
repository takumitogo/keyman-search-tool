"""
retry_failed_prtimes_batch.py
============================================================
submit_prtimes_keyman_batch.py で送信したバッチのうち、クレジット切れ等で
失敗したリクエストだけを検出し、再送信用の新しいJSONLファイルを作る。

使い方:
    python retry_failed_prtimes_batch.py --batch-id msgbatch_xxx \
        --original-jsonl prtimes_batch.jsonl \
        --output-jsonl prtimes_batch_retry.jsonl

その後、通常のバッチ送信と同じ要領で再送信する:
    python -c "
import anthropic, json
client = anthropic.Anthropic()
requests = [json.loads(l) for l in open('prtimes_batch_retry.jsonl', encoding='utf-8')]
batch = client.messages.batches.create(requests=requests)
print(batch.id)
"
============================================================
"""

import argparse
import json

import anthropic


def main():
    parser = argparse.ArgumentParser(description="失敗したバッチリクエストだけを再送信用に抽出する")
    parser.add_argument("--batch-id", action="append", default=None,
                         help="バッチID（複数回指定可）")
    parser.add_argument("--batch-id-file", default=None,
                         help="バッチIDが1行ずつ書かれたテキストファイル（分割送信時に使用）")
    parser.add_argument("--original-jsonl", required=True, help="最初に送信したバッチのJSONLファイル（全チャンク分）")
    parser.add_argument("--output-jsonl", required=True)
    args = parser.parse_args()

    batch_ids = list(args.batch_id) if args.batch_id else []
    if args.batch_id_file:
        with open(args.batch_id_file, encoding="utf-8") as f:
            batch_ids += [line.strip() for line in f if line.strip()]
    if not batch_ids:
        print("エラー: --batch-id または --batch-id-file のいずれかを指定してください。")
        return

    client = anthropic.Anthropic()

    # custom_id -> 元のリクエスト全体（再送信用）
    with open(args.original_jsonl, encoding="utf-8") as f:
        original_requests = {}
        for line in f:
            req = json.loads(line)
            original_requests[req["custom_id"]] = req

    failed_ids = []
    succeeded = 0
    for batch_id in batch_ids:
        batch = client.messages.batches.retrieve(batch_id)
        print(f"[{batch_id}] ステータス: {batch.processing_status}")
        if batch.processing_status != "ended":
            print("  まだ処理が終わっていません。このバッチはスキップします。")
            continue

        for result in client.messages.batches.results(batch_id):
            if result.result.type == "succeeded":
                succeeded += 1
                continue
            # errored / expired / canceled 等はすべて再送信対象とする
            error_type = getattr(result.result, "type", "unknown")
            error_detail = ""
            if hasattr(result.result, "error"):
                error_detail = str(result.result.error)
            print(f"  失敗: {result.custom_id} ({error_type}) {error_detail[:100]}")
            failed_ids.append(result.custom_id)

    print(f"\n成功: {succeeded:,}件")
    print(f"失敗(再送信対象): {len(failed_ids):,}件")

    retry_requests = [original_requests[cid] for cid in failed_ids if cid in original_requests]
    missing = len(failed_ids) - len(retry_requests)
    if missing:
        print(f"警告: {missing}件は元のJSONLにcustom_idが見つかりませんでした（スキップ）")

    with open(args.output_jsonl, "w", encoding="utf-8") as f:
        for req in retry_requests:
            f.write(json.dumps(req, ensure_ascii=False) + "\n")

    print(f"\n再送信用JSONLを {args.output_jsonl} に書き出しました（{len(retry_requests):,}件）")
    print("クレジットを追加した後、以下で新しいバッチとして再送信してください:")
    print(f"""
python -c "
import anthropic, json
client = anthropic.Anthropic()
requests = [json.loads(l) for l in open('{args.output_jsonl}', encoding='utf-8')]
batch = client.messages.batches.create(requests=requests)
print('新しいbatch_id:', batch.id)
"
""")


if __name__ == "__main__":
    main()
