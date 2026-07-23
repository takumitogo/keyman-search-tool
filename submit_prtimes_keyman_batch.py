"""
submit_prtimes_keyman_batch.py
============================================================
fetch_and_filter_prtimes.py で絞り込んだPR TIMES記事を、Claude Batch API
に送信し、以下を構造化抽出する（JSON形式で応答させる）:
    - 会社名
    - 部署
    - 役職
    - 担当者名
    - 担当テーマ（発表・登壇・就任などの文脈）
    - 記事タイトル
    - 公開日
    - URL

1記事に複数人物が登場する場合は、複数件のJSONオブジェクトを配列で返す
よう指示する。

使い方:
    python submit_prtimes_keyman_batch.py --input prtimes_filtered_v2.csv \
        --batch-file prtimes_batch_requests.jsonl
    （その後、submitted batch idを controlしてcollect_prtimes_keyman_batch.pyで回収）
============================================================
"""

import argparse
import csv
import json

import anthropic

SYSTEM_PROMPT = """あなたはプレスリリース本文から企業のキーマン（部署役職を持つ担当者）情報を
抽出するアシスタントです。以下のJSON配列形式で**のみ**回答してください。前置きや説明、
Markdownのコードブロック記号は一切不要です。該当する人物が本文中に見つからない場合は
空配列 [] を返してください。

各要素のフィールド:
- company: 会社名（法人格を含む正式名称。本文から判断できる範囲でよい）
- department: 部署名（不明な場合は空文字）
- title: 役職名（部長・課長・マネージャー等。不明な場合は空文字）
- name: 担当者の氏名（フルネーム。姓名の間にスペースがあれば保持する）
- theme: その人物が本文中で担当・言及されているテーマや文脈を一言で
  （例: "新商品発表の責任者", "登壇者として紹介", "就任のお知らせ" 等）

出力例:
[
  {"company": "株式会社サンプル", "department": "マーケティング本部", "title": "部長", "name": "山田太郎", "theme": "新サービス発表の責任者"}
]
"""

USER_PROMPT_TEMPLATE = """以下はプレスリリース記事の本文です。この中から、部署役職を持つ
担当者（部長・課長・マネージャー・マネジャー等の肩書きを持つ人物）の情報を全て抽出してください。

---
{body_text}
---
"""


def build_batch_requests(rows, max_chars=6000):
    requests_list = []
    for i, row in enumerate(rows):
        body = row["body_text"][:max_chars]  # 長すぎる本文はコスト抑制のため切り詰め
        custom_id = f"prtimes-{i:06d}"
        requests_list.append({
            "custom_id": custom_id,
            "params": {
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1500,
                "system": SYSTEM_PROMPT,
                "messages": [
                    {"role": "user", "content": USER_PROMPT_TEMPLATE.format(body_text=body)}
                ],
            },
        })
    return requests_list


def main():
    parser = argparse.ArgumentParser(description="PR TIMES記事をClaude Batch APIに送信するリクエストを作成する")
    parser.add_argument("--input", required=True)
    parser.add_argument("--batch-file", required=True, help="バッチリクエストJSONLの保存先(ローカル控え用)")
    parser.add_argument("--meta-file", default=None,
                         help="custom_id -> 元記事情報(url/title/date/keyword)の対応表を保存するJSON")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--submit", action="store_true",
                         help="実際にAnthropic Batch APIへ送信する（付けない場合はJSONL作成のみ）")
    parser.add_argument("--chunk-size", type=int, default=6000,
                         help="1バッチあたりのリクエスト数（256MB上限を超えないよう分割送信する）")
    args = parser.parse_args()

    with open(args.input, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if args.limit is not None:
        rows = rows[: args.limit]

    print(f"対象記事数: {len(rows):,}")

    batch_requests = build_batch_requests(rows)

    with open(args.batch_file, "w", encoding="utf-8") as f:
        for req in batch_requests:
            f.write(json.dumps(req, ensure_ascii=False) + "\n")
    print(f"バッチリクエストを {args.batch_file} に保存しました（{len(batch_requests):,}件）")

    meta_path = args.meta_file or (args.batch_file.rsplit(".", 1)[0] + "_meta.json")
    meta = {
        f"prtimes-{i:06d}": {
            "article_url": row["article_url"], "title": row["title"],
            "publish_date": row["publish_date"], "keyword": row["keyword"],
        }
        for i, row in enumerate(rows)
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"メタ情報(custom_id対応表)を {meta_path} に保存しました")

    if args.submit:
        client = anthropic.Anthropic()
        chunk_size = args.chunk_size
        chunks = [batch_requests[i:i + chunk_size] for i in range(0, len(batch_requests), chunk_size)]
        print(f"\n{len(chunks)}個のバッチに分割して送信します（1バッチ最大{chunk_size:,}件）")

        batch_ids = []
        for ci, chunk in enumerate(chunks, 1):
            batch = client.messages.batches.create(requests=chunk)
            batch_ids.append(batch.id)
            print(f"  [{ci}/{len(chunks)}] batch_id: {batch.id} （{len(chunk):,}件）")

        ids_path = args.batch_file.rsplit(".", 1)[0] + "_batch_ids.txt"
        with open(ids_path, "w", encoding="utf-8") as f:
            f.write("\n".join(batch_ids))

        print(f"\n全 {len(batch_ids)} 個のbatch_idを {ids_path} に保存しました。")
        print("collect_prtimes_keyman_batch.py --batch-id-file " + ids_path + " で結果を回収してください。")
    else:
        print("\n--submit を付けずに実行したため、実際の送信はしていません。")
        print("内容を確認後、--submit を付けて再実行するとAnthropic APIに送信されます。")


if __name__ == "__main__":
    main()
