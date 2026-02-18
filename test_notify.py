"""notify.py のユニットテスト"""

import json
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

class _FakeSlackApiError(Exception):
    def __init__(self, message="", response=None):
        super().__init__(message)
        self.response = response or {}

_mock_slack_errors = MagicMock()
_mock_slack_errors.SlackApiError = _FakeSlackApiError

sys.modules["instaloader"] = MagicMock()
sys.modules["slack_sdk"] = MagicMock()
sys.modules["slack_sdk.errors"] = _mock_slack_errors
sys.modules["dotenv"] = MagicMock()

import notify  # noqa: E402

PROFILE_ANTENNA = notify.ProfileConfig(
    username="antenna_america_tokyo",
    display_name="antenna america tokyo",
    brewery_name=None,
)
PROFILE_INKHORN = notify.ProfileConfig(
    username="inkhorn_brewing",
    display_name="Inkhorn Brewing",
    brewery_name="Inkhorn Brewing",
)

ANTENNA_CAPTION = """\
【新作商品のご案内】
こんにちは！アンテナアメリカ東京店です🇺🇸

Sierra Nevada @sierranevada より新作ビールが到着です🍻

Sierra Nevada Pils (Pilsner)
伝統的なピルスナーにアメリカ特有のフレーバーを加えた革新的でプレミアムな一杯

Sierra Nevada Springfest IPA
オレンジの花やトロピカルフルーツを思わせる香りと、グレープフルーツのすっきりとした風味。

本日もアンテナアメリカ東京店にてお待ちしております❗️"""

INKHORN_CAPTION = """\
【BEER】

Name: Mejiro 2026
Style: Hazy IPA
Abv. : 6.5%

ホップ由来のグレープフルーツ、マンゴー、ピーチなどのフルーツ感を楽しめる濁りが強めのIPA。
≡≡≡≡≡≡≡≡≡≡≡≡≡≡≡≡≡≡≡≡≡≡≡≡≡≡≡
Malt: Extra Pils, Oats
Hops: Citra, Idaho 7, Nelson Sauvin
≡≡≡≡≡≡≡≡≡≡≡≡≡≡≡≡≡≡≡≡≡≡≡≡≡≡≡"""


# ── seen_key ──────────────────────────────────────────────────────────────────

class TestSeenKey:
    def test_combines_username_and_shortcode(self):
        assert notify.seen_key("antenna_america_tokyo", "ABC123") == "antenna_america_tokyo:ABC123"

    def test_inkhorn_key(self):
        assert notify.seen_key("inkhorn_brewing", "XYZ") == "inkhorn_brewing:XYZ"


# ── load_seen_posts ───────────────────────────────────────────────────────────

class TestLoadSeenPosts:
    def test_returns_empty_set_when_file_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(notify, "SEEN_POSTS_FILE", tmp_path / "seen_posts.json")
        assert notify.load_seen_posts() == set()

    def test_loads_new_format_keys(self, tmp_path, monkeypatch):
        f = tmp_path / "seen_posts.json"
        f.write_text(json.dumps(["antenna_america_tokyo:abc", "inkhorn_brewing:def"]), encoding="utf-8")
        monkeypatch.setattr(notify, "SEEN_POSTS_FILE", f)
        assert notify.load_seen_posts() == {"antenna_america_tokyo:abc", "inkhorn_brewing:def"}

    def test_migrates_old_format_to_antenna_prefix(self, tmp_path, monkeypatch):
        f = tmp_path / "seen_posts.json"
        f.write_text(json.dumps(["oldshortcode"]), encoding="utf-8")
        monkeypatch.setattr(notify, "SEEN_POSTS_FILE", f)
        assert notify.load_seen_posts() == {"antenna_america_tokyo:oldshortcode"}

    def test_returns_empty_set_on_invalid_json(self, tmp_path, monkeypatch):
        f = tmp_path / "seen_posts.json"
        f.write_text("NOT_JSON", encoding="utf-8")
        monkeypatch.setattr(notify, "SEEN_POSTS_FILE", f)
        assert notify.load_seen_posts() == set()

    def test_returns_empty_set_when_data_is_not_list(self, tmp_path, monkeypatch):
        f = tmp_path / "seen_posts.json"
        f.write_text(json.dumps({"key": "value"}), encoding="utf-8")
        monkeypatch.setattr(notify, "SEEN_POSTS_FILE", f)
        assert notify.load_seen_posts() == set()


# ── save_seen_posts ───────────────────────────────────────────────────────────

class TestSaveSeenPosts:
    def test_saves_sorted_list(self, tmp_path, monkeypatch):
        f = tmp_path / "seen_posts.json"
        monkeypatch.setattr(notify, "SEEN_POSTS_FILE", f)
        notify.save_seen_posts({"zzz:1", "aaa:1", "mmm:1"})
        data = json.loads(f.read_text(encoding="utf-8"))
        assert data == ["aaa:1", "mmm:1", "zzz:1"]


