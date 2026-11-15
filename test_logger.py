"""Tests for logger.py using fake ESPN data. Run: python3 -m unittest test_logger"""

import copy
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone

import logger

KEYS = ["minutes", "points", "fieldGoalsMade-fieldGoalsAttempted",
        "threePointFieldGoalsMade-threePointFieldGoalsAttempted", "freeThrowsMade-freeThrowsAttempted",
        "rebounds", "assists", "turnovers", "steals", "blocks", "offensiveRebounds", "defensiveRebounds",
        "fouls", "plusMinus"]


def athlete(pid, name, pts, reb, ast, fga=14):
    return {"athlete": {"id": pid, "displayName": name},
            "stats": ["30", str(pts), "9-%d" % fga, "1-1", "7-7", str(reb), str(ast), "4", "3", "0", "1",
                      str(reb - 1), "1", "+5"]}


def make_summary(jokic_ast=10, gordon_ast=1, jokic_fga=14):
    return {
        "header": {"id": "401", "competitions": [{
            "date": "2026-11-15T00:00Z",
            "status": {"type": {"completed": True}},
            "competitors": [
                {"homeAway": "home", "score": "118", "team": {"abbreviation": "LAL", "name": "Lakers"}},
                {"homeAway": "away", "score": "112", "team": {"abbreviation": "DEN", "name": "Nuggets"}},
            ],
        }]},
        "boxscore": {"players": [
            {"team": {"abbreviation": "DEN"}, "statistics": [{"keys": KEYS, "athletes": [
                athlete(1, "Nikola Jokic", 28, 14, jokic_ast, jokic_fga),
                athlete(2, "Aaron Gordon", 11, 5, gordon_ast),
                {"athlete": {"id": 3, "displayName": "Bench Guy"}, "stats": []},
            ]}]},
            {"team": {"abbreviation": "LAL"}, "statistics": [{"keys": KEYS, "athletes": [
                athlete(4, "LeBron James", 25, 8, 9),
            ]}]},
        ]},
    }


def scoreboard():
    return {"events": [{
        "id": "401", "date": "2026-11-15T00:00Z",
        "status": {"type": {"completed": True}},
        "competitions": [{"competitors": [
            {"homeAway": "home", "score": "118", "team": {"abbreviation": "LAL", "name": "Lakers"}},
            {"homeAway": "away", "score": "112", "team": {"abbreviation": "DEN", "name": "Nuggets"}},
        ]}],
    }]}


EXPECTED_TWEET = ("🔴 NBA Stat Correction | 11/14\n\n"
                  "Nikola Jokic (DEN): -1 AST\n"
                  "DEN 112 @ LAL 118 | Final\n\n"
                  "Now: 9 AST\n"
                  "Was: 10 AST\n\n"
                  "#NBA #NBAProps")


class FakeNotifier:
    owner = "someone"

    def __init__(self):
        self.sent, self.fail = [], False

    def send(self, title, body, key):
        if self.fail:
            raise urllib.error.URLError("down")
        if key not in [k for _, _, k in self.sent]:      # same behaviour as the real duplicate check
            self.sent.append((title, body, key))


