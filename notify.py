#!/usr/bin/env python3
"""Instagram → Slack 通知スクリプト

複数の Instagram アカウントの新着投稿を検知し、
正規表現でビール名・醸造所名を抽出してSlackに通知する。
"""

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import instaloader
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# ── 定数 ──────────────────────────────────────────────────────────────────────
SLACK_CHANNEL_ID = "C0AFFKY9DHB"
SEEN_POSTS_FILE = Path(__file__).parent / "seen_posts.json"
FETCH_COUNT = 10
POST_SINCE = datetime(2026, 2, 1, tzinfo=timezone.utc)  # この日時以降の投稿のみ対象


@dataclass
class ProfileConfig:
    username: str              # Instagram ユーザー名
    display_name: str          # Slack 通知に表示する名称
    brewery_name: Optional[str] = None  # 固定の醸造所名（None = キャプションから抽出）


PROFILES: list[ProfileConfig] = [
    ProfileConfig(
        username="antenna_america_tokyo",
        display_name="antenna america tokyo",
        brewery_name=None,
    ),
    ProfileConfig(
        username="inkhorn_brewing",
        display_name="Inkhorn Brewing",
        brewery_name="Inkhorn Brewing",
    ),
]

# ── ログ設定 ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── seen_posts.json 管理 ──────────────────────────────────────────────────────

