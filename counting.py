from nba_api.live.nba.endpoints import scoreboard, PlayByPlay
from nba_api.live.nba.library.http import NBALiveHTTP
from datetime import datetime
import time
import sys
import os
import re


def _nba_cdn_headers():
    """
    cdn.nba.com often returns 403 HTML (not JSON) unless the request looks like
    it comes from the NBA site. nba_api's default Chrome 87 UA alone is not enough.
    """
    h = dict(NBALiveHTTP.headers)
    h.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.nba.com/",
            "Origin": "https://www.nba.com",
            "Accept": "application/json, text/plain, */*",
        }
    )
    return h


NBA_CDN_HEADERS = _nba_cdn_headers()

# Fix Windows console encoding for emojis
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        import io

        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")


def get_team_display_name(team_data):
    """Get a nice display name for a team"""
    return f"{team_data['teamCity']} {team_data['teamName']}"


def format_clock_time(clock_str):
    """Convert PT04M03.00S format to M:SS format"""
    if not clock_str or clock_str == "N/A":
        return ""

    # Parse PT04M03.00S format
    match = re.search(r"PT(\d+)M(\d+(?:\.\d+)?)S", clock_str)
    if match:
        minutes = int(match.group(1))
        seconds = float(match.group(2))
        return f"{minutes}:{int(seconds):02d}"

    return clock_str


def get_recent_plays(game_id, num_plays=20):
    """Fetch recent plays for a game"""
    try:
        pbp = PlayByPlay(game_id=game_id, headers=NBA_CDN_HEADERS)
        pbp_data = pbp.get_dict()

        if "game" in pbp_data and "actions" in pbp_data["game"]:
            actions = pbp_data["game"]["actions"]
            # Return the most recent plays (last N plays)
            return actions[-num_plays:] if len(actions) > num_plays else actions
    except Exception as e:
        return None

    return None


def display_recent_plays_espn_style(game, num_plays=20):
    """Display recent plays in ESPN-style format"""
    game_id = game.get("gameId")
    if not game_id:
        return

    home_team = game["homeTeam"]
    away_team = game["awayTeam"]
    home_tricode = home_team.get("teamTricode", "")
    away_tricode = away_team.get("teamTricode", "")

    plays = get_recent_plays(game_id, num_plays)
    if not plays:
        return

    print("\n" + "=" * 100)
    print("RECENT PLAYS")
    print("=" * 100)
    print(f"{'TIME':<8} {'PLAY':<70} {away_tricode:>6} {home_tricode:>6}")
    print("-" * 100)

    for play in reversed(plays):  # Show most recent first (reverse order)
        clock = format_clock_time(play.get("clock", ""))
        description = play.get("description", "")
        score_away = play.get("scoreAway", "")
        score_home = play.get("scoreHome", "")

        # Format scores (show empty if not available)
        away_score_str = str(score_away) if score_away != "" else ""
        home_score_str = str(score_home) if score_home != "" else ""

        # Truncate description if too long
        if len(description) > 68:
            description = description[:65] + "..."

        print(f"{clock:<8} {description:<70} {away_score_str:>6} {home_score_str:>6}")

    print("=" * 100)
    print()


def display_game_state(game):
    """Display the current state of a game"""
    home_team = game["homeTeam"]
    away_team = game["awayTeam"]
    game_status = game["gameStatusText"]

    home_name = get_team_display_name(home_team)
    away_name = get_team_display_name(away_team)
    home_score = home_team["score"]
    away_score = away_team["score"]

    print("=" * 60)
    print(f"{away_name:30} @ {home_name:30}")
    print("-" * 60)
    print(f"{'AWAY':^30} | {'HOME':^30}")
    print(f"{away_score:^30} | {home_score:^30}")
    print("-" * 60)
    print(f"Status: {game_status}")

    # Display additional game details if game is live
    if game["gameStatus"] == 2:  # Game is live
        period = game.get("period", 0)
        game_clock = game.get("gameClock", "N/A")
        print(f"Quarter: {period} | Time: {game_clock}")

        # Show team leaders if available
        if "homeTeam" in game and "statistics" in home_team:
            print("\n--- Team Leaders ---")
            # Note: Live API may have different structure for stats

    print("=" * 60)
    print()


