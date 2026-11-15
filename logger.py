#!/usr/bin/env python3
"""NBA postgame stat correction tracker.

Watches NBA games after they go final and records any change to a player's
box score stats (a postgame stat correction). Built to run on GitHub Actions:
each run checks the games that are due, saves what it knows as text files in
this repository, and opens a GitHub issue for each confirmed correction with
a ready-to-post tweet. Nothing is posted to X automatically.

Data comes from ESPN's public NBA feed (NBA.com's own feeds block scripts).

Usage:
    python3 logger.py once          # one pass (GitHub runs this on a schedule)
    python3 logger.py report        # summarize what has been logged so far
    python3 logger.py test-notify   # send one sample notification (on GitHub)
"""

import argparse
import gzip
import hashlib
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={date}"
SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={game_id}"
USER_AGENT = "stat-correction-logger/0.2"
ET = ZoneInfo("America/New_York")

# Which games each run checks, based on how long ago the game ended.
FAST_WINDOW = timedelta(hours=24)       # ended in the last 24 hours: checked every run
SLOW_HOURS_UTC = {11, 15, 19, 23}       # older games: once during each of these hours
WATCH_WINDOW = timedelta(days=7)        # stop watching 7 days after the game ends
DISCOVER_DAYS_BACK = 2                  # scoreboard days to scan for newly finished games
EST_GAME_LENGTH = timedelta(hours=2, minutes=30)
LIVE_DISCOVERY_WINDOW = timedelta(hours=5)
MAX_PARALLEL_FETCHES = 8
MAX_NOTIFICATIONS_PER_DAY = 30
ISSUE_LABEL = "stat-correction"
PLAY_OPTIONS_MAX = 6                    # list this many possible plays in a notification, at most
TWEET_LIMIT = 270                       # X allows 280; emoji count double, so leave room

# Finding posts to reply to (only runs when an X API token is set). X bills per post returned.
X_SEARCH_URL = "https://api.x.com/2/tweets/search/recent"
X_READ_PRICE = 0.005                    # dollars per post returned, X's listed rate
X_SEARCH_SIZE = 10                      # the smallest search X allows
MAX_X_READS_PER_DAY = 40                # hard stop: about $0.20 a day
MAX_X_READS_PER_MONTH = 900             # hard stop: about $4.50 a month
MAX_X_SEARCHES_PER_RUN = 2
REPLY_IDEAS_PER_SEARCH = 3
# The account's own X handle, so searches skip its own posts. Kept in a secret, not in the code.
OWN_HANDLE = re.sub(r"[^A-Za-z0-9_]", "", os.environ.get("X_HANDLE", ""))
SWEEP_HOUR_UTC = 15                     # one general search a day, around 10am US Eastern
SWEEP_QUERY = ('("stat correction" OR "stat corrections" OR "stat change") '
               '(NBA OR rebound OR assist OR points OR prop) -is:retweet lang:en')
STAT_WORDS = {
    "points": "points OR pts",
    "rebounds": "rebound OR rebounds OR boards",
    "assists": "assist OR assists",
    "steals": "steal OR steals",
    "blocks": "block OR blocks",
    "threePointFieldGoalsMade": "three OR threes OR 3PM",
    "turnovers": "turnover OR turnovers",
}
NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
# Surnames that are also everyday words, so a match in a post proves nothing.
COMMON_WORD_NAMES = {"green", "white", "brown", "black", "young", "love", "smart", "ball", "wall", "house",
                     "king", "bridges", "rivers", "strong", "little", "long", "small", "best", "moody"}

# ESPN stat key -> label used in tweets. Order here is the order changes are listed.
LABELS = {
    "points": "PTS",
    "rebounds": "REB",
    "assists": "AST",
    "steals": "STL",
    "blocks": "BLK",
    "threePointFieldGoalsMade": "3PM",
    "turnovers": "TOV",
    "fieldGoalsMade": "FGM",
    "fieldGoalsAttempted": "FGA",
    "threePointFieldGoalsAttempted": "3PA",
    "freeThrowsMade": "FTM",
    "freeThrowsAttempted": "FTA",
    "offensiveRebounds": "OREB",
    "defensiveRebounds": "DREB",
    "fouls": "PF",
}
# Stats that get a draft tweet. The rest are logged but marked as minor.
HEADLINE_STATS = {"points", "rebounds", "assists", "steals", "blocks", "threePointFieldGoalsMade", "turnovers"}

# ESPN abbreviations that differ from the standard NBA ones.
ESPN_TO_NBA_ABBR = {"NY": "NYK", "GS": "GSW", "SA": "SAS", "UTAH": "UTA", "WSH": "WAS", "NO": "NOP"}

log = logging.getLogger("stat-tracker")


def nba_abbr(espn_abbr):
    return ESPN_TO_NBA_ABBR.get(espn_abbr, espn_abbr)


# ---------------------------------------------------------------------------
# Fetching and parsing
# ---------------------------------------------------------------------------

def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
    return json.loads(body)


