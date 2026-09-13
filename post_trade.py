"""
Googleスプレッドシートの「トレード記録」タブから、まだ投稿していない
最新の1行を見つけて、X(旧Twitter)に投稿するスクリプト。

必要な環境変数(GitHub Secretsから渡される想定):
  GOOGLE_SERVICE_ACCOUNT_JSON : サービスアカウントのJSON文字列
  SPREADSHEET_ID              : スプレッドシートのID
  X_API_KEY                   : X APIのConsumer Key
  X_API_SECRET                : X APIのConsumer Secret
  X_ACCESS_TOKEN              : X APIのAccess Token
  X_ACCESS_TOKEN_SECRET       : X APIのAccess Token Secret
  ANTHROPIC_API_KEY           : (任意) 投稿文を自然な文章にしたい場合のみ

状態管理:
  last_posted_row.txt に「最後に投稿した行番号(トレード記録内の連番No.)」を
  保存する。スクリプト実行後、GitHub Actions側でこのファイルをコミットし直す。
"""

import json
import os
import sys
from pathlib import Path

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from requests_oauthlib import OAuth1

STATE_FILE = Path("last_posted_row.txt")
SHEET_TAB_NAME = "トレード記録"  # スプレッドシートのタブ名。違う場合はここを変更してください。

# ヘッダーの列名(スプレッドシートの実際の見出しと完全一致させる)
COL_NO = "No."
COL_DATE = "日付"
COL_PAIR = "通貨ペア"
COL_SESSION = "セッション"
COL_DIRECTION = "方向"
COL_SETUP = "セットアップ"
COL_RESULT = "結果"
COL_PLANNED_RR = "計画RR"
COL_ACTUAL_R = "実績R"
COL_NOTE = "メモ / 反省"


def get_sheet_values():
    creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    creds_info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(
        creds_info,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    service = build("sheets", "v4", credentials=creds)
    spreadsheet_id = os.environ["SPREADSHEET_ID"]

    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"{SHEET_TAB_NAME}!A1:AB500")
        .execute()
    )
    return result.get("values", [])


def find_header_row(rows):
    """「No.」と「結果」を含む行をヘッダー行として探す"""
    for idx, row in enumerate(rows):
        if COL_NO in row and COL_RESULT in row:
            return idx, row
    raise RuntimeError(
        f"ヘッダー行が見つかりませんでした。'{COL_NO}' と '{COL_RESULT}' を含む行が必要です。"
    )


def row_to_dict(header, row):
    d = {}
    for i, col_name in enumerate(header):
        d[col_name] = row[i] if i < len(row) else ""
    return d


def get_latest_unposted_trade(rows):
    header_idx, header = find_header_row(rows)
    data_rows = rows[header_idx + 1 :]

    last_posted_no = 0
    if STATE_FILE.exists():
        content = STATE_FILE.read_text().strip()
        if content:
            last_posted_no = int(content)

    candidate = None
    for row in data_rows:
        d = row_to_dict(header, row)
        no_str = d.get(COL_NO, "").strip()
        result = d.get(COL_RESULT, "").strip()

        if not no_str or not result:
            # 結果が未記入(=未来の空行)はスキップ
            continue

        try:
            no = int(no_str)
        except ValueError:
            continue

        if no > last_posted_no:
            candidate = (no, d)
            break  # 古い順に並んでいる前提で、最初に見つかった未投稿分だけ処理

    return candidate


def compose_text_template(trade: dict) -> str:
    """AIを使わない場合のフォールバック用テンプレート"""
    pair = trade.get(COL_PAIR, "")
    setup = trade.get(COL_SETUP, "")
    session = trade.get(COL_SESSION, "")
    result = trade.get(COL_RESULT, "")
    actual_r = trade.get(COL_ACTUAL_R, "")
    note = trade.get(COL_NOTE, "").strip()

    result_jp = {"TP": "利確", "SL": "損切り"}.get(result, result)

    lines = [
        f"{pair} {session}",
        f"セットアップ: {setup}",
        f"結果: {result_jp}({actual_r}R)",
    ]
    if note:
        lines.append(f"メモ: {note}")

    return "\n".join(lines)


def compose_text_with_ai(trade: dict) -> str:
    """ANTHROPIC_API_KEYがある場合、自然な文章に整形する"""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return compose_text_template(trade)

    prompt = (
        "以下のFXトレード記録をもとに、X(旧Twitter)用の投稿文を1つだけ作成してください。\n"
        "条件:\n"
        "- 140文字以内\n"
        "- 淡々とした検証ログのトーン。煽らない、誇張しない\n"
        "- ハッシュタグは付けない\n"
        "- 絵文字は使わない\n"
        "- 投稿文以外の説明や前置きは一切出力しない\n\n"
        f"データ: {json.dumps(trade, ensure_ascii=False)}"
    )

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-5",
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        text = "".join(
            block["text"] for block in data["content"] if block["type"] == "text"
        ).strip()
        return text if text else compose_text_template(trade)
    except Exception as e:
        print(f"AI生成に失敗したためテンプレートにフォールバック: {e}", file=sys.stderr)
        return compose_text_template(trade)


def post_to_x(text: str):
    api_key = os.environ["X_API_KEY"]
    api_secret = os.environ["X_API_SECRET"]
    access_token = os.environ["X_ACCESS_TOKEN"]
    access_token_secret = os.environ["X_ACCESS_TOKEN_SECRET"]

    auth = OAuth1(
        api_key,
        client_secret=api_secret,
        resource_owner_key=access_token,
        resource_owner_secret=access_token_secret,
    )

    response = requests.post(
        "https://api.twitter.com/2/tweets",
        auth=auth,
        json={"text": text},
        timeout=30,
    )

    if response.status_code >= 300:
        raise RuntimeError(f"投稿に失敗しました: {response.status_code} {response.text}")

    print("投稿成功:", response.json())


def main():
    rows = get_sheet_values()
    candidate = get_latest_unposted_trade(rows)

    if candidate is None:
        print("新しい未投稿トレードはありません。")
        return

    no, trade = candidate
    text = compose_text_with_ai(trade)

    print("=== 投稿予定のテキスト ===")
    print(text)
    print("==========================")

    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    if dry_run:
        print("DRY_RUN=true のため、実際の投稿はスキップしました。")
    else:
        post_to_x(text)

    STATE_FILE.write_text(str(no))
    print(f"last_posted_row.txt を {no} に更新しました。")


if __name__ == "__main__":
    main()
