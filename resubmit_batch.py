"""
resubmit_batch.py
============================================================
retry_failed_prtimes_batch.py 等で作った「再送信用JSONL」を、
新しいバッチとしてAnthropic APIに送信するだけの小さなスクリプト。

使い方:
    python resubmit_batch.py --input prtimes_batch_retry.jsonl
============================================================
"""

import argparse
import json

import anthropic


def main():
    parser = argparse.ArgumentParser(description="JSONLファイルを新しいバッチとして送信する")
    parser.add_argument("--input", required=True)
    args = parser.parse_args()

    with open(args.input, encoding="utf-8") as f:
        requests = [json.loads(line) for line in f if line.strip()]

    print(f"{len(requests):,}件のリクエストを送信します...")

    client = anthropic.Anthropic()
    batch = client.messages.batches.create(requests=requests)

    print(f"\n送信完了。新しいbatch_id: {batch.id}")
    print("この batch_id を、既存の prtimes_batch_batch_ids.txt に追記しておくと、")
    print("次回の collect_prtimes_keyman_batch.py --batch-id-file 実行時にまとめて回収できます。")

    with open("prtimes_batch_batch_ids.txt", "a", encoding="utf-8") as f:
        f.write(f"\n{batch.id}")
    print("→ prtimes_batch_batch_ids.txt に自動で追記しました。")


if __name__ == "__main__":
    main()
