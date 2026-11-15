# NBA Stat Correction Tracker

Watches NBA games after they go final and catches postgame stat corrections
(a stat added to or taken away from a player). It runs on GitHub for free, so no
computer has to stay on. It does **not** post to X by itself: when it finds a
correction it notifies you with the tweet already written, and you press Post.

Data comes from ESPN's public NBA feed, because NBA.com's own feeds block scripts.

## How it works

1. GitHub runs `logger.py` every 15 minutes, all day.
2. Each run looks for games that have finished and saves their box scores.
   - Games that ended in the last 24 hours are checked on every run.
   - Games 1 to 7 days old are checked four times a day.
   - After 7 days a game is dropped.
3. If a player's stat changes, the tracker waits one more run to make sure the change
   sticks. A stat that flips back is logged as cancelled and never sent.
4. Confirmed corrections for a game go into one GitHub issue. GitHub emails you, or
   sends a phone notification if you use the GitHub app.
5. Each tweet in the issue has a **Post to X** link. Tap it and X opens with the text
   filled in. Press Post.

Main stats (PTS, REB, AST, STL, BLK, 3PM, TOV) get a tweet. Other stat changes
(FGA, FTA, OREB, DREB, PF and so on) are logged as minor, with no tweet.

## Finding posts to reply to

This part is optional and only runs when the repository has a secret named `X_BEARER_TOKEN`
(an X API token). X charges about half a cent for each post a search returns.

- **After a confirmed correction**, the bot searches X for posts about that player and stat.
  The notification lists the three busiest posts, each with a drafted reply and a
  **Reply on X** link that opens X with the reply attached to that post.
- **Once a day** (around 10am US Eastern) it searches for general stat-correction talk and
  sends a "Posts to reply to" notification. If a post names a player the tracker is watching,
  the draft quotes his current box score. Otherwise you write the reply yourself.
- **Spending limits** are built in: 40 posts a day and 900 a month (about $4.50). The bot
  stops searching when it hits either one. `python3 logger.py report` shows the running total.

Without the token, every correction notification still has a free **Search X** link that
opens X's own search for that player and stat.

To turn it on: repository Settings > Secrets and variables > Actions > New repository secret,
name `X_BEARER_TOKEN`, value the Bearer Token from your X developer app.
To turn it off, delete that secret.

## Where things are

| File | What's in it |
| --- | --- |
| `data/games/` | One file per game being watched, with each player's last known stats |
| `data/corrections.jsonl` | Every confirmed or cancelled correction, one per line |
| `logs/corrections.log` | The draft tweet for each confirmed correction |
| `data/state.json` | Notifications waiting to be sent and today's count |
| `.github/workflows/track.yml` | The schedule GitHub follows |

GitHub saves these files back to the repository whenever something changes, so the
repository's history is a record of every box score change.

## Everyday use

- **See what it has found:** open the repository's Issues tab, or look at
  `logs/corrections.log`.
- **Run it right now:** Actions tab > "Track stat corrections" > "Run workflow".
- **Send a test notification:** same button, with "Send one sample notification" ticked.
- **Pause it:** Actions tab > "Track stat corrections" > "..." menu > "Disable workflow".
  Turn it back on the same way.

On this computer, get the latest data and print a summary:

```bash
git pull
```

```bash
python3 logger.py report
```

Don't run `python3 logger.py once` here. GitHub does that, and running it in two places
makes the data files disagree.

## Tweet format

```
🔴 NBA Stat Correction | 11/14

Nikola Jokic (DEN): -1 AST
DEN 116 @ NOP 108 | Final

Now: 9 AST
Was: 10 AST

#NBA #NBAProps
```

🔴 means a stat was removed and 🟢 means one was added.

## Limits to know about

- The tracker's first look at a finished game can be 15 to 45 minutes after it ends.
  Corrections made before that are missed.
- GitHub's scheduled runs can start late at busy times.
- GitHub pauses scheduled runs after 60 days with no repository activity, so the
  workflow needs turning back on each October.
- The repository is public so that GitHub runs the schedule for free. Notifications (issues)
  and run logs are public too. API tokens are stored as GitHub secrets and are never shown.

## Tests

```bash
python3 -m unittest test_logger
```