class ParseAndFormatTests(unittest.TestCase):
    def test_parse_summary(self):
        game = logger.parse_summary(make_summary())
        jokic = game["players"]["1"]
        self.assertEqual(jokic["stats"]["assists"], 10)
        self.assertEqual(jokic["stats"]["fieldGoalsMade"], 9)
        self.assertEqual(jokic["stats"]["fieldGoalsAttempted"], 14)
        self.assertNotIn("plusMinus", jokic["stats"])
        self.assertEqual(game["players"]["3"]["stats"], {})
        self.assertEqual(game["away"]["abbr"], "DEN")

    def test_removal_tweet(self):
        old = logger.parse_summary(make_summary())
        new = logger.parse_summary(make_summary(jokic_ast=9))
        changes = logger.diff_players(old["players"], new["players"])
        self.assertEqual(len(changes), 1)
        self.assertEqual(logger.format_tweet(changes[0], new), EXPECTED_TWEET)

    def test_addition_tweet_starts_green(self):
        old = logger.parse_summary(make_summary())
        new = logger.parse_summary(make_summary(gordon_ast=2))
        change = logger.diff_players(old["players"], new["players"])[0]
        self.assertTrue(logger.format_tweet(change, new).startswith("🟢 NBA Stat Correction | 11/14"))
        self.assertIn("Aaron Gordon (DEN): +1 AST", logger.format_tweet(change, new))

    def test_moved_stat_is_noted(self):
        old = logger.parse_summary(make_summary())
        new = logger.parse_summary(make_summary(jokic_ast=9, gordon_ast=2))
        notes = {c["player_name"]: c["note"] for c in logger.diff_players(old["players"], new["players"])}
        self.assertEqual(notes["Nikola Jokic"], "moved to Aaron Gordon")
        self.assertEqual(notes["Aaron Gordon"], "moved from Nikola Jokic")

    def test_no_change_no_corrections(self):
        game = logger.parse_summary(make_summary())
        self.assertEqual(logger.diff_players(game["players"], copy.deepcopy(game["players"])), [])

    def test_espn_abbreviations_converted(self):
        self.assertEqual(logger.nba_abbr("NY"), "NYK")
        self.assertEqual(logger.nba_abbr("DEN"), "DEN")

    def test_post_link_carries_exact_tweet(self):
        link = logger.post_link(EXPECTED_TWEET)
        self.assertTrue(link.startswith("https://x.com/intent/post?text="))
        self.assertNotIn(" ", link)
        self.assertNotIn("\n", link)
        self.assertEqual(urllib.parse.unquote(link.split("text=", 1)[1]), EXPECTED_TWEET)


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.clock = datetime(2026, 11, 15, 3, 0, tzinfo=timezone.utc)   # game tipped at 00:00
        self.summary = make_summary()
        self.fetched = []

        def fetch(url):
            self.fetched.append(url)
            return scoreboard() if "scoreboard" in url else copy.deepcopy(self.summary)

        self.notifier = FakeNotifier()
        self.tracker = logger.Tracker(self.dir, fetch=fetch, now=lambda: self.clock, notifier=self.notifier)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def run_after(self, minutes=15, **summary_args):
        if summary_args:
            self.summary = make_summary(**summary_args)
        self.clock += timedelta(minutes=minutes)
        self.fetched.clear()
        return self.tracker.run_once()

    def records(self):
        path = self.tracker.corrections_path
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return [json.loads(line) for line in fh]

    def game_file(self):
        with open(os.path.join(self.tracker.games_dir, "401.json")) as fh:
            return fh.read()

    def test_correction_is_held_then_confirmed_and_sent_once(self):
        self.assertEqual(self.tracker.run_once(), [])            # saves the starting box score
        self.assertEqual(self.run_after(), [])                   # nothing changed
        self.assertEqual(self.run_after(jokic_ast=9), [])        # change seen, held for one check
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self.records(), [])
        confirmed = self.run_after()                             # still 9: confirmed
        self.assertEqual([(c["player_name"], c["stat"], c["delta"]) for c in confirmed],
                         [("Nikola Jokic", "assists", -1)])
        self.assertEqual(len(self.notifier.sent), 1)
        title, body, _ = self.notifier.sent[0]
        self.assertEqual(title, "🔴 Nikola Jokic -1 AST (DEN @ LAL, 11/14)")
        self.assertIn(EXPECTED_TWEET, body)
        self.assertIn(logger.post_link(EXPECTED_TWEET), body)
        self.assertIn("cc @someone", body)
        record = self.records()[0]
        self.assertEqual((record["status"], record["stat"], record["old"], record["new"]), ("confirmed", "AST", 10, 9))
        with open(self.tracker.corrections_log) as fh:
            self.assertIn("Now: 9 AST", fh.read())
        self.assertEqual(self.run_after(), [])                   # not reported twice
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(len(self.records()), 1)

    def test_flipped_back_stat_is_cancelled_not_sent(self):
        self.tracker.run_once()
        self.run_after(jokic_ast=9)
        self.assertEqual(self.run_after(jokic_ast=10), [])       # glitch: back to 10
        self.assertEqual(self.run_after(), [])
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual([r["status"] for r in self.records()], ["cancelled"])
        self.assertIn('"pending": []', self.game_file())

    def test_stat_that_changes_again_is_tracked_from_original_value(self):
        self.tracker.run_once()
        self.run_after(jokic_ast=9)
        self.run_after(jokic_ast=8)                              # 10 -> 9 -> 8
        confirmed = self.run_after()
        self.assertEqual([(c["old"], c["new"], c["delta"]) for c in confirmed], [(10, 8, -2)])
        self.assertIn("-2 AST", self.notifier.sent[0][0])

    def test_minor_stat_is_logged_without_notification(self):
        self.tracker.run_once()
        self.run_after(jokic_fga=13)
        self.assertEqual(len(self.run_after()), 1)
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self.records()[0]["headline"], False)

    def test_failed_notification_is_retried_without_duplicates(self):
        self.tracker.run_once()
        self.run_after(jokic_ast=9)
        self.notifier.fail = True
        self.run_after()                                         # confirmed, but GitHub is down
        self.assertEqual(self.notifier.sent, [])
        self.notifier.fail = False
        self.run_after()
        self.run_after()
        self.assertEqual(len(self.notifier.sent), 1)

    def test_idle_run_changes_no_files(self):
        self.tracker.run_once()
        self.run_after()

        def snapshot():
            out = {}
            for root, _, files in os.walk(self.dir):
                for name in files:
                    path = os.path.join(root, name)
                    with open(path) as fh:
                        out[path] = (fh.read(), os.path.getmtime(path))
            return out

        before = snapshot()
        self.run_after()
        self.assertEqual(snapshot(), before)

    def test_age_rules(self):
        self.tracker.run_once()
        summary_url = logger.SUMMARY_URL.format(game_id="401")
        self.run_after(minutes=60)                               # under 24 hours old: every run
        self.assertIn(summary_url, self.fetched)
        self.clock = datetime(2026, 11, 17, 4, 0, tzinfo=timezone.utc)   # 2 days old, not a slow-check hour
        self.run_after(minutes=7)
        self.assertNotIn(summary_url, self.fetched)
        self.clock = datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc)  # 2 days old, slow-check hour
        self.run_after(minutes=7)
        self.assertIn(summary_url, self.fetched)

    def test_dropped_after_seven_days(self):
        self.tracker.run_once()
        self.assertTrue(os.path.exists(os.path.join(self.tracker.games_dir, "401.json")))
        self.run_after(minutes=60 * 24 * 7 + 60)
        self.assertEqual(os.listdir(self.tracker.games_dir), [])

    def test_final_time_is_when_first_seen_for_a_live_game(self):
        self.tracker.run_once()                                  # 3 hours after tip: just ended
        self.assertIn('"final_seen_utc": "2026-11-15T03:00:00+00:00"', self.game_file())