# ── extract_beer_info (antenna 形式) ──────────────────────────────────────────

class TestExtractBeerInfoAntenna:
    def test_extracts_brewery_from_at_mention_pattern(self):
        result = notify.extract_beer_info(ANTENNA_CAPTION)
        assert result["brewery_name"] == "Sierra Nevada"

    def test_extracts_multiple_beer_names(self):
        result = notify.extract_beer_info(ANTENNA_CAPTION)
        assert "Sierra Nevada Pils (Pilsner)" in result["beer_name"]
        assert "Sierra Nevada Springfest IPA" in result["beer_name"]

    def test_beer_names_joined_with_slash(self):
        result = notify.extract_beer_info(ANTENNA_CAPTION)
        assert " / " in result["beer_name"]

    def test_japanese_lines_not_included(self):
        result = notify.extract_beer_info(ANTENNA_CAPTION)
        assert "伝統的" not in result["beer_name"]


# ── extract_beer_info (inkhorn 形式) ──────────────────────────────────────────

class TestExtractBeerInfoInkhorn:
    def test_extracts_beer_name_and_style(self):
        result = notify.extract_beer_info(INKHORN_CAPTION, brewery_hint="Inkhorn Brewing")
        assert result["beer_name"] == "Mejiro 2026 (Hazy IPA)"

    def test_brewery_hint_used(self):
        result = notify.extract_beer_info(INKHORN_CAPTION, brewery_hint="Inkhorn Brewing")
        assert result["brewery_name"] == "Inkhorn Brewing"

    def test_fallback_on_empty_caption(self):
        result = notify.extract_beer_info("", brewery_hint="Inkhorn Brewing")
        assert result == {"beer_name": "不明", "brewery_name": "Inkhorn Brewing"}

    def test_name_without_style(self):
        caption = "【BEER】\n\nName: Shinjuku Stout\n\n美味しいスタウトです。"
        result = notify.extract_beer_info(caption, brewery_hint="Inkhorn Brewing")
        assert result["beer_name"] == "Shinjuku Stout"
        assert "(" not in result["beer_name"]

    def test_fallback_when_no_pattern_matches(self):
        result = notify.extract_beer_info("今日も良い天気ですね！")
        assert result == {"beer_name": "不明", "brewery_name": "不明"}


# ── format_slack_message ──────────────────────────────────────────────────────

class TestFormatSlackMessage:
    def test_contains_display_name(self):
        dt = datetime(2026, 2, 18, 10, 30, 0, tzinfo=timezone.utc)
        msg = notify.format_slack_message("Torpedo IPA", "Sierra Nevada", dt, "ABC123", "caption", "antenna america tokyo")
        assert "antenna america tokyo" in msg

    def test_contains_beer_and_brewery(self):
        dt = datetime(2026, 2, 18, 10, 30, 0, tzinfo=timezone.utc)
        msg = notify.format_slack_message("Mejiro 2026 (Hazy IPA)", "Inkhorn Brewing", dt, "XYZ", "cap", "Inkhorn Brewing")
        assert "Mejiro 2026 (Hazy IPA)" in msg
        assert "Inkhorn Brewing" in msg
        assert "https://www.instagram.com/p/XYZ/" in msg

    def test_caption_truncated_at_100_chars(self):
        msg = notify.format_slack_message("B", "BR", datetime.now(), "SC", "A" * 150, "Test")
        excerpt_line = [l for l in msg.splitlines() if "キャプション抜粋" in l][0]
        assert excerpt_line.endswith("...")

    def test_short_caption_no_ellipsis(self):
        msg = notify.format_slack_message("B", "BR", datetime.now(), "SC", "Short", "Test")
        excerpt_line = [l for l in msg.splitlines() if "キャプション抜粋" in l][0]
        assert not excerpt_line.endswith("...")


# ── POST_SINCE フィルタ ────────────────────────────────────────────────────────

class TestPostSinceFilter:
    def test_post_since_is_feb_1_2026_utc(self):
        assert notify.POST_SINCE == datetime(2026, 2, 1, tzinfo=timezone.utc)

    def test_post_before_since_is_excluded(self):
        assert datetime(2026, 1, 31, 23, 59, 59, tzinfo=timezone.utc) < notify.POST_SINCE

    def test_post_on_since_is_included(self):
        assert not (datetime(2026, 2, 1, 0, 0, 0, tzinfo=timezone.utc) < notify.POST_SINCE)


# ── notify 関数 ───────────────────────────────────────────────────────────────

