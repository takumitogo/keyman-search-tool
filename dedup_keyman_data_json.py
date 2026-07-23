"""
dedup_keyman_data_json.py
============================================================
keyman_data.json（Webツール用データ）から、会社名・氏名・部署・役職・製品・投稿日が
全て一致する重複レコードを除去する。

同じサイト内で同じ人物のテストモニアルが複数の異なるURLに埋め込まれている場合等、
article_urlレベルでは別物に見えても内容が完全に同じレコードを1件にまとめる。

使い方:
    python dedup_keyman_data_json.py --input keyman_data.json --output keyman_data.json
============================================================
"""

import argparse
import json


def main():
    parser = argparse.ArgumentParser(description="keyman_data.jsonの重複レコードを除去する")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)

    print(f"元の件数: {len(data):,}")

    seen = set()
    deduped = []
    for d in data:
        sig = (
            d.get("company", ""), d.get("name", ""), d.get("dept", ""),
            d.get("title", ""), d.get("product", ""), d.get("date", ""),
        )
        if sig in seen:
            continue
        seen.add(sig)
        deduped.append(d)

    print(f"除去後: {len(deduped):,}")
    print(f"除去件数: {len(data) - len(deduped):,}")

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(deduped, f, ensure_ascii=False, separators=(",", ":"))

    print(f"完了。{args.output} に書き出しました。")


if __name__ == "__main__":
    main()