def parse_time(value):
    """ESPN times look like 2026-11-15T00:00Z (sometimes with seconds)."""
    for fmt in ("%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
    raise ValueError("Unrecognized time: %r" % (value,))


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_summary(summary):
    """Turn an ESPN game summary into {game info, players: {id: {name, team, stats}}}."""
    comp = summary["header"]["competitions"][0]
    teams = {}
    for c in comp["competitors"]:
        team = c["team"]
        teams[c["homeAway"]] = {
            "abbr": nba_abbr(team.get("abbreviation")),
            "name": team.get("name") or team.get("displayName") or team.get("abbreviation"),
            "score": to_int(c.get("score")),
        }

    players = {}
    for team_block in summary.get("boxscore", {}).get("players", []):
        abbr = nba_abbr(team_block["team"].get("abbreviation"))
        for stat_block in team_block.get("statistics", []):
            keys = stat_block.get("keys", [])
            for athlete in stat_block.get("athletes", []):
                stats = {}
                for key, value in zip(keys, athlete.get("stats") or []):
                    # Combined columns like "fieldGoalsMade-fieldGoalsAttempted" = "4-6"
                    sub_keys = key.split("-")
                    sub_vals = str(value).split("-") if len(sub_keys) > 1 else [value]
                    if len(sub_keys) != len(sub_vals):
                        continue
                    for k, v in zip(sub_keys, sub_vals):
                        if k in LABELS and to_int(v) is not None:
                            stats[k] = to_int(v)
                info = athlete["athlete"]
                players[str(info["id"])] = {
                    "name": info.get("displayName") or info.get("shortName"),
                    "team": abbr,
                    "stats": stats,
                }

    try:
        plays = parse_plays(summary)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        log.warning("Could not read the play list (stat tracking is not affected): %r", exc)
        plays = []

    return {
        "game_id": str(summary["header"]["id"]),
        "start_utc": parse_time(comp["date"]),
        "completed": bool(comp["status"]["type"].get("completed")),
        "away": teams["away"],
        "home": teams["home"],
        "players": players,
        "plays": plays,
    }


# ---------------------------------------------------------------------------
# Finding the play behind a correction
# ---------------------------------------------------------------------------

def play_time(period, clock):
    """Q1 5:04, Q4 0:42.8, OT 1:10, 2OT 3:00."""
    quarter = "Q%d" % period if period <= 4 else ("OT" if period == 5 else "%dOT" % (period - 4))
    return "%s %s" % (quarter, clock if ":" in clock else "0:" + clock)


def parse_plays(summary):
    """The plays in ESPN's play list that credit a stat: [{when, text, credits: {(player id, stat): amount}}]."""
    plays = []
    for raw in summary.get("plays") or []:
        text = " ".join((raw.get("text") or "").split())
        lower = text.lower()
        kind = " ".join(((raw.get("type") or {}).get("text") or "").lower().split())
        ids = [str(p["athlete"]["id"]) for p in raw.get("participants") or [] if (p.get("athlete") or {}).get("id")]
        if not ids:
            continue
        credits = {}
        value = to_int(raw.get("scoreValue")) or 0
        if raw.get("scoringPlay") and value > 0:
            credits[(ids[0], "points")] = value
            if kind.startswith("free throw"):
                credits[(ids[0], "freeThrowsMade")] = 1
            else:
                credits[(ids[0], "fieldGoalsMade")] = 1
                if value == 3:
                    credits[(ids[0], "threePointFieldGoalsMade")] = 1
        if "rebound" in kind:
            credits[(ids[0], "rebounds")] = 1
            if kind.startswith("offensive"):
                credits[(ids[0], "offensiveRebounds")] = 1
            elif kind.startswith("defensive"):
                credits[(ids[0], "defensiveRebounds")] = 1
        if "turnover" in lower:
            credits[(ids[0], "turnovers")] = 1
        if len(ids) > 1:                            # the second player named gets the steal, assist or block
            if "steals)" in lower:
                credits[(ids[1], "steals")] = 1
            if "assists)" in lower:
                credits[(ids[1], "assists")] = 1
            if " blocks " in lower:
                credits[(ids[1], "blocks")] = 1
        if credits:
            period = to_int((raw.get("period") or {}).get("number")) or 0
            clock = str((raw.get("clock") or {}).get("displayValue") or "")
            plays.append({"when": play_time(period, clock), "text": text, "credits": credits})
    return plays


def removed_play_options(plays, change, changes):
    """Plays that could be the one taken away from a player.

    ESPN corrects the box score but leaves its play list as first scored, so the play
    behind a removed stat is still listed. A play qualifies only if every stat it gave
    this player was reduced in the same check (a removed offensive rebound lowers both
    REB and OREB). Returns nothing unless the play list adds up to the old box score.
    """
    pid, stat = change["player_id"], change["stat"]
    credited = [p for p in plays if (pid, stat) in p["credits"]]
    if change["delta"] >= 0 or sum(p["credits"][(pid, stat)] for p in credited) != change["old"]:
        return []
    drops = {(c["player_id"], c["stat"]): -c["delta"] for c in changes if c["delta"] < 0}
    options = [p for p in credited
               if all(drops.get(key) == amount for key, amount in p["credits"].items() if key[0] == pid)]
    # Another player's stat removed in the same check points to a shared play (a turnover and its steal).
    for key, amount in drops.items():
        if key[0] != pid:
            shared = [p for p in options if p["credits"].get(key) == amount]
            if shared:
                options = shared
    return options


def attach_plays(changes, plays):
    """Add the play behind each headline change: 'play' when exactly one fits, 'play_options' for a short list."""
    if not plays:
        return
    for change in changes:
        if change["stat"] not in HEADLINE_STATS:
            continue
        source = change
        if change["delta"] > 0:
            # A stat that moved here from one other player comes from that player's play.
            givers = [c for c in changes if c["stat"] == change["stat"] and c["delta"] == -change["delta"]
                      and c["player_id"] != change["player_id"]]
            if len(givers) != 1:
                continue
            source = givers[0]
        options = ["%s, %s" % (p["when"], p["text"]) for p in removed_play_options(plays, source, changes)]
        if len(options) == 1:
            change["play"] = options[0]
        elif 1 < len(options) <= PLAY_OPTIONS_MAX:
            change["play_options"] = options


# ---------------------------------------------------------------------------
# Diffing and formatting
# ---------------------------------------------------------------------------

def diff_players(old_players, new_players):
    """List every stat that changed between two snapshots of the same game."""
    changes = []
    for pid, new in new_players.items():
        old = old_players.get(pid, {"stats": {}})
        for stat in LABELS:
            before = old["stats"].get(stat, 0)
            after = new["stats"].get(stat, 0)
            if before != after:
                changes.append({
                    "player_id": pid,
                    "player_name": new["name"],
                    "team": new["team"],
                    "stat": stat,
                    "old": before,
                    "new": after,
                    "delta": after - before,
                    "note": "",
                })
    # A player who vanished from the feed is almost always a glitch, not a correction.
    missing = [old_players[p]["name"] for p in old_players if p not in new_players]
    if missing:
        log.warning("Players missing from latest box score (ignored): %s", ", ".join(missing))
    mark_moves(changes)
    return changes


def mark_moves(changes):
    """If one player loses a stat and a teammate gains the same amount, note the move."""
    used = set()
    for i, lost in enumerate(changes):
        if lost["delta"] >= 0 or i in used:
            continue
        for j, gained in enumerate(changes):
            if (j not in used and gained["delta"] == -lost["delta"] and gained["stat"] == lost["stat"]
                    and gained["team"] == lost["team"] and gained["player_id"] != lost["player_id"]):
                lost["note"] = "moved to %s" % gained["player_name"]
                gained["note"] = "moved from %s" % lost["player_name"]
                used.update((i, j))
                break


def as_time(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def game_date(game):
    date = as_time(game["start_utc"]).astimezone(ET)
    return "%d/%d" % (date.month, date.day)


def game_label(game):
    return "%s @ %s" % (game["away"]["abbr"], game["home"]["abbr"])


def format_tweet(change, game):
    label = LABELS[change["stat"]]
    away, home = game["away"], game["home"]
    play_line = ""
    if change.get("play"):
        room = TWEET_LIMIT - len(format_tweet(dict(change, play=""), game)) - len("Play: \n\n")
        play = change["play"] if len(change["play"]) <= room else change["play"][:room - 1].rstrip() + "…"
        play_line = "Play: %s\n\n" % play if room > 20 else ""
    return (
        "{emoji} NBA Stat Correction | {date}\n"
        "\n"
        "{player} ({team}): {delta:+d} {label}\n"
        "{away} {away_score} @ {home} {home_score} | Final\n"
        "\n"
        "Now: {new} {label}\n"
        "Was: {old} {label}\n"
        "\n"
        "{play_line}"
        "#NBA #NBAProps"
    ).format(
        play_line=play_line,
        emoji="🔴" if change["delta"] < 0 else "🟢",
        date=game_date(game),
        player=change["player_name"], team=change["team"],
        delta=change["delta"], label=label,
        away=away["abbr"], away_score=away["score"],
        home=home["abbr"], home_score=home["score"],
        new=change["new"], old=change["old"],
    )


def post_link(tweet):
    """Link that opens X with the tweet text already filled in."""
    return "https://x.com/intent/post?text=" + urllib.parse.quote(tweet, safe="")


def describe(change):
    text = "%s (%s) %s: %d -> %d" % (
        change["player_name"], change["team"], LABELS[change["stat"]], change["old"], change["new"])
    return text + (" [%s]" % change["note"] if change.get("note") else "")


def build_issue(game, corrections, mention=None, extra_lines=None):
    """Title, body, and a unique key for the GitHub issue announcing these corrections."""
    headline = [c for c in corrections if c["stat"] in HEADLINE_STATS]
    minor = [c for c in corrections if c["stat"] not in HEADLINE_STATS]
    fingerprint = json.dumps([game["game_id"], sorted(
        (c["player_id"], c["stat"], c["old"], c["new"]) for c in corrections)])
    key = hashlib.sha1(fingerprint.encode()).hexdigest()[:12]

    if len(headline) == 1:
        c = headline[0]
        title = "%s %s %+d %s (%s, %s)" % (
            "🔴" if c["delta"] < 0 else "🟢", c["player_name"], c["delta"], LABELS[c["stat"]],
            game_label(game), game_date(game))
    else:
        title = "%d stat corrections: %s, %s" % (len(headline), game_label(game), game_date(game))

    away, home = game["away"], game["home"]
    lines = [
        "**%s %s @ %s %s | Final | %s**" % (away["abbr"], away["score"], home["abbr"], home["score"], game_date(game)),
        "Found about %d minutes after the game ended." % max(c.get("minutes_after_final", 0) for c in corrections),
        "",
    ]
    for c in headline:
        tweet = format_tweet(c, game)
        lines += [
            "### %s %s (%s): %+d %s" % ("🔴" if c["delta"] < 0 else "🟢", c["player_name"], c["team"],
                                       c["delta"], LABELS[c["stat"]]),
        ]
        if c.get("note"):
            lines.append("Note: " + c["note"])
        lines += ["**[Post to X](%s)**" % post_link(tweet), "", "```text", tweet, "```", ""]
        if c.get("play_options"):
            lines += ["Most likely one of these plays. ESPN doesn't say which, so the post leaves it out:"]
            lines += ["- " + option for option in c["play_options"]] + [""]
    if minor:
        lines += ["Other changes in the same check (no tweet): " + "; ".join(describe(c) for c in minor), ""]
    lines += extra_lines or []
    if mention:
        lines.append("cc @" + mention)
    lines.append("<!-- correction-key:%s -->" % key)
    return title, "\n".join(lines), key


# ---------------------------------------------------------------------------
# Finding posts on X and drafting replies
# ---------------------------------------------------------------------------

def last_name(full_name):
    parts = [p for p in (full_name or "").split() if p.lower().strip(".") not in NAME_SUFFIXES]
    return parts[-1] if parts else ""


def reply_link(post_id, text):
    """Link that opens X with a reply to that post already filled in."""
    return "https://x.com/intent/post?in_reply_to=%s&text=%s" % (post_id, urllib.parse.quote(text, safe=""))


def search_link(query):
    """Link that opens X's own live search. Free: it's the person searching, not the bot."""
    return "https://x.com/search?q=%s&f=live" % urllib.parse.quote(query, safe="")


def correction_query(change):
    return "%s (%s) -is:retweet lang:en" % (last_name(change["player_name"]), STAT_WORDS[change["stat"]])


def skip_own_posts(query):
    return query + (" -from:" + OWN_HANDLE if OWN_HANDLE else "")


def correction_reply(change, game, post_id):
    """A short factual reply about one correction. Wording varies so replies aren't identical."""
    facts = dict(player=change["player_name"], new=change["new"], old=change["old"], label=LABELS[change["stat"]],
                 game=game_label(game), date=game_date(game), way="down" if change["delta"] < 0 else "up")
    variants = [
        "{player} is {way} to {new} {label} in the box score, from {old}. It changed after the final ({game}, {date}).",
        "Heads up, the box score changed after the game. {player} now has {new} {label}, was {old} ({game}, {date}).",
        "This one was changed postgame. {player}: {old} {label}, now {new} ({game}, {date}).",
    ]
    digits = "".join(ch for ch in str(post_id) if ch.isdigit()) or "0"
    return variants[int(digits) % len(variants)].format(**facts)


def stat_line_reply(player, game):
    stats = player["stats"]
    return "Current box score: %s has %d PTS, %d REB, %d AST (%s, %s)." % (
        player["name"], stats.get("points", 0), stats.get("rebounds", 0), stats.get("assists", 0),
        game_label(game), game_date(game))


def match_player(text, games):
    """The one tracked player a post names (by first or last name), or None if there are none or several."""
    words = set(re.findall(r"[a-z']+", text.lower()))
    found = {}
    for game in sorted(games, key=lambda g: g["start_utc"]):       # a player's latest game wins
        for pid, player in game["players"].items():
            if player["stats"].get("points") is None:
                continue
            surname, first = last_name(player["name"]).lower(), (player["name"] or "").split(" ")[0].lower()
            # First names only count when long enough to be distinctive ("lebron", not "will").
            if ((len(surname) >= 4 and surname not in COMMON_WORD_NAMES and surname in words)
                    or (len(first) >= 6 and first in words)):
                found[pid] = (player, game)
    return next(iter(found.values())) if len(found) == 1 else None


def reply_section(posts, draft_for, heading="### Posts you could reply to"):
    fence = "`" * 3
    lines = [heading, ""]
    for post in posts:
        draft = draft_for(post)
        # Shown as plain text so nothing in the post is treated as formatting or an @mention.
        lines += [fence + "text", post["text"].replace(fence, "'" * 3), fence,
                  "%d likes, %d replies | [View post](https://x.com/i/web/status/%s)" % (
                      post["likes"], post["replies"], post["id"])]
        if draft:
            lines += ["**[Reply on X](%s)**" % reply_link(post["id"], draft), "", "Draft reply:",
                      fence + "text", draft, fence, ""]
        else:
            lines += ["**[Reply on X](%s)** (no draft for this one, write your own)" % reply_link(post["id"], ""), ""]
    return lines


class XSearch:
    """Searches recent X posts through X's paid API. Each post returned is billed."""

    def __init__(self, token):
        self.token = token

    @classmethod
    def from_env(cls):
        token = os.environ.get("X_BEARER_TOKEN")
        return cls(token) if token else None

    def search(self, query, start_time=None):
        params = {"query": query, "max_results": X_SEARCH_SIZE, "sort_order": "relevancy",
                  "tweet.fields": "created_at,public_metrics"}
        if start_time:
            params["start_time"] = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        req = urllib.request.Request(X_SEARCH_URL + "?" + urllib.parse.urlencode(params),
                                     headers={"Authorization": "Bearer " + self.token, "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as exc:
            log.warning("X search failed (%s): %s", exc.code, exc.read().decode("utf-8", "replace")[:300])
            return []
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("X search failed: %s", exc)
            return []
        posts = []
        for item in data.get("data", []):
            if not str(item.get("id", "")).isdigit():           # ids go into links, so accept digits only
                continue
            metrics = item.get("public_metrics", {})
            posts.append({"id": item["id"], "text": item.get("text", ""), "likes": metrics.get("like_count", 0),
                          "replies": metrics.get("reply_count", 0), "reposts": metrics.get("retweet_count", 0)})
        return posts


# ---------------------------------------------------------------------------
# GitHub notifications
# ---------------------------------------------------------------------------

class GitHubNotifier:
    """Opens a GitHub issue per batch of corrections. GitHub then emails/pushes the owner."""

    def __init__(self, repo, token, owner=None):
        self.repo, self.token, self.owner = repo, token, owner

    @classmethod
    def from_env(cls):
        repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
        if not (repo and token):
            return None
        return cls(repo, token, os.environ.get("GITHUB_REPOSITORY_OWNER"))

    def _request(self, method, path, payload=None):
        req = urllib.request.Request(
            "https://api.github.com/repos/%s%s" % (self.repo, path),
            data=json.dumps(payload).encode() if payload is not None else None,
            method=method,
            headers={"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                     "User-Agent": USER_AGENT, "X-GitHub-Api-Version": "2022-11-28"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)

    def already_sent(self, key):
        since = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        issues = self._request("GET", "/issues?state=all&per_page=100&labels=%s&since=%s" % (ISSUE_LABEL, since))
        return any("correction-key:%s" % key in (issue.get("body") or "") for issue in issues)

    def send(self, title, body, key):
        if self.already_sent(key):
            log.info("Notification %s already exists; not sending again", key)
            return
        self._request("POST", "/issues", {"title": title, "body": body, "labels": [ISSUE_LABEL]})


# ---------------------------------------------------------------------------
# Tracker: storage and scheduling
# ---------------------------------------------------------------------------

def dump_game(game):
    """Game file text with one player per line, so git shows small, readable changes."""
    lines = ["{"]
    for field in ("game_id", "start_utc", "final_seen_utc", "away", "home", "pending"):
        lines.append(" %s: %s," % (json.dumps(field), json.dumps(game[field], sort_keys=True, ensure_ascii=False)))
    lines.append(' "players": {')
    players = sorted(game["players"].items(), key=lambda kv: (kv[1]["team"] or "", kv[1]["name"] or ""))
    for i, (pid, player) in enumerate(players):
        comma = "," if i < len(players) - 1 else ""
        lines.append("  %s: %s%s" % (json.dumps(pid), json.dumps(player, sort_keys=True, ensure_ascii=False), comma))
    lines += [" }", "}"]
    return "\n".join(lines) + "\n"


def write_if_changed(path, text):
    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() == text:
                return False
    except FileNotFoundError:
        pass
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return True


class Tracker:
    def __init__(self, base_dir, fetch=fetch_json, now=None, notifier=None, x_search=None):
        self.games_dir = os.path.join(base_dir, "data", "games")
        self.state_path = os.path.join(base_dir, "data", "state.json")
        self.corrections_path = os.path.join(base_dir, "data", "corrections.jsonl")
        self.corrections_log = os.path.join(base_dir, "logs", "corrections.log")
        os.makedirs(self.games_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self.corrections_log), exist_ok=True)
        self.fetch = fetch
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.notifier = notifier
        self.x_search = x_search
        self.counts = {"checked": 0, "new_games": 0, "pending": 0, "confirmed": 0, "cancelled": 0, "sent": 0,
                       "x_reads": 0, "x_searches": 0}

    # -- files --------------------------------------------------------------

    def load_games(self):
        games = {}
        for name in sorted(os.listdir(self.games_dir)):
            if name.endswith(".json"):
                with open(os.path.join(self.games_dir, name), encoding="utf-8") as fh:
                    game = json.load(fh)
                games[game["game_id"]] = game
        return games

    def save_game(self, game):
        write_if_changed(os.path.join(self.games_dir, game["game_id"] + ".json"), dump_game(game))

    def drop_game(self, game):
        os.remove(os.path.join(self.games_dir, game["game_id"] + ".json"))
        log.info("Finished watching %s (%s)", game_label(game), game_date(game))

    def load_state(self):
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return {"outbox": [], "notify_date": "", "notify_count": 0}

    def save_state(self, state):
        write_if_changed(self.state_path, json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False) + "\n")

    def _append_record(self, game, change, status, now):
        record = {
            "status": status,
            "detected_utc": change["detected_utc"],
            "resolved_utc": now.isoformat(timespec="seconds"),
            "minutes_after_final": change["minutes_after_final"],
            "game_id": game["game_id"], "game": game_label(game), "date": game_date(game),
            "player_id": change["player_id"], "player_name": change["player_name"], "team": change["team"],
            "stat": LABELS[change["stat"]], "old": change["old"], "new": change["new"], "delta": change["delta"],
            "headline": change["stat"] in HEADLINE_STATS, "note": change.get("note", ""),
            "play": change.get("play", ""),
        }
        with open(self.corrections_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")

    # -- discovery ----------------------------------------------------------

    def discover(self, games, now):
        """Add games that have gone final (today and the previous few days, ET)."""
        today_et = now.astimezone(ET).date()
        for offset in range(DISCOVER_DAYS_BACK + 1):
            day = today_et - timedelta(days=offset)
            try:
                board = self.fetch(SCOREBOARD_URL.format(date=day.strftime("%Y%m%d")))
            except (urllib.error.URLError, OSError, ValueError) as exc:
                log.warning("Could not load scoreboard for %s: %s", day, exc)
                continue
            for event in board.get("events", []):
                game_id = str(event["id"])
                if game_id in games or not event.get("status", {}).get("type", {}).get("completed"):
                    continue
                start = parse_time(event["date"])
                # Seen close to tip-off: it just ended. Otherwise it's an older game, so estimate.
                final_seen = now if now - start < LIVE_DISCOVERY_WINDOW else start + EST_GAME_LENGTH
                if now - final_seen > WATCH_WINDOW:
                    continue
                teams = {c["homeAway"]: {"abbr": nba_abbr(c["team"]["abbreviation"]),
                                         "name": c["team"].get("name"), "score": to_int(c.get("score"))}
                         for c in event["competitions"][0]["competitors"]}
                games[game_id] = {
                    "game_id": game_id,
                    "start_utc": start.isoformat(timespec="seconds"),
                    "final_seen_utc": final_seen.isoformat(timespec="seconds"),
                    "away": teams["away"], "home": teams["home"],
                    "pending": [], "players": {},
                }
                self.counts["new_games"] += 1
                log.info("Now watching %s (game %s)", game_label(games[game_id]), game_id)

    # -- checking -----------------------------------------------------------

    def is_due(self, game, now, slow_check=False):
        if not game["players"] or game["pending"]:
            return True
        age = now - as_time(game["final_seen_utc"])
        return age <= FAST_WINDOW or slow_check

    def fetch_summaries(self, games):
        def load(game):
            try:
                return self.fetch(SUMMARY_URL.format(game_id=game["game_id"]))
            except (urllib.error.URLError, OSError, ValueError) as exc:
                return exc
        if not games:
            return []
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_FETCHES) as pool:
            return list(pool.map(load, games))

    def check_game(self, game, summary, now):
        """Compare a fresh box score to the saved one. Returns corrections confirmed this run."""
        if isinstance(summary, Exception):
            log.warning("Could not load box score for %s: %s", game_label(game), summary)
            return []
        try:
            new = parse_summary(summary)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            log.warning("Could not read box score for %s: %r", game_label(game), exc)
            return []
        if not new["players"]:
            log.warning("Box score for %s came back empty; will retry", game_label(game))
            return []
        self.counts["checked"] += 1
        game["away"], game["home"] = new["away"], new["home"]

        if not game["players"]:
            game["players"] = new["players"]
            log.info("Saved starting box score for %s (%d players)", game_label(game), len(new["players"]))
            return []

        found = diff_players(game["players"], new["players"])
        attach_plays(found, new["plays"])
        changes = {(c["player_id"], c["stat"]): c for c in found}

        # Settle changes seen on an earlier run: still there = confirmed, flipped back = cancelled.
        confirmed, still_pending = [], []
        for held in game["pending"]:
            player = new["players"].get(held["player_id"])
            if player is None:                      # player missing from this fetch: decide next run
                still_pending.append(held)
                continue
            key = (held["player_id"], held["stat"])
            current = player["stats"].get(held["stat"], 0)
            if current == held["new"]:
                confirmed.append(held)
            else:
                self._append_record(game, held, "cancelled", now)
                self.counts["cancelled"] += 1
                log.info("CANCELLED %s: %s (now %d)", game_label(game), describe(held), current)
                if current == held["old"]:
                    changes.pop(key, None)          # back where it started: nothing to report
                elif key in changes:                # changed again: track from the original value
                    changes[key]["old"] = held["old"]
                    changes[key]["delta"] = changes[key]["new"] - held["old"]

        minutes_after = int((now - as_time(game["final_seen_utc"])).total_seconds() // 60)
        for change in changes.values():
            change["detected_utc"] = now.isoformat(timespec="seconds")
            change["minutes_after_final"] = minutes_after
            still_pending.append(change)
            self.counts["pending"] += 1
            log.info("CHANGE SEEN %s: %s (waiting one more check to confirm)", game_label(game), describe(change))
        game["pending"] = still_pending

        merged = dict(game["players"])              # keep last known stats for players missing this time
        merged.update(new["players"])
        game["players"] = merged

        for change in confirmed:
            self._record_confirmed(game, change, now)
        return confirmed

    def _record_confirmed(self, game, change, now):
        self._append_record(game, change, "confirmed", now)
        self.counts["confirmed"] += 1
        log.info("CORRECTION %s: %s", game_label(game), describe(change))
        headline = change["stat"] in HEADLINE_STATS
        lines = [
            "=" * 60,
            "Confirmed %s (first seen about %d min after final)%s" % (
                now.astimezone(ET).strftime("%m/%d %I:%M %p ET"), change["minutes_after_final"],
                "" if headline else "  [minor stat, no tweet]"),
            format_tweet(change, game) if headline else describe(change),
        ]
        if headline and change.get("note"):
            lines.append("Note: " + change["note"])
        with open(self.corrections_log, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    # -- notifying ----------------------------------------------------------

    def was_sent(self, key):
        """True if this notification already exists on GitHub, or if that can't be checked.

        Asked before every paid X search, so a search is never bought twice for the same
        notification, even if a run's saved records were lost.
        """
        if self.notifier is None:
            return False
        try:
            return self.notifier.already_sent(key)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("Could not check for an existing notification, so skipping the X search: %s", exc)
            return True

    def limit_alert(self, state, day, month, day_count, month_count):
        """Tell the owner, once, when a search limit stops the bot. Hitting one is unusual and worth a look."""
        monthly = month_count + X_SEARCH_SIZE > MAX_X_READS_PER_MONTH
        key = "x-limit-" + (month if monthly else day)
        if state.get("limit_alert") == key:
            return
        state["limit_alert"] = key
        lines = [
            "The bot has stopped searching X for the rest of %s because it reached its built-in limit." % (
                "this month" if monthly else "today"),
            "",
            "- Posts read today: %d (limit %d)" % (day_count, MAX_X_READS_PER_DAY),
            "- Posts read this month: %d (limit %d), about $%.2f" % (
                month_count, MAX_X_READS_PER_MONTH, month_count * X_READ_PRICE),
            "",
            "Game tracking and correction notifications are not affected. Searches start again by themselves.",
            "The bot normally reads far fewer posts than this, so check the balance in the X console.",
        ]
        if self.notifier and self.notifier.owner:
            lines.append("cc @" + self.notifier.owner)
        lines.append("<!-- correction-key:%s -->" % key)
        state["outbox"].append({"title": "X search limit reached", "body": "\n".join(lines), "key": key})

    def find_posts(self, state, query, start_time, now):
        """Search X for posts to reply to, within the daily and monthly read limits."""
        if self.x_search is None or self.counts["x_searches"] >= MAX_X_SEARCHES_PER_RUN:
            return []
        day, month = now.astimezone(ET).strftime("%Y-%m-%d"), now.astimezone(ET).strftime("%Y-%m")
        reads = state.setdefault("x_reads", {"day": "", "day_count": 0, "month": "", "month_count": 0, "total": 0})
        day_count = reads["day_count"] if reads["day"] == day else 0
        month_count = reads["month_count"] if reads["month"] == month else 0
        if day_count + X_SEARCH_SIZE > MAX_X_READS_PER_DAY or month_count + X_SEARCH_SIZE > MAX_X_READS_PER_MONTH:
            log.warning("X read limit reached (%d today, %d this month); skipping search", day_count, month_count)
            self.limit_alert(state, day, month, day_count, month_count)
            return []
        earliest = now - timedelta(days=6, hours=20)            # X only searches the last 7 days
        posts = self.x_search.search(skip_own_posts(query), max(start_time, earliest))
        self.counts["x_searches"] += 1
        self.counts["x_reads"] += len(posts)
        reads.update(day=day, day_count=day_count + len(posts), month=month, month_count=month_count + len(posts),
                     total=reads.get("total", 0) + len(posts))
        log.info("X search returned %d posts for: %s", len(posts), query)
        posts.sort(key=lambda p: p["likes"] + 2 * p["replies"] + p["reposts"], reverse=True)
        return posts[:REPLY_IDEAS_PER_SEARCH]

    def reply_ideas(self, state, game, corrections, now):
        """Extra issue lines for a correction: posts with drafted replies if X search is on, plus a free search link."""
        change = next(c for c in corrections if c["stat"] in HEADLINE_STATS)
        plain_query = "%s %s" % (last_name(change["player_name"]), STAT_WORDS[change["stat"]].split(" OR ")[0])
        lines = []
        posts = self.find_posts(state, correction_query(change), as_time(game["start_utc"]), now)
        if posts:
            lines += reply_section(posts, lambda post: correction_reply(change, game, post["id"]))
        lines += ["[Search X for posts about this](%s)" % search_link(plain_query), ""]
        return lines

    def daily_sweep(self, state, games, now, force=False):
        """Once a day, look for general stat-correction talk and queue an issue with reply ideas."""
        today = now.astimezone(ET).strftime("%Y-%m-%d")
        due = now.hour == SWEEP_HOUR_UTC and state.get("sweep_date") != today
        if self.x_search is None or not (due or force):
            return
        state["sweep_date"] = today
        if not force and self.was_sent("sweep-" + today):
            return
        posts = self.find_posts(state, SWEEP_QUERY, now - timedelta(hours=24), now)
        if not posts:
            return

        def draft(post):
            match = match_player(post["text"], games.values())
            return stat_line_reply(*match) if match else None

        date = "%d/%d" % (now.astimezone(ET).month, now.astimezone(ET).day)
        lines = ["People talking about stat corrections in the last 24 hours. Reply to the ones worth it.", ""]
        lines += reply_section(posts, draft, heading="### Posts")
        if self.notifier and self.notifier.owner:
            lines.append("cc @" + self.notifier.owner)
        key = "sweep-" + today
        lines.append("<!-- correction-key:%s -->" % key)
        state["outbox"].append({"title": "Posts to reply to | %s" % date, "body": "\n".join(lines), "key": key})

    def notify(self, batches, now, games=None, force_sweep=False):
        """Queue an issue per game with confirmed headline corrections, then send the queue."""
        state = self.load_state()
        mention = self.notifier.owner if self.notifier else None
        for game, corrections in batches:
            if any(c["stat"] in HEADLINE_STATS for c in corrections):
                key = build_issue(game, corrections, mention)[2]
                extra = [] if self.was_sent(key) else self.reply_ideas(state, game, corrections, now)
                title, body, key = build_issue(game, corrections, mention, extra)
                state["outbox"].append({"title": title, "body": body, "key": key})
        self.daily_sweep(state, games or {}, now, force_sweep)

        today = now.astimezone(ET).strftime("%Y-%m-%d")
        sent_today = state["notify_count"] if state["notify_date"] == today else 0
        unsent = []
        for item in state["outbox"]:
            if self.notifier is None:
                log.info("Not running on GitHub; skipping notification: %s", item["title"])
            elif sent_today >= MAX_NOTIFICATIONS_PER_DAY:
                log.warning("Daily notification cap reached; not sending: %s", item["title"])
            else:
                try:
                    self.notifier.send(item["title"], item["body"], item["key"])
                    sent_today += 1
                    state["notify_date"], state["notify_count"] = today, sent_today
                    self.counts["sent"] += 1
                    log.info("Notification sent: %s", item["title"])
                except (urllib.error.URLError, OSError, ValueError) as exc:
                    log.warning("Could not send notification (will retry next run): %s", exc)
                    unsent.append(item)
        state["outbox"] = unsent
        self.save_state(state)

    # -- one pass -----------------------------------------------------------

    def run_once(self):
        now = self.now()
        games = self.load_games()
        self.discover(games, now)
        for game in list(games.values()):
            if now - as_time(game["final_seen_utc"]) > WATCH_WINDOW and not game["pending"]:
                self.drop_game(games.pop(game["game_id"]))
        # Older games get one check per slow hour, however many runs land in that hour.
        state = self.load_state()
        slot = now.strftime("%Y-%m-%dT%H") if now.hour in SLOW_HOURS_UTC else None
        slow_check = slot is not None and state.get("slow_check") != slot
        if slow_check:
            state["slow_check"] = slot
            self.save_state(state)
        due = [g for g in games.values() if self.is_due(g, now, slow_check)]
        batches = []
        for game, summary in zip(due, self.fetch_summaries(due)):
            confirmed = self.check_game(game, summary, now)
            if confirmed:
                batches.append((game, confirmed))
            self.save_game(game)
        self.notify(batches, now, games)
        summary_line = ("Watching %d games. Checked %d this run. New games: %d. Changes seen: %d. "
                        "Confirmed: %d. Cancelled: %d. Notifications sent: %d. X posts read: %d." % (
                            len(games), self.counts["checked"], self.counts["new_games"], self.counts["pending"],
                            self.counts["confirmed"], self.counts["cancelled"], self.counts["sent"],
                            self.counts["x_reads"]))
        log.info(summary_line)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
                fh.write(summary_line + "\n")
        return [c for _, corrections in batches for c in corrections]

    # -- reporting ----------------------------------------------------------

    def report(self):
        games = self.load_games()
        print("Games being watched: %d" % len(games))
        records = []
        if os.path.exists(self.corrections_path):
            with open(self.corrections_path, encoding="utf-8") as fh:
                records = [json.loads(line) for line in fh if line.strip()]
        confirmed = [r for r in records if r["status"] == "confirmed"]
        print("Corrections confirmed: %d (%d would be tweeted, %d minor). Cancelled (flipped back): %d" % (
            len(confirmed), sum(r["headline"] for r in confirmed), sum(not r["headline"] for r in confirmed),
            len(records) - len(confirmed)))
        reads = self.load_state().get("x_reads")
        if reads:
            print("X posts read: %d this month (about $%.2f), %d in total (about $%.2f)" % (
                reads["month_count"], reads["month_count"] * X_READ_PRICE,
                reads["total"], reads["total"] * X_READ_PRICE))
        waiting = sum(len(g["pending"]) for g in games.values())
        if waiting:
            print("Changes waiting to be confirmed on the next run: %d" % waiting)
        if not confirmed:
            return
        print("\nBy stat:")
        by_stat = {}
        for r in confirmed:
            by_stat[r["stat"]] = by_stat.get(r["stat"], 0) + 1
        for stat, n in sorted(by_stat.items(), key=lambda kv: -kv[1]):
            print("  %-5s %d" % (stat, n))
        print("\nWhen they were first seen (after the game ended):")
        low = None
        for high, name in ((60, "within 1 hour"), (360, "1-6 hours"), (1440, "6-24 hours"), (None, "after 24 hours")):
            n = sum(1 for r in confirmed if (low is None or r["minutes_after_final"] >= low)
                    and (high is None or r["minutes_after_final"] < high))
            print("  %-15s %d" % (name, n))
            low = high
        print("\nLatest corrections:")
        for r in confirmed[-10:][::-1]:
            print("  %s  %s  %s (%s) %s %d -> %d %s" % (
                r["detected_utc"][:16].replace("T", " "), r["game"], r["player_name"], r["team"],
                r["stat"], r["old"], r["new"], r["note"]))


def send_test_notification(notifier):
    game = {
        "game_id": "test", "start_utc": "2026-11-15T02:00:00+00:00",
        "away": {"abbr": "LAL", "name": "Lakers", "score": 112},
        "home": {"abbr": "DEN", "name": "Nuggets", "score": 118},
    }
    change = {"player_id": "test", "player_name": "Nikola Jokic", "team": "DEN", "stat": "assists",
              "old": 10, "new": 9, "delta": -1, "note": "", "minutes_after_final": 95}
    title, body, _ = build_issue(game, [change], notifier.owner)
    key = "test-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    body = ("**This is a test. The correction below is made up.** Tap the Post to X link to check that X "
            "opens with the tweet filled in. You don't need to post it.\n\n"
            + body.rsplit("<!--", 1)[0] + "<!-- correction-key:%s -->" % key)
    notifier.send("TEST: " + title, body, key)


def main():
    parser = argparse.ArgumentParser(description="Track NBA postgame stat corrections.")
    parser.add_argument("command", choices=["once", "report", "test-notify", "test-search"])
    parser.add_argument("--local", action="store_true",
                        help="allow 'once' outside GitHub Actions (changes the data files on this computer)")
    args = parser.parse_args()
    base_dir = os.path.dirname(os.path.abspath(__file__))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", stream=sys.stdout)
    notifier = GitHubNotifier.from_env()

    if args.command == "report":
        Tracker(base_dir).report()
    elif args.command == "test-notify":
        if notifier is None:
            sys.exit("test-notify only works on GitHub. Use the 'Run now' button with the test option.")
        send_test_notification(notifier)
        log.info("Test notification sent.")
    elif args.command == "test-search":
        x_search = XSearch.from_env()
        if notifier is None or x_search is None:
            sys.exit("test-search only works on GitHub, with the X_BEARER_TOKEN secret set.")
        tracker = Tracker(base_dir, notifier=notifier, x_search=x_search)
        tracker.notify([], tracker.now(), tracker.load_games(), force_sweep=True)   # today's daily search, now
        log.info("Test search done. Posts read: %d. Notifications sent: %d.",
                 tracker.counts["x_reads"], tracker.counts["sent"])
    else:
        if not os.environ.get("GITHUB_ACTIONS") and not args.local:
            sys.exit("GitHub runs this on a schedule. Running it here too would make the data files on this "
                     "computer differ from GitHub's. Add --local if you really want to.")
        Tracker(base_dir, notifier=notifier, x_search=XSearch.from_env()).run_once()


if __name__ == "__main__":
    main()