class FakeX:
    def __init__(self):
        self.queries = []
        self.posts = [
            {"id": "111", "text": "Jokic had 10 assists, how is this a loss @someone", "likes": 2, "replies": 0, "reposts": 0},
            {"id": "222", "text": "they took an assist off Jokic after the game", "likes": 40, "replies": 9, "reposts": 3},
        ]

    def search(self, query, start_time=None):
        self.queries.append((query, start_time))
        return [dict(p) for p in self.posts]


class ReplyFinderTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.clock = datetime(2026, 11, 15, 3, 0, tzinfo=timezone.utc)
        self.summary = make_summary()

        def fetch(url):
            return scoreboard() if "scoreboard" in url else copy.deepcopy(self.summary)

        self.notifier, self.x = FakeNotifier(), FakeX()
        self.tracker = logger.Tracker(self.dir, fetch=fetch, now=lambda: self.clock, notifier=self.notifier,
                                      x_search=self.x)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def run_after(self, minutes=15, **summary_args):
        if summary_args:
            self.summary = make_summary(**summary_args)
        self.clock += timedelta(minutes=minutes)
        self.tracker.counts["x_searches"] = 0          # a fresh run on GitHub starts from zero
        return self.tracker.run_once()

    def confirm_correction(self):
        self.tracker.run_once()
        self.run_after(jokic_ast=9)
        self.run_after()

    def state(self):
        return self.tracker.load_state()

    def test_correction_issue_gets_posts_and_drafted_replies(self):
        self.confirm_correction()
        self.assertEqual(len(self.x.queries), 1)
        query, start = self.x.queries[0]
        self.assertEqual(query, "Jokic (assist OR assists) -is:retweet lang:en")
        body = self.notifier.sent[0][1]
        self.assertLess(body.index("took an assist off"), body.index("how is this a loss"))   # busiest post first
        draft = logger.correction_reply({"player_name": "Nikola Jokic", "new": 9, "old": 10, "stat": "assists",
                                         "delta": -1}, logger.parse_summary(self.summary), "222")
        self.assertIn("9 AST", draft)
        self.assertIn(logger.reply_link("222", draft), body)
        self.assertIn("in_reply_to=222", body)
        self.assertIn("Search X for posts about this", body)
        self.assertEqual(self.state()["x_reads"]["day_count"], 2)
        self.assertEqual(self.state()["x_reads"]["total"], 2)

    def test_no_token_means_no_search_but_still_a_free_search_link(self):
        self.tracker.x_search = None
        self.confirm_correction()
        body = self.notifier.sent[0][1]
        self.assertNotIn("Reply on X", body)
        self.assertIn("https://x.com/search?q=Jokic%20assist&f=live", body)
        self.assertNotIn("x_reads", self.state())

    def test_daily_read_limit_stops_searches(self):
        self.tracker.run_once()
        state = self.state()
        state["x_reads"] = {"day": "2026-11-14", "day_count": logger.MAX_X_READS_PER_DAY - 5,
                            "month": "2026-11", "month_count": 100, "total": 100}
        self.tracker.save_state(state)
        self.run_after(jokic_ast=9)
        self.run_after()
        self.assertEqual(self.x.queries, [])
        self.assertEqual(len(self.notifier.sent), 1)          # the correction still goes out

    def test_monthly_read_limit_stops_searches(self):
        self.tracker.run_once()
        state = self.state()
        state["x_reads"] = {"day": "2026-11-01", "day_count": 0, "month": "2026-11",
                            "month_count": logger.MAX_X_READS_PER_MONTH - 5, "total": 895}
        self.tracker.save_state(state)
        self.run_after(jokic_ast=9)
        self.run_after()
        self.assertEqual(self.x.queries, [])

    def test_daily_sweep_runs_once_and_drafts_from_box_score(self):
        self.tracker.run_once()
        self.x.posts = [{"id": "333", "text": "Need a stat correction on that LeBron rebound", "likes": 5,
                         "replies": 1, "reposts": 0}]
        self.clock = datetime(2026, 11, 15, 15, 0, tzinfo=timezone.utc)
        self.run_after(minutes=7)
        self.run_after(minutes=15)                            # same hour, same day: no second search
        self.assertEqual([q for q, _ in self.x.queries], [logger.SWEEP_QUERY])
        self.assertEqual(len(self.notifier.sent), 1)
        title, body, key = self.notifier.sent[0]
        self.assertEqual(title, "Posts to reply to | 11/15")
        self.assertEqual(key, "sweep-2026-11-15")
        self.assertIn("Current box score: LeBron James has 25 PTS, 8 REB, 9 AST (DEN @ LAL, 11/14).", body)

    def test_sweep_post_without_a_known_player_gets_no_draft(self):
        self.tracker.run_once()
        self.x.posts = [{"id": "444", "text": "stat corrections are a joke", "likes": 1, "replies": 0, "reposts": 0}]
        self.clock = datetime(2026, 11, 15, 15, 0, tzinfo=timezone.utc)
        self.run_after(minutes=7)
        self.assertIn("no draft for this one", self.notifier.sent[0][1])

    def test_post_text_cannot_ping_github_users(self):
        section = "\n".join(logger.reply_section(self.x.posts, lambda post: None))
        before, _, rest = section.partition("@someone")
        self.assertEqual(before.count("```") % 2, 1)          # the @mention sits inside a plain-text block

    def test_player_match_ignores_capitals_and_short_first_names(self):
        game = dict(logger.parse_summary(make_summary()), start_utc="2026-11-15T00:00:00+00:00")
        match = logger.match_player("we need a jokic stat correction someone find a rebound", [game])
        self.assertEqual(match[0]["name"], "Nikola Jokic")
        self.assertIsNone(logger.match_player("aaron will get that rebound back", [game]))

    def test_own_posts_are_skipped_only_when_a_handle_is_set(self):
        self.assertEqual(logger.skip_own_posts("q"), "q")
        logger.OWN_HANDLE = "ExampleHandle"
        try:
            self.assertEqual(logger.skip_own_posts("q"), "q -from:ExampleHandle")
        finally:
            logger.OWN_HANDLE = ""

    def test_last_name_ignores_suffixes(self):
        self.assertEqual(logger.last_name("Jaren Jackson Jr."), "Jackson")
        self.assertEqual(logger.last_name("Nikola Jokic"), "Jokic")


if __name__ == "__main__":
    unittest.main()