def find_warriors_game(games_data):
    """Find the Warriors game from the scoreboard"""
    warriors_keywords = ["Warriors", "GSW", "Golden State"]

    if "scoreboard" not in games_data or "games" not in games_data["scoreboard"]:
        return None

    for game in games_data["scoreboard"]["games"]:
        home_team = game["homeTeam"]
        away_team = game["awayTeam"]

        # Check if Warriors are playing
        home_tricode = home_team.get("teamTricode", "")
        away_tricode = away_team.get("teamTricode", "")
        home_name = home_team.get("teamName", "")
        away_name = away_team.get("teamName", "")

        if (
            home_tricode == "GSW"
            or away_tricode == "GSW"
            or "Warriors" in home_name
            or "Warriors" in away_name
        ):
            return game

    return None


def find_spurs_game(games_data):
    """Find the Spurs game from the scoreboard"""
    spurs_keywords = ["Spurs", "SAS", "San Antonio Spurs"]
    if "scoreboard" not in games_data or "games" not in games_data["scoreboard"]:
        return None
    for game in games_data["scoreboard"]["games"]:
        home_team = game["homeTeam"]
        away_team = game["awayTeam"]

        # Check if Spurs are playing
        home_tricode = home_team.get("teamTricode", "")
        away_tricode = away_team.get("teamTricode", "")
        home_name = home_team.get("teamName", "")
        away_name = away_team.get("teamName", "")

        if (
            home_tricode == "SAS"
            or away_tricode == "SAS"
            or "Spurs" in home_name
            or "Spurs" in away_name
        ):
            return game

    return None


def find_rockets_game(games_data):
    """Find the Rockets game from the scoreboard"""
    if "scoreboard" not in games_data or "games" not in games_data["scoreboard"]:
        return None
    for game in games_data["scoreboard"]["games"]:
        home_team = game["homeTeam"]
        away_team = game["awayTeam"]

        # Check if Rockets are playing
        home_tricode = home_team.get("teamTricode", "")
        away_tricode = away_team.get("teamTricode", "")
        home_name = home_team.get("teamName", "")
        away_name = away_team.get("teamName", "")

        if (
            home_tricode == "HOU"
            or away_tricode == "HOU"
            or "Rockets" in home_name
            or "Rockets" in away_name
        ):
            return game

    return None


def find_thunder_game(games_data):
    """Find the Thunder game from the scoreboard"""
    if "scoreboard" not in games_data or "games" not in games_data["scoreboard"]:
        return None
    for game in games_data["scoreboard"]["games"]:
        home_team = game["homeTeam"]
        away_team = game["awayTeam"]

        # Check if Thunder are playing
        home_tricode = home_team.get("teamTricode", "")
        away_tricode = away_team.get("teamTricode", "")
        home_name = home_team.get("teamName", "")
        away_name = away_team.get("teamName", "")

        if (
            home_tricode == "OKC"
            or away_tricode == "OKC"
            or "Thunder" in home_name
            or "Thunder" in away_name
        ):
            return game

    return None


def display_all_games(games_data):
    """Display all games from today"""
    if "scoreboard" not in games_data or "games" not in games_data["scoreboard"]:
        print("No games found for today.")
        return False

    games = games_data["scoreboard"]["games"]

    if not games:
        print("No games scheduled for today.")
        return False

    print(f"\nGames - {datetime.now().strftime('%B %d, %Y')}")
    print(f"Total games: {len(games)}\n")

    for game in games:
        display_game_state(game)

    return True


