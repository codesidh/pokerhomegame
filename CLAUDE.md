# Poker Night Tournament Bot

## Project Overview
Telegram bot for managing home poker tournament finances. Host-controlled — the host approves all player joins, rebuys, and records winners. Payouts auto-calculate based on player count.

## Tech Stack
- Python 3.9+
- `python-telegram-bot` library (async, v20+)
- JSON file persistence (`poker_data.json`)
- Deployed on AWS EC2

## Architecture
- Single-file bot: `poker_bot.py` (843 lines)
- Data stored per chat_id in `poker_data.json` — each Telegram group is independent
- Uses inline keyboard buttons for host approval workflow and winner selection
- Greedy algorithm for minimal settlement transactions

## Key Conventions
- Bot token loaded from `BOT_TOKEN` environment variable
- Payout structure: >6 players = top 3 (60/30/10%), <=6 players = top 2 (70/30%)
- All player actions (join, rebuy) require host approval via inline buttons
- User IDs stored as strings in game state

## Commands
- Host: /newgame, /status, /winners, /settle, /endgame, /history, /kick
- Player: /join, /rebuy, /mystatus

## Running Locally
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
export BOT_TOKEN="your-token-here"
python poker_bot.py
```

## Deploying to AWS EC2
See `deploy/` directory for systemd service file and setup script.
