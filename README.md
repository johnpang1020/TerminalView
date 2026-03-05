# NBA Live Game Tracker

A Python script to track live NBA games with a focus on Warriors, Spurs, Rockets, and Thunder games. Displays real-time scores, game status, and play-by-play data.

## Features

- **Team-focused tracking**: Automatically finds and displays games for Warriors, Spurs, Rockets, and Thunder
- **Live updates**: Continuous monitoring with customizable refresh intervals
- **Play-by-play**: ESPN-style play-by-play display for Warriors games
- **All games mode**: Option to view all NBA games instead of just tracked teams
- **Cross-platform**: Works on Windows, macOS, and Linux with proper encoding support

## Command Options

| Command                           | Description                                           |
| --------------------------------- | ----------------------------------------------------- |
| `python counting.py`              | Show Warriors, Rockets, Spurs, Thunder games once     |
| `python counting.py --all`        | Show all NBA games once                               |
| `python counting.py --live`       | Live tracking (Warriors, Spurs, Rockets, Thunder)     |
| `python counting.py --all --live` | Live tracking (All games)                              |
| `python counting.py --live 15`    | Custom refresh interval (15 seconds)                  |
| `python counting.py --p2p`        | Show play-by-play for Warriors games                  |
| `python counting.py --p2p --live` | Live tracking with play-by-play for Warriors games    |

## Requirements

- Python 3.10+
- nba_api
- requests
- Internet connection

## Installation

```bash
pip install -r requirements.txt
```

## Usage Examples

### Check games once
```bash
python counting.py
```

### View all games
```bash
python counting.py --all
```

### Live tracking with 15-second updates
```bash
python counting.py --live 15
```

### Live tracking with play-by-play
```bash
python counting.py --p2p --live
```

### View all games with live updates
```bash
python counting.py --all --live 30
```

## API Source

Based on [nba_api](https://github.com/swar/nba_api) - An API Client package to access NBA.com APIs.

## Tips

- **Before games**: Run once to check start times
- **During live games**: Use `--live 15` for frequent updates
- **Multiple games**: Use `--all` to see everything happening today
- **Play-by-play**: Use `--p2p` flag to see detailed play descriptions for Warriors games
- **Network issues**: Script will show error if NBA.com APIs are unreachable
- **Stop live mode**: Press `Ctrl+C` to exit live tracking mode