def main(show_p2p=False, show_spurs_detail=False):
    """Main function to fetch and display live game data.

    show_p2p: ESPN-style recent plays for Warriors and Thunder (OKC) games.
    """
    print("Live Game Tracker")
    print("Fetching live game data...\n")

    try:
        # Get today's scoreboard
        board = scoreboard.ScoreBoard(headers=NBA_CDN_HEADERS)
        games_data = board.get_dict()

        # Try to find team games (collect unique games to avoid duplicates)
        tracked_games = {}

        warriors_game = find_warriors_game(games_data)
        if warriors_game:
            game_id = warriors_game.get("gameId", id(warriors_game))
            tracked_games[game_id] = ("Warriors", warriors_game)

        spurs_game = find_spurs_game(games_data)
        if spurs_game:
            game_id = spurs_game.get("gameId", id(spurs_game))
            if game_id not in tracked_games:
                tracked_games[game_id] = ("Spurs", spurs_game)
            else:
                # Multiple teams in same game
                tracked_games[game_id] = (
                    tracked_games[game_id][0] + " & Spurs",
                    spurs_game,
                )

        rockets_game = find_rockets_game(games_data)
        if rockets_game:
            game_id = rockets_game.get("gameId", id(rockets_game))
            if game_id not in tracked_games:
                tracked_games[game_id] = ("Rockets", rockets_game)
            else:
                # Multiple teams in same game
                existing_teams = tracked_games[game_id][0]
                tracked_games[game_id] = (existing_teams + " & Rockets", rockets_game)

        thunder_game = find_thunder_game(games_data)
        if thunder_game:
            game_id = thunder_game.get("gameId", id(thunder_game))
            if game_id not in tracked_games:
                tracked_games[game_id] = ("Thunder", thunder_game)
            else:
                # Multiple teams in same game
                existing_teams = tracked_games[game_id][0]
                tracked_games[game_id] = (existing_teams + " & Thunder", thunder_game)

        if tracked_games:
            for team_name, game in tracked_games.values():
                print(f"{team_name} Game Found!\n")
                display_game_state(game)

                # Recent plays: --p2p for Warriors & Thunder; --spurs for Spurs
                if (
                    ("Warriors" in team_name or "Thunder" in team_name) and show_p2p
                ) or ("Spurs" in team_name and show_spurs_detail):
                    display_recent_plays_espn_style(game)

                print()  # Add spacing between games

        else:
            print("No tracked team games found today.")
            print("Showing all games instead:\n")
            display_all_games(games_data)

    except Exception as e:
        print(f"❌ Error fetching game data: {e}")
        print("\nTroubleshooting tips:")
        print("1. Make sure you have internet connection")
        print("2. Check if NBA.com APIs are accessible")
        print("3. Verify nba_api is installed: pip install nba_api")
        sys.exit(1)


def watch_game_live(refresh_interval=30, show_p2p=False, show_spurs_detail=False):
    """
    Continuously update the game state (Warriors, Spurs, Rockets, Thunder focus)

    Args:
        refresh_interval: Seconds between updates (default: 30)
        show_p2p: Play-by-play for Warriors and Thunder (OKC) games (default: False)
        show_spurs_detail: Whether to show Spurs play-by-play data (default: False)
    """
    print(" Live Game Tracker (Live Mode - Warriors, Spurs, Rockets & Thunder Focus)")
    if show_p2p:
        print(" Play-by-Play mode enabled (Warriors & Thunder)")
    if show_spurs_detail:
        print(" Spurs detail mode enabled")
    print(f"Refreshing every {refresh_interval} seconds...")
    print("Press Ctrl+C to stop\n")

    try:
        while True:
            # Clear screen (cross-platform)
            print("\033[H\033[J", end="")

            # Get and display current games
            board = scoreboard.ScoreBoard(headers=NBA_CDN_HEADERS)
            games_data = board.get_dict()

            # Collect unique games to avoid duplicates (e.g., OKC vs Spurs)
            tracked_games = {}

            warriors_game = find_warriors_game(games_data)
            if warriors_game:
                game_id = warriors_game.get("gameId", id(warriors_game))
                tracked_games[game_id] = ("Warriors", warriors_game)

            spurs_game = find_spurs_game(games_data)
            if spurs_game:
                game_id = spurs_game.get("gameId", id(spurs_game))
                if game_id not in tracked_games:
                    tracked_games[game_id] = ("Spurs", spurs_game)
                else:
                    # Multiple teams in same game
                    tracked_games[game_id] = (
                        tracked_games[game_id][0] + " & Spurs",
                        spurs_game,
                    )

            rockets_game = find_rockets_game(games_data)
            if rockets_game:
                game_id = rockets_game.get("gameId", id(rockets_game))
                if game_id not in tracked_games:
                    tracked_games[game_id] = ("Rockets", rockets_game)
                else:
                    # Multiple teams in same game
                    existing_teams = tracked_games[game_id][0]
                    tracked_games[game_id] = (
                        existing_teams + " & Rockets",
                        rockets_game,
                    )

            thunder_game = find_thunder_game(games_data)
            if thunder_game:
                game_id = thunder_game.get("gameId", id(thunder_game))
                if game_id not in tracked_games:
                    tracked_games[game_id] = ("Thunder", thunder_game)
                else:
                    # Multiple teams in same game
                    existing_teams = tracked_games[game_id][0]
                    tracked_games[game_id] = (
                        existing_teams + " & Thunder",
                        thunder_game,
                    )

            if tracked_games:
                print(
                    f" Game Update (Updated: {datetime.now().strftime('%I:%M:%S %p')})\n"
                )
                for team_name, game in tracked_games.values():
                    print(f"{team_name} Game Found!\n")
                    display_game_state(game)

                    if (
                        ("Warriors" in team_name or "Thunder" in team_name)
                        and show_p2p
                    ) or ("Spurs" in team_name and show_spurs_detail):
                        display_recent_plays_espn_style(game)

                    print()  # Add spacing between games
            else:
                print(
                    f" All Games (Updated: {datetime.now().strftime('%I:%M:%S %p')})\n"
                )
                display_all_games(games_data)

            print(f"\n Next update in {refresh_interval} seconds... (Ctrl+C to stop)")
            time.sleep(refresh_interval)

    except KeyboardInterrupt:
        print("\n\n👋 Stopped live tracking. Goodbye!")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        sys.exit(1)