def load_seen_posts() -> set[str]:
    """通知済み "username:shortcode" セットを読み込む。
    旧形式（プレフィックスなし）は antenna_america_tokyo として自動移行。
    """
    try:
        data = json.loads(SEEN_POSTS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            logger.warning("seen_posts.json の形式が不正です。空セットで初期化します。")
            return set()
        result: set[str] = set()
        for entry in data:
            result.add(entry if ":" in entry else f"antenna_america_tokyo:{entry}")
        return result
    except FileNotFoundError:
        return set()
    except json.JSONDecodeError as e:
        logger.warning("seen_posts.json の解析に失敗しました (%s)。空セットで初期化します。", e)
        return set()


def save_seen_posts(seen: set[str]) -> None:
    """通知済みセットを保存する。"""
    SEEN_POSTS_FILE.write_text(
        json.dumps(sorted(seen), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def seen_key(username: str, shortcode: str) -> str:
    return f"{username}:{shortcode}"


# ── Instagram 取得 ────────────────────────────────────────────────────────────

SESSION_FILE = Path(__file__).parent / ".instagram_session"


def _build_loader(ig_username: Optional[str], ig_password: Optional[str]) -> instaloader.Instaloader:
    """認証済み instaloader インスタンスを返す。
    セッションファイルがあれば再利用し、なければログインして保存する。
    認証情報が未設定の場合は匿名アクセスを試みる。
    """
    loader = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        quiet=True,
    )
    if not ig_username or not ig_password:
        logger.warning("INSTAGRAM_USERNAME/PASSWORD が未設定です。匿名アクセスを試みます。")
        return loader

    if SESSION_FILE.exists():
        try:
            loader.load_session_from_file(ig_username, str(SESSION_FILE))
            logger.info("Instagram セッションをファイルから読み込みました。")
            return loader
        except Exception as e:
            logger.warning("セッションファイルの読み込みに失敗しました (%s)。", e)

    # CI 環境ではログインを試みない（セキュリティチャレンジでハングするため）
    if os.environ.get("CI"):
        logger.warning("CI 環境のため Instagram ログインをスキップします。匿名アクセスを試みます。")
        return loader

    try:
        loader.login(ig_username, ig_password)
        loader.save_session_to_file(str(SESSION_FILE))
        logger.info("Instagram にログインし、セッションを保存しました。")
    except instaloader.exceptions.BadCredentialsException:
        logger.error("Instagram のログインに失敗しました。認証情報を確認してください。")
        sys.exit(1)
    except instaloader.exceptions.TwoFactorAuthRequiredException:
        logger.error("Instagram の二段階認証が必要です。事前に instaloader でセッションを作成してください。")
        sys.exit(1)
    except instaloader.exceptions.ConnectionException as e:
        logger.error("Instagram ログイン中に接続エラーが発生しました: %s", e)
        sys.exit(1)
    return loader


def fetch_recent_posts(
    username: str,
    count: int = FETCH_COUNT,
    ig_username: Optional[str] = None,
    ig_password: Optional[str] = None,
) -> list[instaloader.Post]:
    """instaloader で公開プロフィールの最新投稿を取得する。"""
    loader = _build_loader(ig_username, ig_password)
    try:
        profile = instaloader.Profile.from_username(loader.context, username)
    except instaloader.exceptions.ProfileNotExistsException:
        logger.error("プロフィール '%s' が見つかりません。", username)
        sys.exit(1)
    except instaloader.exceptions.PrivateProfileNotFollowedException:
        logger.error("プロフィール '%s' は非公開です。", username)
        sys.exit(1)
    except instaloader.exceptions.ConnectionException as e:
        logger.error("Instagram への接続に失敗しました: %s", e)
        return []

    posts: list[instaloader.Post] = []
    try:
        for post in profile.get_posts():
            # POST_SINCE より古い投稿に達したら打ち切り（タイムライン順 = 新しい順）
            if post.date_utc.replace(tzinfo=timezone.utc) < POST_SINCE:
                break
            posts.append(post)
            if len(posts) >= count:
                break
    except instaloader.exceptions.TooManyRequestsException:
        logger.error("Instagram のレートリミットに達しました。次回の実行でリトライします。")

    return posts


# ── キャプション解析 ──────────────────────────────────────────────────────────

# inkhorn 形式: "Name: ..." / "Style: ..."
_NAME_RE = re.compile(r'^Name:\s*(.+)$', re.MULTILINE)
_STYLE_RE = re.compile(r'^Style:\s*(.+)$', re.MULTILINE)
# antenna 形式: 醸造所名を "@mention より" または "英語名 より新作/の" から抽出
_BREWERY_RE = re.compile(r'([A-Za-z][A-Za-z0-9\s&\-]+?)\s+(?:@\w+\s+)?より(?:新作|の|$)')
# ビール名候補行: 英字で始まり日本語を含まない独立した行
_BEER_LINE_RE = re.compile(r'^[A-Z][A-Za-z0-9\s\-\'&().]+$')
_JAPANESE_RE = re.compile(r'[ぁ-んァ-ン一-龥]')


def extract_beer_info(caption: str, brewery_hint: Optional[str] = None) -> dict[str, str]:
    """キャプションからビール名・醸造所名を抽出する。

    - inkhorn 形式（Name:/Style: 行あり）: 構造化フィールドを使用
    - antenna 形式（非構造化）: 正規表現で醸造所名と英語独立行を抽出
    """
    if not caption:
        return {"beer_name": "不明", "brewery_name": brewery_hint or "不明"}

    # inkhorn 形式: "Name: ..." が存在する場合
    name_m = _NAME_RE.search(caption)
    if name_m:
        beer_name = name_m.group(1).strip()
        style_m = _STYLE_RE.search(caption)
        if style_m:
            beer_name = f"{beer_name} ({style_m.group(1).strip()})"
        return {"beer_name": beer_name, "brewery_name": brewery_hint or "不明"}

    # antenna 形式
    brewery = brewery_hint or "不明"
    if not brewery_hint:
        bm = _BREWERY_RE.search(caption)
        if bm:
            brewery = bm.group(1).strip()

    beer_lines = [
        line.strip()
        for line in caption.splitlines()
        if (
            _BEER_LINE_RE.match(line.strip())
            and not _JAPANESE_RE.search(line)
            and 5 < len(line.strip()) < 80
        )
    ]
    beer_name = " / ".join(beer_lines) if beer_lines else "不明"
    return {"beer_name": beer_name, "brewery_name": brewery}


# ── Slack 通知 ────────────────────────────────────────────────────────────────

def format_slack_message(
    beer_name: str,
    brewery_name: str,
    post_date: datetime,
    shortcode: str,
    caption: str,
    display_name: str,
) -> str:
    """Slack 通知メッセージを組み立てる。"""
    url = f"https://www.instagram.com/p/{shortcode}/"
    caption_excerpt = caption[:100].replace("\n", " ") + ("..." if len(caption) > 100 else "")
    date_str = post_date.strftime("%Y-%m-%dT%H:%M:%S")

    return (
        f":beer: *新しいInstagram投稿 - {display_name}*\n"
        f">*ビール名*: {beer_name}\n"
        f">*醸造所*: {brewery_name}\n"
        f">*投稿日時*: {date_str}\n"
        f">*投稿URL*: {url}\n"
        f">*キャプション抜粋*: {caption_excerpt}"
    )


def send_slack_notification(text: str, slack_token: str) -> None:
    """Slack チャンネルにメッセージを送信する。失敗時は例外を再送出する。"""
    client = WebClient(token=slack_token)
    try:
        client.chat_postMessage(channel=SLACK_CHANNEL_ID, text=text)
        logger.info("Slack 送信完了 (channel=%s)", SLACK_CHANNEL_ID)
    except SlackApiError as e:
        logger.error("Slack 送信エラー: %s", e.response["error"])
        raise


# ── メイン処理 ────────────────────────────────────────────────────────────────

def bootstrap(profile: ProfileConfig, posts: list[instaloader.Post]) -> None:
    """現在の投稿をすべて seen に登録し、初回通知を抑制する。"""
    seen = load_seen_posts()
    added = 0
    for post in posts:
        key = seen_key(profile.username, post.shortcode)
        if key not in seen:
            seen.add(key)
            added += 1
    save_seen_posts(seen)
    logger.info(
        "[%s] Bootstrap 完了: %d 件登録 (合計 %d 件)。",
        profile.username, added, len(seen),
    )


def notify(profile: ProfileConfig, posts: list[instaloader.Post], slack_token: str) -> None:
    """新規投稿を検出し、Slack に通知する。"""
    seen = load_seen_posts()
    new_posts = [p for p in posts if seen_key(profile.username, p.shortcode) not in seen]

    if not new_posts:
        logger.info("[%s] 新規投稿はありません。", profile.username)
        return

    logger.info("[%s] %d 件の新規投稿を検出しました。", profile.username, len(new_posts))
    new_posts.sort(key=lambda p: p.date_utc)

    for post in new_posts:
        caption = post.caption or ""
        logger.info("[%s] 処理中: shortcode=%s", profile.username, post.shortcode)

        beer_info = extract_beer_info(caption, brewery_hint=profile.brewery_name)

        text = format_slack_message(
            beer_name=beer_info["beer_name"],
            brewery_name=beer_info["brewery_name"],
            post_date=post.date_local,
            shortcode=post.shortcode,
            caption=caption,
            display_name=profile.display_name,
        )

        try:
            send_slack_notification(text, slack_token)
            seen.add(seen_key(profile.username, post.shortcode))
            save_seen_posts(seen)
        except SlackApiError:
            logger.error(
                "[%s] shortcode=%s の Slack 送信に失敗したため、seen に追加しません。",
                profile.username, post.shortcode,
            )
            continue


def main() -> None:
    parser = argparse.ArgumentParser(description="Instagram → Slack 通知スクリプト")
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="現在の投稿を seen に登録して通知を抑制する（初回セットアップ用）",
    )
    args = parser.parse_args()

    load_dotenv()
    slack_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not slack_token:
        logger.error("SLACK_BOT_TOKEN が設定されていません。")
        sys.exit(1)

    ig_username = os.environ.get("INSTAGRAM_USERNAME", "")
    ig_password = os.environ.get("INSTAGRAM_PASSWORD", "")

    for profile in PROFILES:
        logger.info("[%s] 最新 %d 件を取得します。", profile.username, FETCH_COUNT)
        posts = fetch_recent_posts(profile.username, FETCH_COUNT, ig_username, ig_password)
        logger.info("[%s] %d 件の投稿を取得しました。", profile.username, len(posts))

        if args.bootstrap:
            bootstrap(profile, posts)
        else:
            notify(profile, posts, slack_token)


if __name__ == "__main__":
    main()