class TestNotifyFunction:
    def _make_post(self, shortcode: str, date_utc: datetime, caption: str = "test"):
        post = MagicMock()
        post.shortcode = shortcode
        post.date_utc = date_utc.replace(tzinfo=None)
        post.date_local = date_utc
        post.caption = caption
        return post

    def test_skips_already_seen_posts(self, tmp_path, monkeypatch):
        f = tmp_path / "seen_posts.json"
        f.write_text(json.dumps(["antenna_america_tokyo:existing"]), encoding="utf-8")
        monkeypatch.setattr(notify, "SEEN_POSTS_FILE", f)
        mock_send = MagicMock()
        monkeypatch.setattr(notify, "send_slack_notification", mock_send)

        post = self._make_post("existing", datetime(2026, 2, 10, tzinfo=timezone.utc))
        notify.notify(PROFILE_ANTENNA, [post], "token")
        mock_send.assert_not_called()

    def test_sends_notification_for_new_post(self, tmp_path, monkeypatch):
        f = tmp_path / "seen_posts.json"
        f.write_text(json.dumps([]), encoding="utf-8")
        monkeypatch.setattr(notify, "SEEN_POSTS_FILE", f)
        mock_send = MagicMock()
        monkeypatch.setattr(notify, "send_slack_notification", mock_send)

        post = self._make_post("newpost", datetime(2026, 2, 10, tzinfo=timezone.utc))
        notify.notify(PROFILE_ANTENNA, [post], "token")
        mock_send.assert_called_once()

    def test_inkhorn_post_uses_brewery_hint(self, tmp_path, monkeypatch):
        f = tmp_path / "seen_posts.json"
        f.write_text(json.dumps([]), encoding="utf-8")
        monkeypatch.setattr(notify, "SEEN_POSTS_FILE", f)
        sent_texts = []
        monkeypatch.setattr(notify, "send_slack_notification", lambda text, token: sent_texts.append(text))

        post = self._make_post("inkpost", datetime(2026, 2, 10, tzinfo=timezone.utc), INKHORN_CAPTION)
        notify.notify(PROFILE_INKHORN, [post], "token")
        assert len(sent_texts) == 1
        assert "Inkhorn Brewing" in sent_texts[0]
        assert "Mejiro 2026" in sent_texts[0]

    def test_does_not_add_to_seen_on_slack_failure(self, tmp_path, monkeypatch):
        f = tmp_path / "seen_posts.json"
        f.write_text(json.dumps([]), encoding="utf-8")
        monkeypatch.setattr(notify, "SEEN_POSTS_FILE", f)
        monkeypatch.setattr(
            notify, "send_slack_notification",
            MagicMock(side_effect=_FakeSlackApiError("error", {"error": "channel_not_found"})),
        )

        post = self._make_post("failpost", datetime(2026, 2, 10, tzinfo=timezone.utc))
        notify.notify(PROFILE_ANTENNA, [post], "token")

        seen = notify.load_seen_posts()
        assert "antenna_america_tokyo:failpost" not in seen

    def test_different_profiles_have_separate_seen_keys(self, tmp_path, monkeypatch):
        """antenna と inkhorn で同じ shortcode があっても独立して管理される"""
        f = tmp_path / "seen_posts.json"
        f.write_text(json.dumps(["antenna_america_tokyo:shared"]), encoding="utf-8")
        monkeypatch.setattr(notify, "SEEN_POSTS_FILE", f)
        mock_send = MagicMock()
        monkeypatch.setattr(notify, "send_slack_notification", mock_send)

        # inkhorn の同じ shortcode は未通知扱い
        post = self._make_post("shared", datetime(2026, 2, 10, tzinfo=timezone.utc))
        notify.notify(PROFILE_INKHORN, [post], "token")
        mock_send.assert_called_once()


# ── bootstrap 関数 ────────────────────────────────────────────────────────────

class TestBootstrap:
    def _make_post(self, shortcode: str):
        post = MagicMock()
        post.shortcode = shortcode
        return post

    def test_registers_posts_with_profile_prefix(self, tmp_path, monkeypatch):
        f = tmp_path / "seen_posts.json"
        monkeypatch.setattr(notify, "SEEN_POSTS_FILE", f)

        posts = [self._make_post("p1"), self._make_post("p2")]
        notify.bootstrap(PROFILE_INKHORN, posts)

        seen = notify.load_seen_posts()
        assert "inkhorn_brewing:p1" in seen
        assert "inkhorn_brewing:p2" in seen

    def test_does_not_duplicate_existing_seen(self, tmp_path, monkeypatch):
        f = tmp_path / "seen_posts.json"
        f.write_text(json.dumps(["inkhorn_brewing:p1"]), encoding="utf-8")
        monkeypatch.setattr(notify, "SEEN_POSTS_FILE", f)

        posts = [self._make_post("p1"), self._make_post("p2")]
        notify.bootstrap(PROFILE_INKHORN, posts)

        seen = notify.load_seen_posts()
        assert seen == {"inkhorn_brewing:p1", "inkhorn_brewing:p2"}