def watch_all_games_live(refresh_interval=30):
    """
    Continuously update all games

    Args:
        refresh_interval: Seconds between updates (default: 30)
    """
    print(" Live Game Tracker (Live Mode - All Games)")
    print(f"Refreshing every {refresh_interval} seconds...")
    print("Press Ctrl+C to stop\n")

    try:
        while True:
            # Clear screen (cross-platform)
            print("\033[H\033[J", end="")

            # Get and display current games
            board = scoreboard.ScoreBoard(headers=NBA_CDN_HEADERS)
            games_data = board.get_dict()

            print(f"--------------------------------")
            print(f" Games (Updated: {datetime.now().strftime('%I:%M:%S %p')})\n")
            display_all_games(games_data)

            print(f" Next update in {refresh_interval} seconds... (Ctrl+C to stop)")
            time.sleep(refresh_interval)

    except KeyboardInterrupt:
        print("\n\n Stopped live tracking. Goodbye!")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    # Check command line arguments
    show_all_games = "--all" in sys.argv
    show_p2p = "--p2p" in sys.argv
    show_spurs_detail = "--spurs" in sys.argv

    # Check if user wants live mode
    if "--live" in sys.argv:
        refresh_interval = 30
        # Look for refresh interval argument
        for i, arg in enumerate(sys.argv):
            if arg == "--live" and i + 1 < len(sys.argv):
                try:
                    next_arg = sys.argv[i + 1]
                    if next_arg not in ["--all", "--p2p", "--spurs"]:
                        refresh_interval = int(next_arg)
                except ValueError:
                    print("Invalid refresh interval. Using default (30 seconds)")
                break

        if show_all_games:
            watch_all_games_live(refresh_interval)
        else:
            watch_game_live(
                refresh_interval,
                show_p2p=show_p2p,
                show_spurs_detail=show_spurs_detail,
            )
    elif show_all_games:
        # Show all games once
        print("Live Game Tracker - All Games")
        print("Fetching live game data...\n")
        try:
            board = scoreboard.ScoreBoard(headers=NBA_CDN_HEADERS)
            games_data = board.get_dict()
            display_all_games(games_data)
        except Exception as e:
            print(f"❌ Error fetching game data: {e}")
            sys.exit(1)
    else:
        main(show_p2p=show_p2p, show_spurs_detail=show_spurs_detail)
        print("\n💡 Tip: Run with '--live' flag for continuous updates:")
        print("   python counting.py --live")
        print("   python counting.py --live 15  (refresh every 15 seconds)")
        print("\n💡 To see all games instead of just Warriors:")
        print("   python counting.py --all")
        print("   python counting.py --all --live")
        print("\n💡 To show play-by-play for Warriors and Thunder (OKC) games:")
        print("   python counting.py --p2p")
        print("   python counting.py --p2p --live")
        print("\n💡 To show detailed play-by-play for Spurs games:")
        print("   python counting.py --spurs")
        print("   python counting.py --spurs --live")
   