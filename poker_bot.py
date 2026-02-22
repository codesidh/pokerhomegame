"""
Poker Night Tournament Bot - Host-Controlled Edition
======================================================
Tournament-style home game management via Telegram.
Host controls all approvals. Payouts are automatic based on placement.

Payout Structure:
  > 6 players: Top 3 paid (50% / 30% / 20%)
  <= 6 players: Top 2 paid (60% / 40%)

Setup:
1. Message @BotFather on Telegram -> /newbot -> get your BOT_TOKEN
2. pip install python-telegram-bot
3. export BOT_TOKEN="your-token"
4. python poker_bot.py
5. Add bot to your poker group chat(s) - each group runs independently
"""

import io
import os
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Load .env file if present (so we don't need python-dotenv)
env_path = Path(__file__).resolve().parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
DATA_FILE = "poker_data.json"

# ── Payout Structures ────────────────────────────────────────────────────────
PAYOUT_3 = {1: 0.50, 2: 0.30, 3: 0.20}   # > 6 players
PAYOUT_2 = {1: 0.60, 2: 0.40}              # <= 6 players
HISTORY_LIMIT = 100
BAR_MAX_LEN = 14


def get_payout_structure(player_count: int) -> dict:
    if player_count > 6:
        return PAYOUT_3
    else:
        return PAYOUT_2


def format_payout_structure(player_count: int) -> str:
    if player_count > 6:
        return "Top 3 paid: 1st 50% | 2nd 30% | 3rd 20%"
    else:
        return "Top 2 paid: 1st 60% | 2nd 40%"


def format_date_ordinal(dt: datetime) -> str:
    """Format date like 'Feb 20th 2025'."""
    day = dt.day
    if 11 <= day <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return dt.strftime(f"%b {day}{suffix} %Y")


def generate_game_name(game: dict) -> str:
    """Auto-generate game name: 'Feb 20th 2025 - Game 1'."""
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    games_today = sum(
        1 for h in game.get("history", [])
        if h.get("date", "")[:10] == today_str
    )
    # Also count current active game if it started today
    if game.get("active") and game.get("started_at", "")[:10] == today_str:
        games_today += 1
    game_number = games_today + 1
    return f"{format_date_ordinal(now)} - Game {game_number}"


def make_bar(value: float, max_val: float) -> str:
    """Create a visual bar using block characters."""
    if max_val == 0:
        return ""
    length = int(abs(value) / max_val * BAR_MAX_LEN)
    return "\u2588" * max(length, 1)


# ── Persistence ──────────────────────────────────────────────────────────────
def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {}


def save_data(data: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_game(chat_id: str, data: dict) -> dict:
    if chat_id not in data:
        data[chat_id] = {
            "active": False,
            "host_id": None,
            "host_name": None,
            "buy_in_amount": None,
            "game_name": None,
            "players": {},
            "pending": [],
            "winners": {},
            "started_at": None,
            "history": [],
            "rebuy_locked": False,
            "lobby_message_id": None,
            "host_panel_message_id": None,
        }
    g = data[chat_id]
    if "winners" not in g:
        g["winners"] = {}
    if "rebuy_locked" not in g:
        g["rebuy_locked"] = False
    if "lobby_message_id" not in g:
        g["lobby_message_id"] = None
    if "game_name" not in g:
        g["game_name"] = None
    if "host_panel_message_id" not in g:
        g["host_panel_message_id"] = None
    if "nicknames" not in g:
        g["nicknames"] = {}
    return g


def is_host(game: dict, user_id: str) -> bool:
    return game["host_id"] == user_id


# ── Helpers ──────────────────────────────────────────────────────────────────
def display_name(player: dict) -> str:
    return player.get("nickname") or player["name"]


def get_total_pot(game: dict) -> float:
    return sum(sum(p["buy_ins"]) for p in game["players"].values())


def player_summary(game: dict) -> str:
    lines = []
    for uid, p in game["players"].items():
        total_in = sum(p["buy_ins"])
        rebuys = len(p["buy_ins"]) - 1
        rebuy_str = f" (+{rebuys} rebuy{'s' if rebuys > 1 else ''})" if rebuys > 0 else ""
        elim = " [OUT]" if p.get("eliminated") else ""
        lines.append(f"  \u2022 {display_name(p)}: ${total_in:.2f} in{rebuy_str}{elim}")
    return "\n".join(lines) if lines else "  No players yet."


def calculate_settlements(winners: dict, players: dict) -> list:
    if not winners:
        return []

    nets = {}
    for uid, p in players.items():
        total_in = sum(p["buy_ins"])
        payout = 0.0
        for w in winners.values():
            if w["user_id"] == uid:
                payout = w["payout"]
                break
        nets[display_name(p)] = payout - total_in

    debtors = sorted([(k, -v) for k, v in nets.items() if v < 0], key=lambda x: -x[1])
    creditors = sorted([(k, v) for k, v in nets.items() if v > 0], key=lambda x: -x[1])

    if not debtors and not creditors:
        return []

    settlements = []
    i, j = 0, 0
    while i < len(debtors) and j < len(creditors):
        d_name, debt = debtors[i]
        c_name, credit = creditors[j]
        amount = min(debt, credit)
        if amount > 0.01:
            settlements.append((d_name, c_name, amount))
        debtors[i] = (d_name, debt - amount)
        creditors[j] = (c_name, credit - amount)
        if debtors[i][1] < 0.01:
            i += 1
        if creditors[j][1] < 0.01:
            j += 1

    return settlements


def build_pnl(winners: dict, players: dict) -> list:
    """Build sorted P&L list: [(name, net_amount), ...]"""
    pnl = []
    for p_uid, p in players.items():
        total_in = sum(p["buy_ins"])
        payout = 0.0
        for w in winners.values():
            if w["user_id"] == p_uid:
                payout = w["payout"]
                break
        net = payout - total_in
        pnl.append((display_name(p), net))
    pnl.sort(key=lambda x: -x[1])
    return pnl


def build_name_map(game: dict) -> dict:
    """Build a mapping from any historical player name to their canonical nickname.

    Uses the nicknames dict (uid -> nickname) and active players dict to resolve
    names used in history records to their current nicknames.
    """
    name_map = {}
    nicknames = game.get("nicknames", {})

    # Build uid->nickname from the nicknames dict
    uid_to_nick = dict(nicknames)

    # Also pull from active players if available
    for uid, p in game.get("players", {}).items():
        nick = p.get("nickname") or p.get("name")
        if uid in uid_to_nick:
            nick = uid_to_nick[uid]
        # Map real name -> nickname
        real_name = p.get("name", "")
        if real_name and real_name != nick:
            name_map[real_name] = nick
        name_map[nick] = nick

    # Map nicknames to themselves
    for uid, nick in uid_to_nick.items():
        name_map[nick] = nick

    return name_map


def build_leaderboard_stats(game: dict) -> tuple:
    """Aggregate per-player stats across all completed history games.

    Returns (stats_list, game_count) where stats_list is sorted by total_pnl desc.
    Each entry: {name, total_pnl, games_played, wins, podiums, total_invested,
                 total_payout, best_win}
    """
    name_map = build_name_map(game)
    stats = {}  # canonical_name -> stats dict
    history = game.get("history", [])

    # Include active game if winners are fully recorded
    games_to_process = list(history)
    if game.get("active") and game.get("winners"):
        player_count = len(game.get("players", {}))
        payout_struct = get_payout_structure(player_count)
        if len(game["winners"]) >= len(payout_struct):
            pot = get_total_pot(game)
            active_summary = {
                "players": {
                    display_name(p): {"in": sum(p["buy_ins"]), "rebuys": len(p["buy_ins"]) - 1}
                    for p in game["players"].values()
                },
                "winners": {
                    place: {"name": w["name"], "payout": w["payout"],
                            "pct": int(w["percentage"] * 100)}
                    for place, w in game["winners"].items()
                },
                "pot": pot,
            }
            games_to_process.append(active_summary)

    # Only count games that have winners
    completed_games = [g for g in games_to_process if g.get("winners")]
    game_count = len(completed_games)

    for g in completed_games:
        players_data = g.get("players", {})
        winners_data = g.get("winners", {})

        for pname, pdata in players_data.items():
            canonical = name_map.get(pname, pname)
            total_in = pdata.get("in", 0)
            payout = 0.0
            for w in winners_data.values():
                w_canonical = name_map.get(w["name"], w["name"])
                if w_canonical == canonical:
                    payout = w["payout"]
                    break
            net = payout - total_in

            if canonical not in stats:
                stats[canonical] = {
                    "name": canonical,
                    "total_pnl": 0.0,
                    "games_played": 0,
                    "wins": 0,
                    "podiums": 0,
                    "total_invested": 0.0,
                    "total_payout": 0.0,
                    "best_win": 0.0,
                }

            s = stats[canonical]
            s["total_pnl"] += net
            s["games_played"] += 1
            s["total_invested"] += total_in
            s["total_payout"] += payout
            if net > s["best_win"]:
                s["best_win"] = net

            # Check placements
            for place_str, w in winners_data.items():
                w_canonical = name_map.get(w["name"], w["name"])
                if w_canonical == canonical:
                    if place_str == "1":
                        s["wins"] += 1
                    try:
                        if int(place_str) <= 3:
                            s["podiums"] += 1
                    except ValueError:
                        pass

    stats_list = sorted(stats.values(), key=lambda x: -x["total_pnl"])
    return stats_list, game_count


def format_leaderboard(stats: list, game_count: int) -> str:
    """Format the leaderboard output message."""
    medal = {1: "\U0001f947", 2: "\U0001f948", 3: "\U0001f949"}

    text = (
        "\U0001f3c6 ALL-TIME LEADERBOARD\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
        f"\U0001f3b0 {game_count} game{'s' if game_count != 1 else ''} played\n\n"
        "\U0001f4c8 RANKINGS (by P&L)\n"
    )

    biggest_win_name = ""
    biggest_win_amount = 0.0
    most_games_name = ""
    most_games_count = 0

    for rank, s in enumerate(stats, 1):
        name = s["name"]
        pnl = s["total_pnl"]
        gp = s["games_played"]
        wins = s["wins"]
        pods = s["podiums"]
        invested = s["total_invested"]
        roi = int((pnl / invested) * 100) if invested > 0 else 0

        if pnl > 0.01:
            icon = "\U0001f7e2"
            sign = "+"
        elif pnl < -0.01:
            icon = "\U0001f534"
            sign = "-"
        else:
            icon = "\u26aa"
            sign = " "

        if rank <= 3:
            prefix = f"  {medal[rank]} {icon}"
        else:
            prefix = f"  {rank}. {icon}"

        text += f"{prefix} {name:<14} {sign}${abs(pnl):.2f}\n"
        text += f"      GP:{gp} W:{wins} Pod:{pods} ROI:{roi:+d}%\n"

        # Track superlatives
        if s["best_win"] > biggest_win_amount:
            biggest_win_amount = s["best_win"]
            biggest_win_name = name
        if gp > most_games_count:
            most_games_count = gp
            most_games_name = name

    # Footer
    text += "\n"
    if biggest_win_name and biggest_win_amount > 0:
        text += f"\U0001f4a5 Biggest Win: {biggest_win_name} +${biggest_win_amount:.2f}\n"
    if most_games_name:
        text += f"\U0001f3b2 Most Games: {most_games_name} ({most_games_count} game{'s' if most_games_count != 1 else ''})\n"

    return text


def build_pnl_grid(game: dict):
    """Build a player x game P&L matrix from history.

    Returns (game_labels, player_rows) where:
      game_labels: list of short game names (e.g. "G1 Feb19")
      player_rows: list of (name, [pnl_per_game_or_None], total) sorted by total desc
    """
    name_map = build_name_map(game)

    games_to_process = []
    history = game.get("history", [])
    for h in history:
        if h.get("winners"):
            games_to_process.append(h)

    # Include active game if winners are fully recorded
    if game.get("active") and game.get("winners"):
        player_count = len(game.get("players", {}))
        payout_struct = get_payout_structure(player_count)
        if len(game["winners"]) >= len(payout_struct):
            pot = get_total_pot(game)
            active_summary = {
                "date": game.get("started_at"),
                "game_name": game.get("game_name") or "Current Game",
                "pot": pot,
                "players": {
                    display_name(p): {"in": sum(p["buy_ins"]), "rebuys": len(p["buy_ins"]) - 1}
                    for p in game["players"].values()
                },
                "winners": {
                    place: {"name": w["name"], "payout": w["payout"],
                            "pct": int(w["percentage"] * 100)}
                    for place, w in game["winners"].items()
                },
            }
            games_to_process.append(active_summary)

    if not games_to_process:
        return [], []

    # Build short game labels
    game_labels = []
    for i, g in enumerate(games_to_process, 1):
        date_str = g.get("date", "")[:10] if g.get("date") else ""
        if date_str:
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                label = f"G{i} {dt.strftime('%b%d')}"
            except ValueError:
                label = f"G{i}"
        else:
            label = f"G{i}"
        game_labels.append(label)

    # Collect per-player per-game P&L and rebuys
    all_players = {}   # canonical_name -> [pnl_or_None per game]
    all_rebuys = {}    # canonical_name -> [rebuys_or_None per game]
    for gi, g in enumerate(games_to_process):
        players_data = g.get("players", {})
        winners_data = g.get("winners", {})
        for pname, pdata in players_data.items():
            canonical = name_map.get(pname, pname)
            if canonical not in all_players:
                all_players[canonical] = [None] * len(games_to_process)
                all_rebuys[canonical] = [None] * len(games_to_process)
            total_in = pdata.get("in", 0)
            payout = 0.0
            for w in winners_data.values():
                w_canonical = name_map.get(w["name"], w["name"])
                if w_canonical == canonical:
                    payout = w["payout"]
                    break
            all_players[canonical][gi] = payout - total_in
            all_rebuys[canonical][gi] = pdata.get("rebuys", 0)

    # Build rows sorted by total desc
    player_rows = []
    for name, pnls in all_players.items():
        total = sum(v for v in pnls if v is not None)
        rebuys = all_rebuys[name]
        player_rows.append((name, pnls, total, rebuys))
    player_rows.sort(key=lambda x: -x[2])

    return game_labels, player_rows


def build_winners_grid(game: dict):
    """Build a winners-per-game matrix from history.

    Returns (game_labels, place_rows) where:
      game_labels: list of short game names
      place_rows: list of (place_label, [(name, net_pnl) or None per game])
      net_pnl = payout - total_invested (accounts for rebuys)
    """
    name_map = build_name_map(game)

    games_to_process = []
    history = game.get("history", [])
    for h in history:
        if h.get("winners"):
            games_to_process.append(h)

    if game.get("active") and game.get("winners"):
        player_count = len(game.get("players", {}))
        payout_struct = get_payout_structure(player_count)
        if len(game["winners"]) >= len(payout_struct):
            pot = get_total_pot(game)
            active_summary = {
                "date": game.get("started_at"),
                "game_name": game.get("game_name") or "Current Game",
                "pot": pot,
                "players": {
                    display_name(p): {"in": sum(p["buy_ins"]), "rebuys": len(p["buy_ins"]) - 1}
                    for p in game["players"].values()
                },
                "winners": {
                    place: {"name": w["name"], "payout": w["payout"],
                            "pct": int(w["percentage"] * 100)}
                    for place, w in game["winners"].items()
                },
            }
            games_to_process.append(active_summary)

    if not games_to_process:
        return [], []

    game_labels = []
    for i, g in enumerate(games_to_process, 1):
        date_str = g.get("date", "")[:10] if g.get("date") else ""
        if date_str:
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                label = f"G{i} {dt.strftime('%b%d')}"
            except ValueError:
                label = f"G{i}"
        else:
            label = f"G{i}"
        game_labels.append(label)

    # Find max places across all games
    max_places = 0
    for g in games_to_process:
        for place_str in g.get("winners", {}):
            try:
                max_places = max(max_places, int(place_str))
            except ValueError:
                pass

    place_labels_map = {1: "1st", 2: "2nd", 3: "3rd"}
    place_rows = []
    for place in range(1, max_places + 1):
        label = place_labels_map.get(place, f"{place}th")
        row = []
        for g in games_to_process:
            w = g.get("winners", {}).get(str(place))
            if w:
                canonical = name_map.get(w["name"], w["name"])
                # Calculate net P&L: payout - total invested
                total_in = 0
                players_data = g.get("players", {})
                for pname, pdata in players_data.items():
                    p_canonical = name_map.get(pname, pname)
                    if p_canonical == canonical:
                        total_in = pdata.get("in", 0)
                        break
                net = w["payout"] - total_in
                row.append((canonical, net))
            else:
                row.append(None)
        place_rows.append((label, row))

    return game_labels, place_rows


def generate_winners_grid_image(game_labels, place_rows):
    """Render the winners grid (games as rows, places as columns). Returns BytesIO."""
    bg_color = (25, 25, 35)
    header_bg = (40, 40, 58)
    row_even = (35, 35, 48)
    row_odd = (40, 40, 55)
    border_color = (60, 60, 80)
    text_header = (180, 180, 200)
    text_white = (220, 220, 230)
    text_gray = (90, 90, 110)
    text_green = (80, 220, 100)
    text_red = (240, 80, 80)

    # Column styles per place
    col_styles = [
        {"name": (255, 215, 50), "header_bg": (55, 50, 20)},    # gold
        {"name": (190, 200, 220), "header_bg": (45, 47, 58)},   # silver
        {"name": (220, 160, 80), "header_bg": (50, 40, 25)},    # bronze
    ]
    default_col = {"name": (200, 200, 210), "header_bg": header_bg}

    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 14)
        font_bold = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 14)
        font_header = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 13)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 11)
    except (OSError, IOError):
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 14)
            font_bold = font
            font_header = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 13)
            font_small = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 11)
        except (OSError, IOError):
            font = ImageFont.load_default()
            font_bold = font
            font_header = font
            font_small = font

    padding = 10
    cell_h = 44  # taller for name + net P&L
    game_label_w = 100
    place_col_w = 130

    num_places = len(place_rows)
    num_games = len(game_labels)

    tmp_img = Image.new("RGB", (1, 1))
    tmp_draw = ImageDraw.Draw(tmp_img)

    # Measure game label column
    for label in game_labels:
        bbox = tmp_draw.textbbox((0, 0), label, font=font_bold)
        game_label_w = max(game_label_w, bbox[2] - bbox[0] + 20)

    # Measure place column widths based on winner names
    for _, row in place_rows:
        for cell in row:
            if cell:
                name, net = cell
                bbox = tmp_draw.textbbox((0, 0), name, font=font_bold)
                place_col_w = max(place_col_w, bbox[2] - bbox[0] + 24)

    img_w = padding + game_label_w + num_places * place_col_w + padding
    img_h = padding + cell_h + num_games * cell_h + padding

    img = Image.new("RGB", (img_w, img_h), bg_color)
    draw = ImageDraw.Draw(img)

    # ── Header row: GAME | 1st | 2nd | 3rd ──
    y = padding
    draw.rectangle([0, y, padding + game_label_w, y + cell_h], fill=header_bg)
    x = padding
    draw.text((x + 4, y + 13), "GAME", font=font_header, fill=text_header)
    x += game_label_w

    place_labels = {0: "\U0001f947 1st", 1: "\U0001f948 2nd", 2: "\U0001f949 3rd"}
    for pi, (place_label, _) in enumerate(place_rows):
        style = col_styles[pi] if pi < len(col_styles) else default_col
        draw.rectangle([x, y, x + place_col_w, y + cell_h], fill=style["header_bg"])
        hdr_text = place_labels.get(pi, place_label)
        bbox = draw.textbbox((0, 0), hdr_text, font=font_header)
        tw = bbox[2] - bbox[0]
        draw.text((x + (place_col_w - tw) // 2, y + 13), hdr_text, font=font_header, fill=style["name"])
        x += place_col_w

    draw.line([0, y + cell_h, img_w, y + cell_h], fill=border_color, width=2)

    # ── Game rows ──
    for gi, label in enumerate(game_labels):
        y = padding + cell_h + gi * cell_h
        row_bg = row_even if gi % 2 == 0 else row_odd
        draw.rectangle([0, y, img_w, y + cell_h], fill=row_bg)

        x = padding
        draw.text((x + 4, y + 13), label, font=font_bold, fill=text_white)
        x += game_label_w

        for pi, (_, row) in enumerate(place_rows):
            style = col_styles[pi] if pi < len(col_styles) else default_col
            cell = row[gi] if gi < len(row) else None

            if cell is None:
                txt = "---"
                bbox = draw.textbbox((0, 0), txt, font=font)
                tw = bbox[2] - bbox[0]
                draw.text((x + (place_col_w - tw) // 2, y + 13), txt, font=font, fill=text_gray)
            else:
                name, net = cell
                # Winner name in place color
                bbox = draw.textbbox((0, 0), name, font=font_bold)
                tw = bbox[2] - bbox[0]
                draw.text((x + (place_col_w - tw) // 2, y + 5), name, font=font_bold, fill=style["name"])

                # Net P&L below
                if net > 0.01:
                    net_txt = f"+${net:.0f}"
                    net_color = text_green
                    pill_bg = (25, 60, 30)
                elif net < -0.01:
                    net_txt = f"-${abs(net):.0f}"
                    net_color = text_red
                    pill_bg = (60, 25, 25)
                else:
                    net_txt = "$0"
                    net_color = text_gray
                    pill_bg = None
                bbox = draw.textbbox((0, 0), net_txt, font=font_small)
                tw = bbox[2] - bbox[0]
                tx = x + (place_col_w - tw) // 2
                if pill_bg:
                    draw.rounded_rectangle(
                        [tx - 4, y + 24, tx + tw + 4, y + cell_h - 4],
                        radius=3, fill=pill_bg,
                    )
                draw.text((tx, y + 25), net_txt, font=font_small, fill=net_color)
            x += place_col_w

        draw.line([0, y + cell_h, img_w, y + cell_h], fill=border_color, width=1)

    # ── Vertical separators ──
    x = padding + game_label_w
    draw.line([x, padding, x, img_h - padding], fill=border_color, width=1)
    for pi in range(num_places):
        x += place_col_w
        draw.line([x, padding, x, img_h - padding], fill=border_color, width=1)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def generate_pnl_grid_image(game_labels, player_rows):
    """Render the P&L grid as a dark-themed PNG image (games as rows, players as columns).
    Returns a BytesIO buffer."""
    # Theme colors
    bg_color = (30, 30, 40)
    header_bg = (45, 45, 60)
    total_row_bg = (50, 50, 65)
    row_even = (35, 35, 48)
    row_odd = (40, 40, 55)
    border_color = (70, 70, 90)
    text_white = (220, 220, 230)
    text_green = (80, 220, 100)
    text_red = (240, 80, 80)
    text_gray = (100, 100, 120)
    text_header = (180, 180, 200)

    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 14)
        font_bold = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 14)
        font_header = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 13)
    except (OSError, IOError):
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 14)
            font_bold = font
            font_header = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 13)
        except (OSError, IOError):
            font = ImageFont.load_default()
            font_bold = font
            font_header = font

    padding = 10
    cell_h = 28
    game_col_w = 100  # left column for game labels
    player_col_w = 100  # each player column

    num_players = len(player_rows)
    num_games = len(game_labels)

    tmp_img = Image.new("RGB", (1, 1))
    tmp_draw = ImageDraw.Draw(tmp_img)

    # Measure game label column width
    for label in game_labels:
        bbox = tmp_draw.textbbox((0, 0), label, font=font_bold)
        game_col_w = max(game_col_w, bbox[2] - bbox[0] + 20)
    bbox = tmp_draw.textbbox((0, 0), "TOTAL", font=font_bold)
    game_col_w = max(game_col_w, bbox[2] - bbox[0] + 20)

    # Measure player column widths (fit names and values like "+$400 (R3)")
    for name, pnls, _, rebuys in player_rows:
        bbox = tmp_draw.textbbox((0, 0), name, font=font_header)
        player_col_w = max(player_col_w, bbox[2] - bbox[0] + 20)
        for pnl, rb in zip(pnls, rebuys):
            if pnl is None:
                continue
            rb_count = rb if rb is not None else 0
            if pnl > 0.01:
                sample = f"+${pnl:.0f}"
            elif pnl < -0.01:
                sample = f"-${abs(pnl):.0f}"
            else:
                sample = "$0"
            if rb_count > 0:
                sample += f" (R{rb_count})"
            bbox = tmp_draw.textbbox((0, 0), sample, font=font)
            player_col_w = max(player_col_w, bbox[2] - bbox[0] + 16)

    # +1 row for TOTAL at the bottom
    img_w = padding + game_col_w + num_players * player_col_w + padding
    img_h = padding + cell_h + num_games * cell_h + cell_h + padding  # header + game rows + total row

    img = Image.new("RGB", (img_w, img_h), bg_color)
    draw = ImageDraw.Draw(img)

    # ── Header row: GAME | Player1 | Player2 | ... ──
    y = padding
    draw.rectangle([0, y, img_w, y + cell_h], fill=header_bg)
    x = padding
    draw.text((x + 4, y + 6), "GAME", font=font_header, fill=text_header)
    x += game_col_w

    for name, _, _, _ in player_rows:
        bbox = draw.textbbox((0, 0), name, font=font_header)
        tw = bbox[2] - bbox[0]
        draw.text((x + (player_col_w - tw) // 2, y + 6), name, font=font_header, fill=text_header)
        x += player_col_w

    draw.line([0, y + cell_h, img_w, y + cell_h], fill=border_color, width=2)

    # ── Game rows ──
    for gi, label in enumerate(game_labels):
        y = padding + cell_h + gi * cell_h
        row_bg = row_even if gi % 2 == 0 else row_odd
        draw.rectangle([0, y, img_w, y + cell_h], fill=row_bg)

        x = padding
        draw.text((x + 4, y + 6), label, font=font_bold, fill=text_white)
        x += game_col_w

        for name, pnls, _, rebuys in player_rows:
            pnl = pnls[gi]
            rb = rebuys[gi] if rebuys[gi] is not None else 0
            if pnl is None:
                txt = "---"
                color = text_gray
            elif pnl > 0.01:
                txt = f"+${pnl:.0f}"
                if rb > 0:
                    txt += f" (R{rb})"
                color = text_green
            elif pnl < -0.01:
                txt = f"-${abs(pnl):.0f}"
                if rb > 0:
                    txt += f" (R{rb})"
                color = text_red
            else:
                txt = "$0"
                if rb > 0:
                    txt += f" (R{rb})"
                color = text_gray
            bbox = draw.textbbox((0, 0), txt, font=font)
            tw = bbox[2] - bbox[0]
            draw.text((x + (player_col_w - tw) // 2, y + 6), txt, font=font, fill=color)
            x += player_col_w

        draw.line([0, y + cell_h, img_w, y + cell_h], fill=border_color, width=1)

    # ── TOTAL row ──
    y = padding + cell_h + num_games * cell_h
    draw.rectangle([0, y, img_w, y + cell_h], fill=total_row_bg)
    draw.line([0, y, img_w, y], fill=border_color, width=2)

    x = padding
    draw.text((x + 4, y + 6), "TOTAL", font=font_bold, fill=text_header)
    x += game_col_w

    for name, _, total, _ in player_rows:
        if total > 0.01:
            total_txt = f"+${total:.0f}"
            total_color = text_green
            pill_color = (30, 70, 35)
        elif total < -0.01:
            total_txt = f"-${abs(total):.0f}"
            total_color = text_red
            pill_color = (70, 30, 30)
        else:
            total_txt = "$0"
            total_color = text_gray
            pill_color = None

        bbox = draw.textbbox((0, 0), total_txt, font=font_bold)
        tw = bbox[2] - bbox[0]
        tx = x + (player_col_w - tw) // 2

        if pill_color:
            draw.rounded_rectangle(
                [tx - 6, y + 3, tx + tw + 6, y + cell_h - 3],
                radius=4, fill=pill_color,
            )
        draw.text((tx, y + 6), total_txt, font=font_bold, fill=total_color)
        x += player_col_w

    draw.line([0, y + cell_h, img_w, y + cell_h], fill=border_color, width=1)

    # ── Vertical separators ──
    x = padding + game_col_w
    draw.line([x, padding, x, img_h - padding], fill=border_color, width=1)
    for _ in player_rows:
        x += player_col_w
        draw.line([x, padding, x, img_h - padding], fill=border_color, width=1)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def format_pnl_grid_text(game_labels, player_rows):
    """Format P&L grid as copyable monospace HTML text."""
    if not game_labels or not player_rows:
        return "No completed games to display."

    # Calculate column widths
    name_w = max(len(name) for name, _, _ in player_rows)
    name_w = max(name_w, 6)  # minimum "PLAYER"
    game_w = max(max(len(l) for l in game_labels), 7)  # min width for values like "+$100"
    total_w = 7

    # Header
    header = f"{'PLAYER':<{name_w}}"
    for label in game_labels:
        header += f" {label:>{game_w}}"
    header += f" {'TOTAL':>{total_w}}"

    sep = "-" * len(header)

    lines = [header, sep]
    for name, pnls, total in player_rows:
        row = f"{name:<{name_w}}"
        for pnl in pnls:
            if pnl is None:
                cell = "---"
            elif pnl > 0.01:
                cell = f"+${pnl:.0f}"
            elif pnl < -0.01:
                cell = f"-${abs(pnl):.0f}"
            else:
                cell = "$0"
            row += f" {cell:>{game_w}}"

        if total > 0.01:
            total_str = f"+${total:.0f}"
        elif total < -0.01:
            total_str = f"-${abs(total):.0f}"
        else:
            total_str = "$0"
        row += f" {total_str:>{total_w}}"
        lines.append(row)

    return "<pre>" + "\n".join(lines) + "</pre>"


def format_settle_dashboard(game: dict) -> str:
    """Build the visual settlement dashboard."""
    pot = get_total_pot(game)
    player_count = len(game["players"])
    game_name = game.get("game_name") or "Tournament"
    place_emojis = {1: "\U0001f947", 2: "\U0001f948", 3: "\U0001f949"}

    text = (
        f"\U0001f3b0 {game_name}\n\n"
        f"\U0001f4b0 Total Prize Pool: ${pot:.2f}\n"
        f"\U0001f465 {player_count} Players  |  Buy-in: ${game['buy_in_amount']:.2f}\n\n"
    )

    # Winners section
    text += "\U0001f3c6 WINNERS\n"
    for place_str in sorted(game["winners"].keys()):
        w = game["winners"][place_str]
        emoji = place_emojis.get(int(place_str), "  ")
        pct = int(w["percentage"] * 100)
        text += f"  {emoji} {w['name']:<12} ${w['payout']:.2f} ({pct}%)\n"
    text += "\n"

    # P&L with visual bars
    pnl = build_pnl(game["winners"], game["players"])
    max_abs = max(abs(net) for _, net in pnl) if pnl else 1

    text += "\U0001f4c8 PROFIT & LOSS\n"
    for rank, (pname, net) in enumerate(pnl, 1):
        if net > 0.01:
            icon = "\U0001f7e2"  # green circle
            sign = "+"
        elif net < -0.01:
            icon = "\U0001f534"  # red circle
            sign = ""
        else:
            icon = "\u26aa"  # white circle
            sign = " "
        text += f"  {icon} {pname:<14} {sign}${net:.2f}\n"

    text += "\n"

    # Payments section
    settlements = calculate_settlements(game["winners"], game["players"])
    if settlements:
        text += "\U0001f4b8 PAYMENTS\n"
        for d_name, c_name, amount in settlements:
            text += f"  {d_name} \u27a1 {c_name}: ${amount:.2f}\n"
    else:
        text += "\U0001f4b8 Everyone broke even!\n"

    return text


# ── Keyboards ────────────────────────────────────────────────────────────────
def approval_keyboard(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Accept", callback_data=f"approve_{request_id}"),
            InlineKeyboardButton("Reject", callback_data=f"reject_{request_id}"),
        ]
    ])


def lobby_keyboard(rebuy_locked: bool) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton("Join Game", callback_data="lobby_join")]]
    if not rebuy_locked:
        buttons[0].append(InlineKeyboardButton("Rebuy", callback_data="lobby_rebuy"))
    return InlineKeyboardMarkup(buttons)


def lobby_text(game: dict) -> str:
    player_count = len(game["players"])
    pot = get_total_pot(game)
    rebuy_status = "CLOSED" if game.get("rebuy_locked") else "OPEN"
    game_name = game.get("game_name") or "Tournament"

    text = (
        f"\U0001f3b0 GAME LOBBY\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
        f"\U0001f3af {game_name}\n\n"
        f"Buy-in: ${game['buy_in_amount']:.2f}\n"
        f"Players: {player_count}\n"
        f"Total Prize Pool: ${pot:.2f}\n"
        f"Rebuys: {rebuy_status}\n"
    )

    # Show dollar amounts when rebuys are closed (pool is final)
    if game.get("rebuy_locked"):
        payout_struct = get_payout_structure(player_count)
        place_emojis = {1: "\U0001f947", 2: "\U0001f948", 3: "\U0001f949"}
        text += "\U0001f3c6 Payouts:\n"
        for place, pct in payout_struct.items():
            emoji = place_emojis.get(place, "")
            text += f"  {emoji} ${pot * pct:.2f} ({int(pct * 100)}%)\n"
    else:
        text += f"{format_payout_structure(player_count)}\n"

    text += f"\nPlayers:\n{player_summary(game)}\n\n"

    if game.get("rebuy_locked"):
        text += "Tap 'Join Game' to enter."
    else:
        text += "Tap 'Join Game' to enter or 'Rebuy' for more chips."
    return text


def winner_keyboard(players: dict, place: int, exclude_uids: list = None) -> InlineKeyboardMarkup:
    exclude_uids = exclude_uids or []
    buttons = []
    for uid, p in players.items():
        if uid not in exclude_uids:
            buttons.append(
                InlineKeyboardButton(display_name(p), callback_data=f"winner_{place}_{uid}")
            )
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


async def update_lobby(game: dict, chat_id: str, context: ContextTypes.DEFAULT_TYPE):
    if not game.get("lobby_message_id"):
        return
    # Delete old lobby and re-post at bottom so it's always current
    try:
        await context.bot.delete_message(
            chat_id=int(chat_id),
            message_id=game["lobby_message_id"],
        )
    except Exception:
        pass
    try:
        new_msg = await context.bot.send_message(
            chat_id=int(chat_id),
            text=lobby_text(game),
            reply_markup=lobby_keyboard(game.get("rebuy_locked", False)),
        )
        game["lobby_message_id"] = new_msg.message_id
        data = load_data()
        data[chat_id]["lobby_message_id"] = new_msg.message_id
        save_data(data)
    except Exception:
        pass


def host_panel_keyboard(game: dict) -> InlineKeyboardMarkup:
    if not game.get("active"):
        # Inactive state: minimal panel
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("\U0001f3ae New Game", callback_data="host_newgame")],
            [
                InlineKeyboardButton("\U0001f3af P&L Grid", callback_data="host_pnlgrid"),
                InlineKeyboardButton("\U0001f3c6 Leaderboard", callback_data="host_leaderboard"),
            ],
            [InlineKeyboardButton("\U0001f4da History", callback_data="host_history")],
        ])

    # Active state: full controls + player buttons at bottom
    rebuy_locked = game.get("rebuy_locked", False)
    if rebuy_locked:
        rebuy_btn = InlineKeyboardButton("\U0001f513 Unlock Rebuy", callback_data="host_unlockrebuy")
    else:
        rebuy_btn = InlineKeyboardButton("\U0001f512 Lock Rebuy", callback_data="host_lockrebuy")

    rows = [
        [
            InlineKeyboardButton("\U0001f4ca Status", callback_data="host_status"),
            rebuy_btn,
        ],
        [
            InlineKeyboardButton("\U0001f3c6 Winners", callback_data="host_winners"),
            InlineKeyboardButton("\U0001f4b0 Settle", callback_data="host_settle"),
        ],
        [
            InlineKeyboardButton("\U0001f3c6 Leaderboard", callback_data="host_leaderboard"),
            InlineKeyboardButton("\U0001f3c1 End Game", callback_data="host_endgame"),
        ],
        [
            InlineKeyboardButton("\U0001f3af P&L Grid", callback_data="host_pnlgrid"),
        ],
        [
            InlineKeyboardButton("\u2795 Add Player", callback_data="host_addplayer"),
            InlineKeyboardButton("\u2795 Add Rebuy", callback_data="host_addrebuy"),
        ],
    ]

    # Player buttons at the bottom (hidden when rebuys locked)
    if not rebuy_locked:
        rows.append([
            InlineKeyboardButton("Join Game", callback_data="lobby_join"),
            InlineKeyboardButton("Rebuy", callback_data="lobby_rebuy"),
        ])

    return InlineKeyboardMarkup(rows)


def host_panel_text(game: dict) -> str:
    if not game.get("active"):
        return (
            "\U0001f3ae POKER NIGHT\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
            "No active game. Start a new game or view past results."
        )

    player_count = len(game["players"])
    pot = get_total_pot(game)
    rebuy_status = "CLOSED" if game.get("rebuy_locked") else "OPEN"
    game_name = game.get("game_name") or "Tournament"
    pending_count = sum(1 for r in game.get("pending", []) if r["status"] == "pending")
    winners_recorded = len(game.get("winners", {}))
    payout_struct = get_payout_structure(player_count)
    winners_needed = len(payout_struct)

    text = (
        f"\U0001f3ae HOST CONTROLS\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
        f"\U0001f3af {game_name}\n\n"
        f"\U0001f464 Host: {game['host_name']}\n"
        f"\U0001f465 Players: {player_count}\n"
        f"\U0001f4b0 Total Prize Pool: ${pot:.2f}\n"
        f"\U0001f504 Rebuys: {rebuy_status}\n"
    )
    if pending_count:
        text += f"\u23f3 Pending requests: {pending_count}\n"
    if winners_recorded > 0:
        text += f"\U0001f3c6 Winners: {winners_recorded}/{winners_needed} recorded\n"

    # Lobby info merged in
    text += f"\nBuy-in: ${game['buy_in_amount']:.2f}\n"
    if game.get("rebuy_locked"):
        place_emojis = {1: "\U0001f947", 2: "\U0001f948", 3: "\U0001f949"}
        text += "\U0001f3c6 Payouts:\n"
        for place, pct in payout_struct.items():
            emoji = place_emojis.get(place, "")
            text += f"  {emoji} ${pot * pct:.2f} ({int(pct * 100)}%)\n"
    else:
        text += f"{format_payout_structure(player_count)}\n"

    text += f"\nPlayers:\n{player_summary(game)}\n\n"

    if game.get("rebuy_locked"):
        text += "Tap 'Join Game' to enter."
    else:
        text += "Tap 'Join Game' to enter or 'Rebuy' for more chips."

    return text


async def update_host_panel(game: dict, chat_id: str, context: ContextTypes.DEFAULT_TYPE):
    if not game.get("host_panel_message_id"):
        return

    # Only bring panel to bottom when:
    # - No active game (inactive panel — easy access to New Game)
    # - Active game with rebuys open (players need Join/Rebuy buttons)
    # Otherwise edit in place so workflow messages aren't buried.
    bring_to_bottom = not game.get("active") or not game.get("rebuy_locked", False)

    if bring_to_bottom:
        try:
            await context.bot.delete_message(
                chat_id=int(chat_id),
                message_id=game["host_panel_message_id"],
            )
        except Exception:
            pass
        try:
            new_msg = await context.bot.send_message(
                chat_id=int(chat_id),
                text=host_panel_text(game),
                reply_markup=host_panel_keyboard(game),
            )
            game["host_panel_message_id"] = new_msg.message_id
            data = load_data()
            data[chat_id]["host_panel_message_id"] = new_msg.message_id
            save_data(data)
        except Exception:
            pass
    else:
        try:
            await context.bot.edit_message_text(
                chat_id=int(chat_id),
                message_id=game["host_panel_message_id"],
                text=host_panel_text(game),
                reply_markup=host_panel_keyboard(game),
            )
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
#  COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "\U0001f0cf POKER NIGHT TOURNAMENT BOT\n"
        "Host-controlled edition\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"
        "\u2501\u2501\u2501\u2501\u2501\n\n"
        "HOST COMMANDS:\n"
        "/newgame <buy_in> [name] - Start tournament\n"
        "/status - View tournament state\n"
        "/winners - Record 1st, 2nd, (3rd) place\n"
        "/settle - Settlement dashboard\n"
        "/settleall - Combined settlement (multi-game)\n"
        "/endgame - End & archive tournament\n"
        "/history - Past tournament results\n"
        "/leaderboard - All-time player rankings\n"
        "/kick <name> - Remove a player\n"
        "/nick <name> <nickname> - Set player nickname\n"
        "/reopen - Reopen last ended tournament\n"
        "/pnlgrid - P&L grid (player x game matrix)\n"
        "/lockrebuy - Close rebuys\n"
        "/unlockrebuy - Reopen rebuys\n\n"
        "PLAYERS:\n"
        "Tap 'Join Game' or 'Rebuy' buttons in the lobby!\n"
        "/join - Request to join\n"
        "/rebuy - Request rebuy\n"
        "/mystatus - Check your position\n\n"
        "PAYOUT STRUCTURE:\n"
        "> 6 players: Top 3 paid (50% / 30% / 20%)\n"
        "<= 6 players: Top 2 paid (60% / 40%)\n\n"
        "Each group chat runs its own independent tournament.\n"
        "All player actions require host approval."
    )
    await update.message.reply_text(text)

    # Post inactive host panel if none exists and no active game
    chat_id = str(update.effective_chat.id)
    data = load_data()
    game = get_game(chat_id, data)
    if not game.get("host_panel_message_id") and not game.get("active"):
        panel_msg = await update.message.reply_text(
            host_panel_text(game),
            reply_markup=host_panel_keyboard(game),
        )
        game["host_panel_message_id"] = panel_msg.message_id
        save_data(data)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user = update.effective_user
    uid = str(user.id)
    name = user.first_name or user.username or f"Player_{uid[-4:]}"

    data = load_data()
    game = get_game(chat_id, data)

    if game["active"]:
        await update.message.reply_text("A tournament is already running! Use /endgame first.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: /newgame <buy_in> [game name]\n"
            "Example: /newgame 20\n"
            "Example: /newgame 50 Friday Night Special"
        )
        return

    try:
        buy_in = float(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid amount. Example: /newgame 20")
        return

    # Custom game name or auto-generate
    if len(context.args) > 1:
        custom_name = " ".join(context.args[1:])
    else:
        custom_name = None

    game["active"] = True
    game["host_id"] = uid
    game["host_name"] = name
    game["buy_in_amount"] = buy_in
    game["players"] = {}
    game["pending"] = []
    game["winners"] = {}
    game["rebuy_locked"] = False
    game["lobby_message_id"] = None
    game["started_at"] = datetime.now().isoformat()
    game["game_name"] = custom_name or generate_game_name(game)
    game.pop("awaiting_newgame", None)

    # Auto-add host (with persistent nickname if set, lookup by user_id)
    host_entry = {"name": name, "buy_ins": [buy_in], "eliminated": False}
    host_nick = game.get("nicknames", {}).get(uid)
    if host_nick:
        host_entry["nickname"] = host_nick
    game["players"][uid] = host_entry
    save_data(data)

    await update.message.reply_text(
        f"\U0001f3b0 TOURNAMENT STARTED!\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
        f"\U0001f3af {game['game_name']}\n\n"
        f"Host: {name}\n"
        f"Buy-in: ${buy_in:.2f}\n"
        f"{name} (host) is automatically in.\n"
    )

    # Post or refresh host panel (which now includes lobby)
    if game.get("host_panel_message_id"):
        await update_host_panel(game, chat_id, context)
    else:
        host_msg = await update.message.reply_text(
            host_panel_text(game),
            reply_markup=host_panel_keyboard(game),
        )
        game["host_panel_message_id"] = host_msg.message_id
    save_data(data)


async def kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    uid = str(update.effective_user.id)

    data = load_data()
    game = get_game(chat_id, data)

    if not game["active"]:
        await update.message.reply_text("No active tournament.")
        return
    if not is_host(game, uid):
        await update.message.reply_text("Only the host can kick players.")
        return

    if update.message.entities:
        for entity in update.message.entities:
            if entity.type == "text_mention" and entity.user:
                target_id = str(entity.user.id)
                if target_id in game["players"]:
                    removed = game["players"].pop(target_id)
                    save_data(data)
                    await update.message.reply_text(f"{removed['name']} has been removed.")
                    await update_host_panel(game, chat_id, context)
                    return

    if context.args:
        target_name = " ".join(context.args).replace("@", "").lower()
        matches = []
        for pid, p in list(game["players"].items()):
            if display_name(p).lower() == target_name or p["name"].lower() == target_name:
                matches.append((pid, p))

        if len(matches) == 1:
            pid, p = matches[0]
            removed = game["players"].pop(pid)
            save_data(data)
            await update.message.reply_text(f"{display_name(removed)} has been removed.")
            await update_host_panel(game, chat_id, context)
            return
        elif len(matches) > 1:
            names = ", ".join(f"{display_name(p)} (uid:{pid[-4:]})" for pid, p in matches)
            await update.message.reply_text(
                f"Multiple players match '{target_name}': {names}\n"
                f"Use the player's nickname instead to be specific."
            )
            return

    await update.message.reply_text("Player not found. Try: /kick PlayerNickname")


async def nick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    uid = str(update.effective_user.id)

    data = load_data()
    game = get_game(chat_id, data)

    if not is_host(game, uid) and game.get("active"):
        await update.message.reply_text("Only the host can set nicknames.")
        return

    if not context.args or len(context.args) < 2:
        # Show current nicknames
        if game["nicknames"] and game.get("active"):
            lines = []
            for pid, p in game["players"].items():
                nick = game["nicknames"].get(pid)
                if nick:
                    lines.append(f"  {p['name']} \u27a1 {nick}")
            if lines:
                await update.message.reply_text(
                    "Current nicknames:\n" + "\n".join(lines) + "\n\n"
                    "Usage: /nick <name> <nickname>"
                )
                return
        elif game["nicknames"]:
            lines = [f"  {uid} \u27a1 {nick}" for uid, nick in game["nicknames"].items()]
            await update.message.reply_text(
                "Current nicknames:\n" + "\n".join(lines) + "\n\n"
                "Usage: /nick <name> <nickname>"
            )
            return
        await update.message.reply_text(
            "No nicknames set.\n\n"
            "Usage: /nick <name> <nickname>\n"
            "Example: /nick Dilip DilipK"
        )
        return

    # Parse: last arg is nickname, everything before is the player name to find
    nickname = context.args[-1]
    search_name = " ".join(context.args[:-1])

    # Find player by name or current nickname, save by user_id
    found = False
    if game.get("active"):
        matches = []
        for pid, p in game["players"].items():
            if p["name"].lower() == search_name.lower() or display_name(p).lower() == search_name.lower():
                matches.append((pid, p))

        if len(matches) == 1:
            pid, p = matches[0]
            old_display = display_name(p)
            p["nickname"] = nickname
            game["nicknames"][pid] = nickname
            save_data(data)
            await update.message.reply_text(f"Nickname set: {old_display} \u27a1 {nickname}")
            await update_host_panel(game, chat_id, context)
            found = True
        elif len(matches) > 1:
            names = ", ".join(f"{display_name(p)} ({p['name']})" for _, p in matches)
            await update.message.reply_text(
                f"Multiple players match '{search_name}': {names}\n"
                f"Use the player's current nickname to be specific."
            )
            found = True  # Prevent fallback

    if not found:
        # No active game or player not found — try to match uid in nicknames first
        matched_uid = None
        for nuid, nnick in game.get("nicknames", {}).items():
            if nnick.lower() == search_name.lower():
                matched_uid = nuid
                break
        if matched_uid:
            game["nicknames"][matched_uid] = nickname
            save_data(data)
            await update.message.reply_text(f"Nickname updated: {search_name} \u27a1 {nickname}")
        else:
            await update.message.reply_text(
                f"Player '{search_name}' not found.\n"
                f"Use their current nickname or Telegram name."
            )


async def lockrebuy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    uid = str(update.effective_user.id)

    data = load_data()
    game = get_game(chat_id, data)

    if not game["active"]:
        await update.message.reply_text("No active tournament.")
        return
    if not is_host(game, uid):
        await update.message.reply_text("Only the host can lock rebuys.")
        return

    game["rebuy_locked"] = True
    save_data(data)
    await update.message.reply_text("Rebuys are now CLOSED.")
    await update_host_panel(game, chat_id, context)


async def unlockrebuy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    uid = str(update.effective_user.id)

    data = load_data()
    game = get_game(chat_id, data)

    if not game["active"]:
        await update.message.reply_text("No active tournament.")
        return
    if not is_host(game, uid):
        await update.message.reply_text("Only the host can unlock rebuys.")
        return

    game["rebuy_locked"] = False
    save_data(data)
    await update.message.reply_text("Rebuys are now OPEN.")
    await update_host_panel(game, chat_id, context)


async def mystatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    uid = str(update.effective_user.id)

    data = load_data()
    game = get_game(chat_id, data)

    if not game["active"]:
        await update.message.reply_text("No active tournament.")
        return
    if uid not in game["players"]:
        await update.message.reply_text("You're not in this tournament.")
        return

    p = game["players"][uid]
    total_in = sum(p["buy_ins"])
    rebuys = len(p["buy_ins"]) - 1
    player_count = len(game["players"])
    pot = get_total_pot(game)

    text = (
        f"YOUR POSITION\n\n"
        f"Name: {display_name(p)}\n"
        f"Buy-in: ${p['buy_ins'][0]:.2f}\n"
        f"Rebuys: {rebuys} (${sum(p['buy_ins'][1:]):.2f})\n"
        f"Total invested: ${total_in:.2f}\n\n"
        f"Total Prize Pool: ${pot:.2f}\n"
        f"Players: {player_count}\n"
        f"Payout: {format_payout_structure(player_count)}"
    )
    await update.message.reply_text(text)


# ── Callback Handler ─────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = str(query.message.chat.id)
    uid = str(query.from_user.id)
    user = query.from_user
    name = user.first_name or user.username or f"Player_{uid[-4:]}"

    data = load_data()
    game = get_game(chat_id, data)
    cb_data = query.data

    # ── Lobby: Join Game Button ───────────────────────────────────────────
    if cb_data == "lobby_join":
        await query.answer()

        if not game["active"]:
            await query.answer("No active tournament.", show_alert=True)
            return
        if uid in game["players"]:
            await query.answer("You're already in the game!", show_alert=True)
            return
        for req in game["pending"]:
            if req["user_id"] == uid and req["type"] == "join" and req["status"] == "pending":
                await query.answer("Your join request is already pending.", show_alert=True)
                return

        request_id = uuid.uuid4().hex[:8]
        game["pending"].append({
            "type": "join", "user_id": uid, "name": name,
            "amount": game["buy_in_amount"], "status": "pending",
            "timestamp": datetime.now().isoformat(),
            "request_id": request_id,
        })
        save_data(data)

        await query.message.reply_text(
            f"JOIN REQUEST\n\n"
            f"Player: {name}\n"
            f"Buy-in: ${game['buy_in_amount']:.2f}\n\n"
            f"{game['host_name']}, approve or reject:",
            reply_markup=approval_keyboard(request_id),
        )
        return

    # ── Lobby: Rebuy Button ───────────────────────────────────────────────
    if cb_data == "lobby_rebuy":
        await query.answer()

        if not game["active"]:
            await query.answer("No active tournament.", show_alert=True)
            return
        if game.get("rebuy_locked"):
            await query.answer("Rebuys are closed!", show_alert=True)
            return
        if uid not in game["players"]:
            await query.answer("You're not in the game. Tap 'Join Game' first!", show_alert=True)
            return

        amount = game["buy_in_amount"]
        player_name = display_name(game["players"][uid])
        request_id = uuid.uuid4().hex[:8]
        logger.info(f"REBUY REQUEST: player={player_name} uid={uid} amount={amount} req={request_id} chat={chat_id}")
        game["pending"].append({
            "type": "rebuy", "user_id": uid, "name": player_name,
            "amount": amount, "status": "pending",
            "timestamp": datetime.now().isoformat(),
            "request_id": request_id,
        })
        save_data(data)

        current_in = sum(game["players"][uid]["buy_ins"])
        await query.message.reply_text(
            f"REBUY REQUEST\n\n"
            f"Player: {player_name}\n"
            f"Rebuy: ${amount:.2f} (currently ${current_in:.2f} in)\n\n"
            f"{game['host_name']}, approve or reject:",
            reply_markup=approval_keyboard(request_id),
        )
        return

    # ── New Game Buy-in Selection ─────────────────────────────────────
    if cb_data.startswith("newgame_buyin_"):
        await query.answer()

        if game.get("active"):
            await query.edit_message_text("A tournament is already running!")
            return

        buyin_str = cb_data[len("newgame_buyin_"):]

        if buyin_str == "custom":
            game["awaiting_newgame"] = {"user_id": uid, "name": name}
            save_data(data)
            await query.edit_message_text("Type the buy-in amount (e.g. 20):")
            return

        buy_in = float(buyin_str)
        host_name = name

        game["active"] = True
        game["host_id"] = uid
        game["host_name"] = host_name
        game["buy_in_amount"] = buy_in
        game["players"] = {}
        game["pending"] = []
        game["winners"] = {}
        game["rebuy_locked"] = False
        game["lobby_message_id"] = None
        game["started_at"] = datetime.now().isoformat()
        game["game_name"] = generate_game_name(game)

        host_entry = {"name": host_name, "buy_ins": [buy_in], "eliminated": False}
        host_nick = game.get("nicknames", {}).get(uid)
        if host_nick:
            host_entry["nickname"] = host_nick
        game["players"][uid] = host_entry
        save_data(data)

        await query.edit_message_text(
            f"\U0001f3b0 TOURNAMENT STARTED!\n"
            f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
            f"\U0001f3af {game['game_name']}\n\n"
            f"Host: {display_name(host_entry)}\n"
            f"Buy-in: ${buy_in:.2f}\n"
            f"{display_name(host_entry)} (host) is automatically in.\n"
        )
        await update_host_panel(game, chat_id, context)
        return

    # ── End Game Confirmation ─────────────────────────────────────────
    if cb_data == "endgame_record_winners":
        if not is_host(game, uid):
            await query.answer("Only the host can do this.", show_alert=True)
            return
        await query.answer()

        player_count = len(game["players"])
        if player_count < 2:
            await query.edit_message_text("Need at least 2 players to record winners.")
            return

        pot = get_total_pot(game)
        payout_struct = get_payout_structure(player_count)
        places = len(payout_struct)

        already_picked = []
        start_place = 1
        for p in range(1, places + 1):
            if str(p) in game["winners"]:
                already_picked.append(game["winners"][str(p)]["user_id"])
                start_place = p + 1

        place_labels = {1: "1st", 2: "2nd", 3: "3rd"}
        place_label = place_labels.get(start_place, f"{start_place}th")

        await query.edit_message_text(
            f"\U0001f3c6 RECORD WINNERS\n"
            f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
            f"\U0001f4b0 Total Prize Pool: ${pot:.2f} | Players: {player_count}\n"
            f"{format_payout_structure(player_count)}\n\n"
            f"Select {place_label} place:",
            reply_markup=winner_keyboard(game["players"], start_place, exclude_uids=already_picked),
        )
        return

    # ── Final Hand Photo Prompt ───────────────────────────────────────
    if cb_data == "endgame_photo_upload":
        afp = game.get("awaiting_final_photo")
        if not afp or afp.get("host_id") != uid:
            await query.answer("Only the host can upload the photo.", show_alert=True)
            return
        await query.answer()
        afp["ready"] = True
        save_data(data)
        await query.edit_message_text("\U0001f4f8 Send the final hand photo now.")
        return

    if cb_data == "endgame_photo_skip":
        afp = game.get("awaiting_final_photo")
        if not afp or afp.get("host_id") != uid:
            await query.answer("Only the host can do this.", show_alert=True)
            return
        await query.answer()
        game.pop("awaiting_final_photo", None)
        save_data(data)
        await query.edit_message_text("Skipped. No photo saved.")
        await update_host_panel(game, chat_id, context)
        return

    if cb_data == "endgame_confirm":
        if not is_host(game, uid):
            await query.answer("Only the host can do this.", show_alert=True)
            return
        await query.answer()

        pot = get_total_pot(game)
        game_name = game.get("game_name") or "Tournament"
        original_host_uid = game["host_id"]

        summary = {
            "date": game["started_at"],
            "host": game["host_name"],
            "host_uid": game["host_id"],
            "buy_in": game["buy_in_amount"],
            "pot": pot,
            "game_name": game_name,
            "player_count": len(game["players"]),
            "players": {
                display_name(p): {
                    "in": sum(p["buy_ins"]),
                    "rebuys": len(p["buy_ins"]) - 1,
                }
                for p in game["players"].values()
            },
            "player_uids": {
                pid: display_name(p)
                for pid, p in game["players"].items()
            },
            "winners": {
                place: {"name": w["name"], "payout": w["payout"], "pct": int(w["percentage"] * 100)}
                for place, w in game["winners"].items()
            },
            "total_requests": len(game["pending"]),
            "approved": sum(1 for r in game["pending"] if r["status"] == "approved"),
            "rejected": sum(1 for r in game["pending"] if r["status"] == "rejected"),
        }
        game["history"].append(summary)

        if len(game["history"]) > HISTORY_LIMIT:
            game["history"] = game["history"][-HISTORY_LIMIT:]

        game["active"] = False
        game["players"] = {}
        game["pending"] = []
        game["winners"] = {}
        game["host_id"] = None
        game["host_name"] = None
        game["buy_in_amount"] = None
        game["started_at"] = None
        game["game_name"] = None
        game["rebuy_locked"] = False
        game["lobby_message_id"] = None

        game["awaiting_final_photo"] = {"host_id": original_host_uid}
        save_data(data)

        await query.edit_message_text(
            f"\U0001f3c1 TOURNAMENT OVER!\n"
            f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
            f"\U0001f3af {game_name}\n"
            f"\U0001f465 Players: {summary['player_count']} | \U0001f4b0 Prize Pool: ${pot:.2f}\n\n"
            f"\u26a0\ufe0f Ended without winners.\n"
            f"Archived. Use /reopen to add winners later."
        )

        await context.bot.send_message(
            chat_id=int(chat_id),
            text="\U0001f4f8 Upload the final hand picture?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("\U0001f4f8 Upload Photo", callback_data="endgame_photo_upload")],
                [InlineKeyboardButton("\u23ed Skip", callback_data="endgame_photo_skip")],
            ]),
        )

        # Refresh host panel to inactive state
        await update_host_panel(game, chat_id, context)
        return

    # ── Host Panel Buttons ──────────────────────────────────────────────
    if cb_data.startswith("host_"):
        # These buttons are available to everyone
        public_buttons = {"host_newgame", "host_pnlgrid", "host_history", "host_leaderboard"}

        # Host-only buttons during active game
        if cb_data not in public_buttons and not is_host(game, uid):
            await query.answer("Only the host can use these controls.", show_alert=True)
            return

        await query.answer()

        if cb_data == "host_newgame":
            if game.get("active"):
                await query.message.reply_text("A tournament is already running!")
                return
            await query.message.reply_text(
                "Select buy-in amount:",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("$50", callback_data="newgame_buyin_50"),
                        InlineKeyboardButton("$100", callback_data="newgame_buyin_100"),
                    ],
                    [
                        InlineKeyboardButton("$200", callback_data="newgame_buyin_200"),
                        InlineKeyboardButton("$300", callback_data="newgame_buyin_300"),
                    ],
                    [InlineKeyboardButton("Custom Amount", callback_data="newgame_buyin_custom")],
                ]),
            )
            return

        if cb_data == "host_history":
            if not game.get("history"):
                await query.message.reply_text("No past tournaments yet.")
                return
            page_size = 10
            total = len(game["history"])
            total_pages = (total + page_size - 1) // page_size
            page = total_pages
            start_idx = (page - 1) * page_size
            end_idx = min(start_idx + page_size, total)
            page_games = game["history"][start_idx:end_idx]

            place_emojis_h = {"1": "\U0001f947", "2": "\U0001f948", "3": "\U0001f949"}
            hist_text = (
                f"\U0001f4da TOURNAMENT HISTORY\n"
                f"Page {page}/{total_pages} ({total} games)\n"
                f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"
                f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
            )
            for i, g in enumerate(page_games, start_idx + 1):
                gname = g.get("game_name") or f"Game #{i}"
                hist_text += (
                    f"\U0001f3af {gname}\n"
                    f"  Host: {g.get('host', 'N/A')} | "
                    f"Buy-in: ${g.get('buy_in', 0):.2f}\n"
                    f"  \U0001f465 {g.get('player_count', '?')} players | "
                    f"\U0001f4b0 ${g.get('pot', 0):.2f}\n"
                )
                if g.get("winners"):
                    for place, w in sorted(g["winners"].items()):
                        emoji = place_emojis_h.get(str(place), "  ")
                        hist_text += f"  {emoji} {w['name']} - ${w['payout']:.2f} ({w['pct']}%)\n"
                if g.get("final_hand_photo"):
                    hist_text += "  \U0001f4f8 Final hand photo attached\n"
                hist_text += "\n"
            if total_pages > 1:
                hist_text += f"Use /history <page> to navigate (1-{total_pages})"
            await query.message.reply_text(hist_text)
            # Send final hand photos for games on this page
            for i, g in enumerate(page_games, start_idx + 1):
                if g.get("final_hand_photo"):
                    gname = g.get("game_name") or f"Game #{i}"
                    await context.bot.send_photo(
                        chat_id=int(chat_id),
                        photo=g["final_hand_photo"],
                        caption=f"\U0001f4f8 Final hand - {gname}",
                    )
            await update_host_panel(game, chat_id, context)
            return

        if cb_data == "host_status":
            player_count = len(game["players"])
            pot = get_total_pot(game)
            pending_count = sum(1 for r in game["pending"] if r["status"] == "pending")
            payout_struct = get_payout_structure(player_count)
            rebuy_status = "CLOSED" if game.get("rebuy_locked") else "OPEN"
            game_name = game.get("game_name") or "Tournament"

            text = (
                f"\U0001f4ca TOURNAMENT STATUS\n"
                f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
                f"\U0001f3af {game_name}\n\n"
                f"\U0001f464 Host: {game['host_name']}\n"
                f"\U0001f4b5 Buy-in: ${game['buy_in_amount']:.2f}\n"
                f"\U0001f552 Started: {game['started_at'][:16].replace('T', ' ')}\n"
                f"\U0001f465 Players: {player_count}\n"
                f"\U0001f4b0 Total Prize Pool: ${pot:.2f}\n"
                f"\U0001f504 Rebuys: {rebuy_status}\n"
            )
            if pending_count:
                text += f"\u23f3 Pending requests: {pending_count}\n"

            text += f"\n\U0001f3c6 {format_payout_structure(player_count)}\n"
            text += "\n\U0001f4b8 Prize Pool Breakdown:\n"
            place_emojis_s = {1: "\U0001f947", 2: "\U0001f948", 3: "\U0001f949"}
            for place, pct in payout_struct.items():
                amount = pot * pct
                emoji = place_emojis_s.get(place, "  ")
                text += f"  {emoji} ${amount:.2f} ({int(pct * 100)}%)\n"

            text += f"\n\U0001f465 Players:\n{player_summary(game)}"

            if game["winners"]:
                text += "\n\n\U0001f3c6 Winners:\n"
                for place_str in sorted(game["winners"].keys()):
                    w = game["winners"][place_str]
                    emoji = place_emojis_s.get(int(place_str), "  ")
                    text += f"  {emoji} {w['name']} - ${w['payout']:.2f}\n"

            await query.message.reply_text(text)
            await update_host_panel(game, chat_id, context)
            return

        if cb_data == "host_lockrebuy":
            game["rebuy_locked"] = True
            save_data(data)
            await query.message.reply_text("\U0001f512 Rebuys are now CLOSED.")
            await update_host_panel(game, chat_id, context)
            return

        if cb_data == "host_unlockrebuy":
            game["rebuy_locked"] = False
            save_data(data)
            await query.message.reply_text("\U0001f513 Rebuys are now OPEN.")
            await update_host_panel(game, chat_id, context)
            return

        if cb_data == "host_winners":
            player_count = len(game["players"])
            if player_count < 2:
                await query.message.reply_text("Need at least 2 players to record winners.")
                return

            pot = get_total_pot(game)
            payout_struct = get_payout_structure(player_count)
            places = len(payout_struct)

            already_picked = []
            start_place = 1
            for p in range(1, places + 1):
                if str(p) in game["winners"]:
                    already_picked.append(game["winners"][str(p)]["user_id"])
                    start_place = p + 1

            if start_place > places:
                await query.message.reply_text(
                    "Winners already recorded! Use \U0001f4b0 Settle to see the dashboard, or \U0001f3c1 End Game to finish."
                )
                return

            place_labels = {1: "1st", 2: "2nd", 3: "3rd"}
            place_label = place_labels.get(start_place, f"{start_place}th")

            await query.message.reply_text(
                f"\U0001f3c6 RECORD WINNERS\n"
                f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
                f"\U0001f4b0 Total Prize Pool: ${pot:.2f} | Players: {player_count}\n"
                f"{format_payout_structure(player_count)}\n\n"
                f"Select {place_label} place:",
                reply_markup=winner_keyboard(game["players"], start_place, exclude_uids=already_picked),
            )
            await update_host_panel(game, chat_id, context)
            return

        if cb_data == "host_settle":
            player_count = len(game["players"])
            payout_struct = get_payout_structure(player_count)
            required_places = len(payout_struct)

            if len(game["winners"]) < required_places:
                await query.message.reply_text(
                    f"Record all winners first! Need {required_places} place(s).\n"
                    f"Use \U0001f3c6 Winners to record."
                )
                return

            await query.message.reply_text(format_settle_dashboard(game))
            await update_host_panel(game, chat_id, context)
            return

        if cb_data == "host_leaderboard":
            history = game.get("history", [])
            completed = [g for g in history if g.get("winners")]
            has_active = False
            if game.get("active") and game.get("winners"):
                pc = len(game.get("players", {}))
                ps = get_payout_structure(pc)
                if len(game["winners"]) >= len(ps):
                    has_active = True
            if not completed and not has_active:
                await query.message.reply_text("No completed games yet. Play some poker first!")
                return
            stats, game_count = build_leaderboard_stats(game)
            if not stats:
                await query.message.reply_text("No stats to show yet.")
                return
            await query.message.reply_text(format_leaderboard(stats, game_count))
            await update_host_panel(game, chat_id, context)
            return

        if cb_data == "host_pnlgrid":
            game_labels, player_rows = build_pnl_grid(game)
            if not game_labels:
                await query.message.reply_text("No completed games yet. Play some poker first!")
                return
            wl, wr = build_winners_grid(game)
            if wl and wr:
                winners_buf = generate_winners_grid_image(wl, wr)
                await context.bot.send_photo(chat_id=int(chat_id), photo=winners_buf)
            img_buf = generate_pnl_grid_image(game_labels, player_rows)
            await context.bot.send_photo(chat_id=int(chat_id), photo=img_buf)
            await update_host_panel(game, chat_id, context)
            return

        if cb_data == "host_addplayer":
            if not game.get("active"):
                await query.message.reply_text("No active game.")
                return
            nicknames = game.get("nicknames", {})
            available = [
                (pid, nick) for pid, nick in nicknames.items()
                if pid not in game["players"]
            ]
            if not available:
                await query.message.reply_text("No known players available to add. All nicknamed players are already in the game.")
                return
            buttons = [
                [InlineKeyboardButton(nick, callback_data=f"hadp_{pid}")]
                for pid, nick in sorted(available, key=lambda x: x[1].lower())
            ]
            await query.message.reply_text(
                "Select a player to add:",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            await update_host_panel(game, chat_id, context)
            return

        if cb_data == "host_addrebuy":
            if not game.get("active"):
                await query.message.reply_text("No active game.")
                return
            if not game["players"]:
                await query.message.reply_text("No players in the game yet.")
                return
            buttons = [
                [InlineKeyboardButton(display_name(p), callback_data=f"hadr_{pid}")]
                for pid, p in game["players"].items()
            ]
            await query.message.reply_text(
                "Select a player to add rebuy for:",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            await update_host_panel(game, chat_id, context)
            return

        if cb_data == "host_endgame":
            # Check if winners are recorded
            player_count = len(game["players"])
            payout_struct = get_payout_structure(player_count)
            required_places = len(payout_struct)

            if len(game["winners"]) < required_places:
                await query.message.reply_text(
                    f"\u26a0\ufe0f WINNERS NOT RECORDED!\n\n"
                    f"You have {len(game['winners'])}/{required_places} winners recorded.\n"
                    f"Without winners, this game can't be included in /settleall.\n\n"
                    f"What would you like to do?",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("\U0001f3c6 Record Winners First", callback_data="endgame_record_winners")],
                        [InlineKeyboardButton("\U0001f3c1 End Without Winners", callback_data="endgame_confirm")],
                    ]),
                )
                await update_host_panel(game, chat_id, context)
                return

            pot = get_total_pot(game)
            game_name = game.get("game_name") or "Tournament"
            place_emojis_e = {"1": "\U0001f947", "2": "\U0001f948", "3": "\U0001f949"}
            original_host_uid = game["host_id"]

            summary = {
                "date": game["started_at"],
                "host": game["host_name"],
                "host_uid": game["host_id"],
                "buy_in": game["buy_in_amount"],
                "pot": pot,
                "game_name": game_name,
                "player_count": len(game["players"]),
                "players": {
                    display_name(p): {
                        "in": sum(p["buy_ins"]),
                        "rebuys": len(p["buy_ins"]) - 1,
                    }
                    for p in game["players"].values()
                },
                "player_uids": {
                    pid: display_name(p)
                    for pid, p in game["players"].items()
                },
                "winners": {
                    place: {"name": w["name"], "payout": w["payout"], "pct": int(w["percentage"] * 100)}
                    for place, w in game["winners"].items()
                },
                "total_requests": len(game["pending"]),
                "approved": sum(1 for r in game["pending"] if r["status"] == "approved"),
                "rejected": sum(1 for r in game["pending"] if r["status"] == "rejected"),
            }
            game["history"].append(summary)

            if len(game["history"]) > HISTORY_LIMIT:
                game["history"] = game["history"][-HISTORY_LIMIT:]

            game["active"] = False
            game["players"] = {}
            game["pending"] = []
            game["winners"] = {}
            game["host_id"] = None
            game["host_name"] = None
            game["buy_in_amount"] = None
            game["started_at"] = None
            game["game_name"] = None
            game["rebuy_locked"] = False
            game["lobby_message_id"] = None

            game["awaiting_final_photo"] = {"host_id": original_host_uid}
            save_data(data)

            winner_lines = ""
            for place, w in sorted(summary["winners"].items()):
                emoji = place_emojis_e.get(str(place), "  ")
                winner_lines += f"  {emoji} {w['name']} - ${w['payout']:.2f}\n"

            await query.message.reply_text(
                f"\U0001f3c1 TOURNAMENT OVER!\n"
                f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
                f"\U0001f3af {game_name}\n"
                f"\U0001f465 Players: {summary['player_count']} | \U0001f4b0 Prize Pool: ${pot:.2f}\n\n"
                f"\U0001f3c6 Winners:\n{winner_lines}\n"
                f"Archived. Use /history to review."
            )

            await context.bot.send_message(
                chat_id=int(chat_id),
                text="\U0001f4f8 Upload the final hand picture?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("\U0001f4f8 Upload Photo", callback_data="endgame_photo_upload")],
                    [InlineKeyboardButton("\u23ed Skip", callback_data="endgame_photo_skip")],
                ]),
            )

            # Refresh host panel to inactive state
            await update_host_panel(game, chat_id, context)
            return

        return

    # ── Host Add Player on Behalf ─────────────────────────────────────────
    if cb_data.startswith("hadp_"):
        if not is_host(game, uid):
            await query.answer("Only the host can do this.", show_alert=True)
            return
        await query.answer()

        target_uid = cb_data[5:]
        nicknames = game.get("nicknames", {})
        nick = nicknames.get(target_uid)
        if not nick:
            await query.edit_message_text("Player not found in nicknames.")
            return
        if target_uid in game["players"]:
            await query.edit_message_text(f"{nick} is already in the game!")
            return
        if not game.get("active"):
            await query.edit_message_text("No active game.")
            return

        amount = game["buy_in_amount"]
        request_id = uuid.uuid4().hex[:8]
        logger.info(f"HOST ADD PLAYER: player={nick} target_uid={target_uid} amount={amount} host_uid={uid} req={request_id} chat={chat_id}")
        game["players"][target_uid] = {
            "name": nick, "buy_ins": [amount], "eliminated": False, "nickname": nick,
        }
        game["pending"].append({
            "type": "join", "user_id": target_uid, "name": nick,
            "amount": amount, "status": "approved",
            "timestamp": datetime.now().isoformat(),
            "request_id": request_id,
        })
        save_data(data)

        total_players = len(game["players"])
        pot = get_total_pot(game)
        await query.edit_message_text(
            f"\u2705 HOST ADDED - {nick} joins!\n"
            f"Buy-in: ${amount:.2f}\n"
            f"Total Prize Pool: ${pot:.2f} | Players: {total_players}\n"
            f"Payout: {format_payout_structure(total_players)}"
        )
        await update_host_panel(game, chat_id, context)
        return

    # ── Host Add Rebuy on Behalf - Confirmation ────────────────────────────
    if cb_data.startswith("hadr_"):
        if not is_host(game, uid):
            await query.answer("Only the host can do this.", show_alert=True)
            return
        await query.answer()

        target_uid = cb_data[5:]
        if target_uid not in game["players"]:
            await query.edit_message_text("Player not found in the game.")
            return
        if not game.get("active"):
            await query.edit_message_text("No active game.")
            return

        player = game["players"][target_uid]
        player_name = display_name(player)
        amount = game["buy_in_amount"]
        current_in = sum(player["buy_ins"])
        rebuys = len(player["buy_ins"]) - 1

        await query.edit_message_text(
            f"Confirm rebuy for {player_name}?\n"
            f"Amount: ${amount:.2f}\n"
            f"Currently in: ${current_in:.2f} ({rebuys} rebuy{'s' if rebuys != 1 else ''})",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"Confirm {player_name} Rebuy", callback_data=f"hadrc_{target_uid}")],
                [InlineKeyboardButton("Cancel", callback_data="hadr_cancel")],
            ]),
        )
        return

    # ── Host Add Rebuy - Confirmed ───────────────────────────────────────────
    if cb_data.startswith("hadrc_"):
        if not is_host(game, uid):
            await query.answer("Only the host can do this.", show_alert=True)
            return
        await query.answer()

        target_uid = cb_data[6:]
        if target_uid not in game["players"]:
            await query.edit_message_text("Player not found in the game.")
            return
        if not game.get("active"):
            await query.edit_message_text("No active game.")
            return

        amount = game["buy_in_amount"]
        player = game["players"][target_uid]
        player_name = display_name(player)
        player["buy_ins"].append(amount)
        player["eliminated"] = False

        total_in = sum(player["buy_ins"])
        rebuys = len(player["buy_ins"]) - 1
        pot = get_total_pot(game)

        request_id = uuid.uuid4().hex[:8]
        logger.info(f"HOST ADD REBUY: player={player_name} target_uid={target_uid} amount={amount} host_uid={uid} req={request_id} chat={chat_id}")
        game["pending"].append({
            "type": "rebuy", "user_id": target_uid, "name": player_name,
            "amount": amount, "status": "approved",
            "timestamp": datetime.now().isoformat(),
            "request_id": request_id,
        })
        save_data(data)

        await query.edit_message_text(
            f"\u2705 HOST ADDED - {player_name} rebuys ${amount:.2f}\n"
            f"Total in: ${total_in:.2f} ({rebuys} rebuy{'s' if rebuys > 1 else ''})\n"
            f"Total Prize Pool: ${pot:.2f}"
        )
        await update_host_panel(game, chat_id, context)
        return

    # ── Host Add Rebuy - Cancelled ───────────────────────────────────────────
    if cb_data == "hadr_cancel":
        await query.answer()
        await query.edit_message_text("Rebuy cancelled.")
        return

    # ── Host-only actions below this point ────────────────────────────────
    if not is_host(game, uid):
        await query.answer("Only the host can do this.", show_alert=True)
        return

    await query.answer()

    # ── Winner Selection ──────────────────────────────────────────────────
    if cb_data.startswith("winner_"):
        parts = cb_data.split("_")
        place = int(parts[1])
        winner_uid = parts[2]

        if winner_uid not in game["players"]:
            await query.edit_message_text("Player not found in tournament.")
            return

        winner_name = display_name(game["players"][winner_uid])
        pot = get_total_pot(game)
        player_count = len(game["players"])
        payout_struct = get_payout_structure(player_count)
        default_amount = pot * payout_struct[place]
        pct = int(payout_struct[place] * 100)

        place_labels = {1: "1st", 2: "2nd", 3: "3rd"}
        place_label = place_labels.get(place, f"{place}th")

        await query.edit_message_text(
            f"Selected {winner_name} for {place_label} place.\n\n"
            f"Choose payout:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"Default: ${default_amount:.0f} ({pct}%)",
                    callback_data=f"winpay_default_{place}_{winner_uid}",
                )],
                [InlineKeyboardButton(
                    "Custom Amount",
                    callback_data=f"winpay_custom_{place}_{winner_uid}",
                )],
            ]),
        )
        return

    # ── Winner Payout Choice ────────────────────────────────────────────
    if cb_data.startswith("winpay_"):
        parts = cb_data.split("_")
        payout_type = parts[1]  # "default" or "custom"
        place = int(parts[2])
        winner_uid = parts[3]

        if winner_uid not in game["players"]:
            await query.edit_message_text("Player not found in tournament.")
            return

        winner_name = display_name(game["players"][winner_uid])
        pot = get_total_pot(game)
        player_count = len(game["players"])
        payout_struct = get_payout_structure(player_count)

        place_emojis = {1: "\U0001f947", 2: "\U0001f948", 3: "\U0001f949"}
        place_labels = {1: "1st", 2: "2nd", 3: "3rd"}
        place_label = place_labels.get(place, f"{place}th")
        place_emoji = place_emojis.get(place, "")

        if payout_type == "custom":
            # Set awaiting state so host can type the amount
            game["awaiting_payout"] = {
                "place": place,
                "uid": winner_uid,
                "name": winner_name,
            }
            save_data(data)
            await query.edit_message_text(
                f"Type the payout amount for {winner_name} ({place_label} place):\n"
                f"(Prize pool: ${pot:.2f})"
            )
            return

        # Default payout
        payout_amount = pot * payout_struct[place]
        pct = payout_struct[place]

        game["winners"][str(place)] = {
            "user_id": winner_uid,
            "name": winner_name,
            "payout": round(payout_amount, 2),
            "percentage": pct,
        }
        if "awaiting_payout" in game:
            del game["awaiting_payout"]
        save_data(data)

        await query.edit_message_text(
            f"{place_emoji} RECORDED: {place_label} Place\n\n"
            f"Winner: {winner_name}\n"
            f"Payout: ${payout_amount:.2f} ({int(pct * 100)}% of ${pot:.2f})"
        )
        await update_host_panel(game, chat_id, context)

        already_picked = [game["winners"][str(p)]["user_id"] for p in range(1, place + 1) if str(p) in game["winners"]]
        max_places = len(payout_struct)

        if place < max_places:
            next_place = place + 1
            next_label = place_labels.get(next_place, f"{next_place}th")
            await query.message.reply_text(
                f"Now select {next_label} place:",
                reply_markup=winner_keyboard(game["players"], next_place, exclude_uids=already_picked),
            )
        else:
            summary_lines = ["\U0001f3c6 ALL WINNERS RECORDED!\n"]
            for p in range(1, max_places + 1):
                w = game["winners"][str(p)]
                emoji = place_emojis.get(p, "")
                summary_lines.append(
                    f"  {emoji} {w['name']} - ${w['payout']:.2f} ({int(w['percentage'] * 100)}%)"
                )
            summary_lines.append(f"\n\U0001f4b0 Total Prize Pool: ${pot:.2f}")
            summary_lines.append("\nUse /settle to see the settlement dashboard.")
            await query.message.reply_text("\n".join(summary_lines))

        await update_host_panel(game, chat_id, context)
        return

    # ── Join / Rebuy Approval ─────────────────────────────────────────────
    parts = cb_data.split("_", 1)
    action = parts[0]
    if len(parts) < 2:
        await query.answer("Invalid request.", show_alert=True)
        return
    request_id = parts[1]

    # Look up request by unique ID (fall back to index for old pending requests)
    req = None
    for r in game["pending"]:
        if r.get("request_id") == request_id:
            req = r
            break
    if req is None:
        # Backward compat: try as index for old requests without request_id
        try:
            req_index = int(request_id)
            if 0 <= req_index < len(game["pending"]):
                req = game["pending"][req_index]
        except ValueError:
            pass
    if req is None:
        await query.answer("Request not found.", show_alert=True)
        return

    await query.answer()

    if req["status"] != "pending":
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    player_uid = req["user_id"]
    player_name = req["name"]
    amount = req["amount"]

    if action == "reject":
        req["status"] = "rejected"
        save_data(data)
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    # APPROVE
    req["status"] = "approved"

    if req["type"] == "join":
        # Auto-apply persistent nickname (lookup by user_id)
        nick = game.get("nicknames", {}).get(player_uid)
        player_entry = {"name": player_name, "buy_ins": [amount], "eliminated": False}
        if nick:
            player_entry["nickname"] = nick
        game["players"][player_uid] = player_entry
        total_players = len(game["players"])
        pot = get_total_pot(game)
        save_data(data)

        try:
            await query.message.delete()
        except Exception:
            pass
        await update_host_panel(game, chat_id, context)

    elif req["type"] == "rebuy":
        if player_uid in game["players"]:
            logger.info(f"REBUY APPROVED: player={player_name} uid={player_uid} amount={amount} req={request_id} host_uid={uid} chat={chat_id}")
            game["players"][player_uid]["buy_ins"].append(amount)
            game["players"][player_uid]["eliminated"] = False
            total_in = sum(game["players"][player_uid]["buy_ins"])
            rebuys = len(game["players"][player_uid]["buy_ins"]) - 1
            pot = get_total_pot(game)
            save_data(data)

            try:
                await query.message.delete()
            except Exception:
                pass
            await update_host_panel(game, chat_id, context)
        else:
            try:
                await query.message.delete()
            except Exception:
                pass

    save_data(data)


# ── Player Commands ──────────────────────────────────────────────────────────

async def join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user = update.effective_user
    uid = str(user.id)
    name = user.first_name or user.username or f"Player_{uid[-4:]}"

    data = load_data()
    game = get_game(chat_id, data)

    if not game["active"]:
        await update.message.reply_text("No active tournament. Ask the host to /newgame.")
        return
    if uid in game["players"]:
        await update.message.reply_text("You're already in!")
        return
    for req in game["pending"]:
        if req["user_id"] == uid and req["type"] == "join" and req["status"] == "pending":
            await update.message.reply_text("Your join request is already pending.")
            return

    request_id = uuid.uuid4().hex[:8]
    game["pending"].append({
        "type": "join", "user_id": uid, "name": name,
        "amount": game["buy_in_amount"], "status": "pending",
        "timestamp": datetime.now().isoformat(),
        "request_id": request_id,
    })
    save_data(data)

    await update.message.reply_text(
        f"JOIN REQUEST\n\n"
        f"Player: {name}\n"
        f"Buy-in: ${game['buy_in_amount']:.2f}\n\n"
        f"{game['host_name']}, approve or reject:",
        reply_markup=approval_keyboard(request_id),
    )


async def rebuy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user = update.effective_user
    uid = str(user.id)

    data = load_data()
    game = get_game(chat_id, data)

    if not game["active"]:
        await update.message.reply_text("No active tournament.")
        return
    if uid not in game["players"]:
        await update.message.reply_text("You're not in this tournament. Use /join first.")
        return
    if game.get("rebuy_locked"):
        await update.message.reply_text("Rebuys are closed!")
        return

    amount = game["buy_in_amount"]
    if context.args:
        try:
            amount = float(context.args[0])
        except ValueError:
            await update.message.reply_text("Usage: /rebuy [amount]")
            return

    name = display_name(game["players"][uid])
    request_id = uuid.uuid4().hex[:8]
    game["pending"].append({
        "type": "rebuy", "user_id": uid, "name": name,
        "amount": amount, "status": "pending",
        "timestamp": datetime.now().isoformat(),
        "request_id": request_id,
    })
    save_data(data)

    current_in = sum(game["players"][uid]["buy_ins"])
    await update.message.reply_text(
        f"REBUY REQUEST\n\n"
        f"Player: {name}\n"
        f"Rebuy: ${amount:.2f} (currently ${current_in:.2f} in)\n\n"
        f"{game['host_name']}, approve or reject:",
        reply_markup=approval_keyboard(request_id),
    )


# ── Host: Winners / Status / Settle / Endgame / History ──────────────────────

async def winners(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    uid = str(update.effective_user.id)

    data = load_data()
    game = get_game(chat_id, data)

    if not game["active"]:
        await update.message.reply_text("No active tournament.")
        return
    if not is_host(game, uid):
        await update.message.reply_text("Only the host can record winners.")
        return

    player_count = len(game["players"])
    if player_count < 2:
        await update.message.reply_text("Need at least 2 players to record winners.")
        return

    pot = get_total_pot(game)
    payout_struct = get_payout_structure(player_count)
    places = len(payout_struct)

    already_picked = []
    start_place = 1
    for p in range(1, places + 1):
        if str(p) in game["winners"]:
            already_picked.append(game["winners"][str(p)]["user_id"])
            start_place = p + 1

    if start_place > places:
        await update.message.reply_text(
            "Winners already recorded! Use /settle to see the dashboard, or /endgame to finish."
        )
        return

    place_labels = {1: "1st", 2: "2nd", 3: "3rd"}
    place_label = place_labels.get(start_place, f"{start_place}th")

    await update.message.reply_text(
        f"\U0001f3c6 RECORD WINNERS\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
        f"\U0001f4b0 Total Prize Pool: ${pot:.2f} | Players: {player_count}\n"
        f"{format_payout_structure(player_count)}\n\n"
        f"Select {place_label} place:",
        reply_markup=winner_keyboard(game["players"], start_place, exclude_uids=already_picked),
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = load_data()
    game = get_game(chat_id, data)

    if not game["active"]:
        await update.message.reply_text("No active tournament. Host: /newgame <amount>")
        return

    player_count = len(game["players"])
    pot = get_total_pot(game)
    pending_count = sum(1 for r in game["pending"] if r["status"] == "pending")
    payout_struct = get_payout_structure(player_count)
    rebuy_status = "CLOSED" if game.get("rebuy_locked") else "OPEN"
    game_name = game.get("game_name") or "Tournament"

    text = (
        f"\U0001f4ca TOURNAMENT STATUS\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
        f"\U0001f3af {game_name}\n\n"
        f"\U0001f464 Host: {game['host_name']}\n"
        f"\U0001f4b5 Buy-in: ${game['buy_in_amount']:.2f}\n"
        f"\U0001f552 Started: {game['started_at'][:16].replace('T', ' ')}\n"
        f"\U0001f465 Players: {player_count}\n"
        f"\U0001f4b0 Total Prize Pool: ${pot:.2f}\n"
        f"\U0001f504 Rebuys: {rebuy_status}\n"
    )
    if pending_count:
        text += f"\u23f3 Pending requests: {pending_count}\n"

    text += f"\n\U0001f3c6 {format_payout_structure(player_count)}\n"

    text += "\n\U0001f4b8 Prize Pool Breakdown:\n"
    place_emojis = {1: "\U0001f947", 2: "\U0001f948", 3: "\U0001f949"}
    for place, pct in payout_struct.items():
        amount = pot * pct
        emoji = place_emojis.get(place, "  ")
        text += f"  {emoji} ${amount:.2f} ({int(pct * 100)}%)\n"

    text += f"\n\U0001f465 Players:\n{player_summary(game)}"

    if game["winners"]:
        text += "\n\n\U0001f3c6 Winners:\n"
        for place_str in sorted(game["winners"].keys()):
            w = game["winners"][place_str]
            emoji = place_emojis.get(int(place_str), "  ")
            text += f"  {emoji} {w['name']} - ${w['payout']:.2f}\n"

    await update.message.reply_text(text)


async def settle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    uid = str(update.effective_user.id)

    data = load_data()
    game = get_game(chat_id, data)

    if not game["active"]:
        await update.message.reply_text("No active tournament.")
        return
    if not is_host(game, uid):
        await update.message.reply_text("Only the host can run settlements.")
        return

    player_count = len(game["players"])
    payout_struct = get_payout_structure(player_count)
    required_places = len(payout_struct)

    if len(game["winners"]) < required_places:
        await update.message.reply_text(
            f"Record all winners first! Need {required_places} place(s).\n"
            f"Use /winners to record."
        )
        return

    await update.message.reply_text(format_settle_dashboard(game))


async def settleall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Combined settlement across multiple games.

    Usage:
      /settleall          - today's games
      /settleall 8        - last 8 games (any dates)
      /settleall 2026-02-18 2026-02-20  - date range
    """
    chat_id = str(update.effective_chat.id)

    data = load_data()
    game = get_game(chat_id, data)

    history = game.get("history", [])
    selected_games = []
    title_line = ""

    # Also build active game summary if winners are fully recorded
    active_summary = None
    if game.get("active") and game.get("winners"):
        player_count = len(game["players"])
        payout_struct = get_payout_structure(player_count)
        if len(game["winners"]) >= len(payout_struct):
            pot = get_total_pot(game)
            active_summary = {
                "date": game.get("started_at"),
                "game_name": game.get("game_name") or "Current Game",
                "pot": pot,
                "buy_in": game["buy_in_amount"],
                "player_count": player_count,
                "players": {
                    display_name(p): {"in": sum(p["buy_ins"]), "rebuys": len(p["buy_ins"]) - 1}
                    for p in game["players"].values()
                },
                "winners": {
                    place: {
                        "name": w["name"],
                        "payout": w["payout"],
                        "pct": int(w["percentage"] * 100),
                    }
                    for place, w in game["winners"].items()
                },
            }

    args = context.args or []

    if not args:
        # Default: today's games
        target_date = datetime.now().strftime("%Y-%m-%d")
        selected_games = [h for h in history if h.get("date", "")[:10] == target_date]
        if active_summary and active_summary.get("date", "")[:10] == target_date:
            selected_games.append(active_summary)
        friendly = format_date_ordinal(datetime.now())
        title_line = f"\U0001f4c5 {friendly}"

    elif len(args) == 1 and args[0].isdigit():
        # Last N games: /settleall 8
        n = int(args[0])
        selected_games = list(history[-n:])
        if active_summary and len(selected_games) < n:
            selected_games.append(active_summary)
        elif active_summary:
            selected_games = selected_games[-(n - 1):] + [active_summary]
        title_line = f"\U0001f3b2 Last {n} games"

    elif len(args) == 2:
        # Date range: /settleall 2026-02-18 2026-02-20
        start_date, end_date = args[0], args[1]
        selected_games = [
            h for h in history
            if start_date <= h.get("date", "")[:10] <= end_date
        ]
        if active_summary and start_date <= active_summary.get("date", "")[:10] <= end_date:
            selected_games.append(active_summary)
        try:
            friendly_start = format_date_ordinal(datetime.strptime(start_date, "%Y-%m-%d"))
            friendly_end = format_date_ordinal(datetime.strptime(end_date, "%Y-%m-%d"))
        except ValueError:
            friendly_start, friendly_end = start_date, end_date
        title_line = f"\U0001f4c5 {friendly_start} \u2192 {friendly_end}"

    elif len(args) == 1:
        # Single date: /settleall 2026-02-18
        target_date = args[0]
        selected_games = [h for h in history if h.get("date", "")[:10] == target_date]
        if active_summary and active_summary.get("date", "")[:10] == target_date:
            selected_games.append(active_summary)
        try:
            friendly = format_date_ordinal(datetime.strptime(target_date, "%Y-%m-%d"))
        except ValueError:
            friendly = target_date
        title_line = f"\U0001f4c5 {friendly}"

    if not selected_games:
        await update.message.reply_text(
            "No completed games found.\n\n"
            "Usage:\n"
            "  /settleall - today's games\n"
            "  /settleall 8 - last 8 games\n"
            "  /settleall 2026-02-18 2026-02-20 - date range"
        )
        return

    # Build the dashboard
    combined_pnl = {}
    place_emojis = {"1": "\U0001f947", "2": "\U0001f948", "3": "\U0001f949"}
    game_count = len(selected_games)

    text = (
        f"\U0001f4ca COMBINED SETTLEMENT\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
        f"{title_line}\n"
        f"\U0001f3b0 {game_count} game{'s' if game_count != 1 else ''} played\n\n"
    )

    total_prize_pool = 0

    for gi, g in enumerate(selected_games, 1):
        gname = g.get("game_name") or f"Game {gi}"
        pot = g.get("pot", 0)
        total_prize_pool += pot
        game_date = g.get("date", "")[:10] if g.get("date") else ""

        text += f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        text += f"\U0001f3af {gname}"
        if game_date:
            text += f"  ({game_date})"
        text += "\n"
        text += f"\U0001f4b0 ${pot:.2f} | \U0001f465 {g.get('player_count', '?')} players\n"

        if g.get("winners"):
            for place, w in sorted(g["winners"].items()):
                emoji = place_emojis.get(str(place), "  ")
                text += f"  {emoji} {w['name']} - ${w['payout']:.2f} ({w['pct']}%)\n"

        # Per-game P&L
        game_pnl = {}
        players_data = g.get("players", {})
        winners_data = g.get("winners", {})

        for pname, pdata in players_data.items():
            total_in = pdata.get("in", 0)
            payout = 0.0
            for w in winners_data.values():
                if w["name"] == pname:
                    payout = w["payout"]
                    break
            net = payout - total_in
            game_pnl[pname] = net
            combined_pnl[pname] = combined_pnl.get(pname, 0) + net

        sorted_pnl = sorted(game_pnl.items(), key=lambda x: -x[1])
        for pname, net in sorted_pnl:
            if net > 0.01:
                icon = "\U0001f7e2"
                sign = "+"
            elif net < -0.01:
                icon = "\U0001f534"
                sign = ""
            else:
                icon = "\u26aa"
                sign = " "
            text += f"  {icon} {pname:<14} {sign}${net:.2f}\n"

        text += "\n"

    # Combined summary
    text += (
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001f3c6 COMBINED RESULTS\n"
        f"\U0001f4b0 Total Prize Pool: ${total_prize_pool:.2f}\n\n"
    )

    # Combined P&L
    text += "\U0001f4c8 OVERALL P&L\n"
    sorted_combined = sorted(combined_pnl.items(), key=lambda x: -x[1])
    for rank, (pname, net) in enumerate(sorted_combined, 1):
        if net > 0.01:
            icon = "\U0001f7e2"
            sign = "+"
        elif net < -0.01:
            icon = "\U0001f534"
            sign = ""
        else:
            icon = "\u26aa"
            sign = " "
        text += f"  {icon} {pname:<14} {sign}${net:.2f}\n"

    # Combined minimal payments
    debtors = sorted(
        [(k, -v) for k, v in combined_pnl.items() if v < -0.01],
        key=lambda x: -x[1],
    )
    creditors = sorted(
        [(k, v) for k, v in combined_pnl.items() if v > 0.01],
        key=lambda x: -x[1],
    )

    if debtors and creditors:
        text += "\n\U0001f4b8 COMBINED PAYMENTS (settle once!)\n"
        i, j = 0, 0
        while i < len(debtors) and j < len(creditors):
            d_name, debt = debtors[i]
            c_name, credit = creditors[j]
            amount = min(debt, credit)
            if amount > 0.01:
                text += f"  {d_name} \u27a1 {c_name}: ${amount:.2f}\n"
            debtors[i] = (d_name, debt - amount)
            creditors[j] = (c_name, credit - amount)
            if debtors[i][1] < 0.01:
                i += 1
            if creditors[j][1] < 0.01:
                j += 1
    else:
        text += "\n\u26aa Everyone broke even across all games!"

    await update.message.reply_text(text)


async def endgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    uid = str(update.effective_user.id)

    data = load_data()
    game = get_game(chat_id, data)

    if not game["active"]:
        await update.message.reply_text("No active tournament.")
        return
    if not is_host(game, uid):
        await update.message.reply_text("Only the host can end the tournament.")
        return

    # Check if winners are recorded
    player_count = len(game["players"])
    payout_struct = get_payout_structure(player_count)
    required_places = len(payout_struct)

    if len(game["winners"]) < required_places:
        await update.message.reply_text(
            f"\u26a0\ufe0f WINNERS NOT RECORDED!\n\n"
            f"You have {len(game['winners'])}/{required_places} winners recorded.\n"
            f"Without winners, this game can't be included in /settleall.\n\n"
            f"What would you like to do?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("\U0001f3c6 Record Winners First", callback_data="endgame_record_winners")],
                [InlineKeyboardButton("\U0001f3c1 End Without Winners", callback_data="endgame_confirm")],
            ]),
        )
        return

    pot = get_total_pot(game)
    game_name = game.get("game_name") or "Tournament"
    place_emojis = {"1": "\U0001f947", "2": "\U0001f948", "3": "\U0001f949"}
    original_host_uid = game["host_id"]

    summary = {
        "date": game["started_at"],
        "host": game["host_name"],
        "host_uid": game["host_id"],
        "buy_in": game["buy_in_amount"],
        "pot": pot,
        "game_name": game_name,
        "player_count": len(game["players"]),
        "players": {
            display_name(p): {
                "in": sum(p["buy_ins"]),
                "rebuys": len(p["buy_ins"]) - 1,
            }
            for p in game["players"].values()
        },
        "player_uids": {
            pid: display_name(p)
            for pid, p in game["players"].items()
        },
        "winners": {
            place: {"name": w["name"], "payout": w["payout"], "pct": int(w["percentage"] * 100)}
            for place, w in game["winners"].items()
        },
        "total_requests": len(game["pending"]),
        "approved": sum(1 for r in game["pending"] if r["status"] == "approved"),
        "rejected": sum(1 for r in game["pending"] if r["status"] == "rejected"),
    }
    game["history"].append(summary)

    if len(game["history"]) > HISTORY_LIMIT:
        game["history"] = game["history"][-HISTORY_LIMIT:]

    # Reset
    game["active"] = False
    game["players"] = {}
    game["pending"] = []
    game["winners"] = {}
    game["host_id"] = None
    game["host_name"] = None
    game["buy_in_amount"] = None
    game["started_at"] = None
    game["game_name"] = None
    game["rebuy_locked"] = False
    game["lobby_message_id"] = None

    game["awaiting_final_photo"] = {"host_id": original_host_uid}
    save_data(data)

    winner_lines = ""
    for place, w in sorted(summary["winners"].items()):
        emoji = place_emojis.get(str(place), "  ")
        winner_lines += f"  {emoji} {w['name']} - ${w['payout']:.2f}\n"

    await update.message.reply_text(
        f"\U0001f3c1 TOURNAMENT OVER!\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
        f"\U0001f3af {game_name}\n"
        f"\U0001f465 Players: {summary['player_count']} | \U0001f4b0 Prize Pool: ${pot:.2f}\n\n"
        f"\U0001f3c6 Winners:\n{winner_lines}\n"
        f"Archived. Use /history to review."
    )

    await update.message.reply_text(
        "\U0001f4f8 Upload the final hand picture?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("\U0001f4f8 Upload Photo", callback_data="endgame_photo_upload")],
            [InlineKeyboardButton("\u23ed Skip", callback_data="endgame_photo_skip")],
        ]),
    )

    # Refresh host panel to inactive state
    await update_host_panel(game, chat_id, context)


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = load_data()
    game = get_game(chat_id, data)

    if not game["history"]:
        await update.message.reply_text("No past tournaments yet.")
        return

    page_size = 10
    total = len(game["history"])
    total_pages = (total + page_size - 1) // page_size

    page = total_pages
    if context.args:
        try:
            page = int(context.args[0])
            page = max(1, min(page, total_pages))
        except ValueError:
            pass

    start_idx = (page - 1) * page_size
    end_idx = min(start_idx + page_size, total)
    page_games = game["history"][start_idx:end_idx]

    place_emojis = {"1": "\U0001f947", "2": "\U0001f948", "3": "\U0001f949"}
    text = (
        f"\U0001f4da TOURNAMENT HISTORY\n"
        f"Page {page}/{total_pages} ({total} games)\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
    )

    for i, g in enumerate(page_games, start_idx + 1):
        gname = g.get("game_name") or f"Game #{i}"
        text += (
            f"\U0001f3af {gname}\n"
            f"  Host: {g.get('host', 'N/A')} | "
            f"Buy-in: ${g.get('buy_in', 0):.2f}\n"
            f"  \U0001f465 {g.get('player_count', '?')} players | "
            f"\U0001f4b0 ${g.get('pot', 0):.2f}\n"
        )
        if g.get("winners"):
            for place, w in sorted(g["winners"].items()):
                emoji = place_emojis.get(str(place), "  ")
                text += f"  {emoji} {w['name']} - ${w['payout']:.2f} ({w['pct']}%)\n"
        if g.get("final_hand_photo"):
            text += "  \U0001f4f8 Final hand photo attached\n"
        text += "\n"

    if total_pages > 1:
        text += f"Use /history <page> to navigate (1-{total_pages})"

    await update.message.reply_text(text)

    # Send final hand photos for games on this page
    for i, g in enumerate(page_games, start_idx + 1):
        if g.get("final_hand_photo"):
            gname = g.get("game_name") or f"Game #{i}"
            await context.bot.send_photo(
                chat_id=int(chat_id),
                photo=g["final_hand_photo"],
                caption=f"\U0001f4f8 Final hand - {gname}",
            )


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    data = load_data()
    game = get_game(chat_id, data)

    history = game.get("history", [])
    completed = [g for g in history if g.get("winners")]

    # Also count active game if winners fully recorded
    has_active = False
    if game.get("active") and game.get("winners"):
        player_count = len(game.get("players", {}))
        payout_struct = get_payout_structure(player_count)
        if len(game["winners"]) >= len(payout_struct):
            has_active = True

    if not completed and not has_active:
        await update.message.reply_text("No completed games yet. Play some poker first!")
        return

    stats, game_count = build_leaderboard_stats(game)
    if not stats:
        await update.message.reply_text("No stats to show yet.")
        return

    await update.message.reply_text(format_leaderboard(stats, game_count))


async def reopen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    uid = str(update.effective_user.id)

    data = load_data()
    game = get_game(chat_id, data)

    if game["active"]:
        await update.message.reply_text("A tournament is already active. Use /endgame first.")
        return

    if not game["history"]:
        await update.message.reply_text("No past tournaments to reopen.")
        return

    last = game["history"][-1]

    if last.get("host_uid") and last["host_uid"] != uid:
        await update.message.reply_text("Only the original host can reopen a tournament.")
        return

    game["active"] = True
    game["host_id"] = uid
    game["host_name"] = last.get("host", update.effective_user.first_name)
    game["buy_in_amount"] = last.get("buy_in", 0)
    game["started_at"] = last.get("date")
    game["game_name"] = last.get("game_name")
    game["pending"] = []
    game["winners"] = {}
    game["rebuy_locked"] = False

    game["players"] = {}
    # Build reverse mapping: display_name -> uid (from player_uids if available)
    name_to_uid = {}
    if last.get("player_uids"):
        name_to_uid = {dname: uid for uid, dname in last["player_uids"].items()}

    nicknames = game.get("nicknames", {})
    for pname, pdata in last.get("players", {}).items():
        total_in = pdata.get("in", 0)
        rebuys = pdata.get("rebuys", 0)
        buy_in = last.get("buy_in", 0)
        buy_ins = [buy_in] + [buy_in] * rebuys if buy_in > 0 else [total_in]
        if abs(sum(buy_ins) - total_in) > 0.01:
            buy_ins = [total_in]
        # Use uid as key if available, otherwise fall back to name
        player_uid = name_to_uid.get(pname, pname)
        nick = nicknames.get(player_uid, pname)
        game["players"][player_uid] = {"name": pname, "buy_ins": buy_ins, "eliminated": False, "nickname": nick}

    game["history"].pop()
    save_data(data)

    pot = get_total_pot(game)
    player_count = len(game["players"])
    game_name = game.get("game_name") or "Tournament"

    await update.message.reply_text(
        f"\U0001f504 TOURNAMENT REOPENED!\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
        f"\U0001f3af {game_name}\n\n"
        f"Host: {game['host_name']}\n"
        f"Buy-in: ${game['buy_in_amount']:.2f}\n"
        f"Players: {player_count} | Total Prize Pool: ${pot:.2f}\n"
        f"{format_payout_structure(player_count)}\n\n"
        f"Use /winners to record placements.\n"
        f"Use /settle after recording winners."
    )

    # Post or refresh host panel (which now includes lobby)
    if game.get("host_panel_message_id"):
        await update_host_panel(game, chat_id, context)
    else:
        host_msg = await update.message.reply_text(
            host_panel_text(game),
            reply_markup=host_panel_keyboard(game),
        )
        game["host_panel_message_id"] = host_msg.message_id
    save_data(data)


# ── Final Hand Photo Handler ──────────────────────────────────────────────────
async def handle_final_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Captures photo upload for final hand when awaiting_final_photo is set."""
    chat_id = str(update.effective_chat.id)
    uid = str(update.effective_user.id)

    data = load_data()
    game = get_game(chat_id, data)

    logger.info(f"Photo received in chat {chat_id} from user {uid}")
    afp = game.get("awaiting_final_photo")
    if not afp or not afp.get("ready"):
        logger.info(f"Not awaiting photo: afp={afp}")
        return  # Not awaiting a photo

    file_id = update.message.photo[-1].file_id  # Largest size
    logger.info(f"Saving final hand photo: {file_id[:20]}...")
    if game["history"]:
        game["history"][-1]["final_hand_photo"] = file_id
    game.pop("awaiting_final_photo", None)
    save_data(data)

    game_name = game["history"][-1].get("game_name", "Tournament") if game["history"] else "Tournament"
    await update.message.reply_text(f"\U0001f4f8 Final hand photo saved for {game_name}!")
    await update_host_panel(game, chat_id, context)


# ── Custom Payout Handler ────────────────────────────────────────────────────
async def handle_custom_payout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Captures typed dollar amount for custom winner payout or new game buy-in."""
    chat_id = str(update.effective_chat.id)
    uid = str(update.effective_user.id)
    user = update.effective_user

    data = load_data()
    game = get_game(chat_id, data)

    # Handle new game buy-in amount
    if game.get("awaiting_newgame"):
        ang = game["awaiting_newgame"]
        if ang["user_id"] != uid:
            return  # Only the user who tapped New Game

        raw = update.message.text.strip().replace("$", "").replace(",", "")
        try:
            buy_in = float(raw)
        except ValueError:
            await update.message.reply_text("Please type a valid buy-in amount (e.g. 20).")
            return

        if buy_in <= 0:
            await update.message.reply_text("Amount must be greater than 0.")
            return

        host_name = ang["name"]
        del game["awaiting_newgame"]

        # Start the game (reuse newgame logic)
        game["active"] = True
        game["host_id"] = uid
        game["host_name"] = host_name
        game["buy_in_amount"] = buy_in
        game["players"] = {}
        game["pending"] = []
        game["winners"] = {}
        game["rebuy_locked"] = False
        game["lobby_message_id"] = None
        game["started_at"] = datetime.now().isoformat()
        game["game_name"] = generate_game_name(game)

        # Auto-add host (with persistent nickname if set)
        host_entry = {"name": host_name, "buy_ins": [buy_in], "eliminated": False}
        host_nick = game.get("nicknames", {}).get(uid)
        if host_nick:
            host_entry["nickname"] = host_nick
        game["players"][uid] = host_entry
        save_data(data)

        await update.message.reply_text(
            f"\U0001f3b0 TOURNAMENT STARTED!\n"
            f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
            f"\U0001f3af {game['game_name']}\n\n"
            f"Host: {host_name}\n"
            f"Buy-in: ${buy_in:.2f}\n"
            f"{host_name} (host) is automatically in.\n"
        )

        # Refresh the host panel to active state
        await update_host_panel(game, chat_id, context)
        return

    if not game.get("awaiting_payout"):
        return  # Not awaiting anything, ignore

    if not is_host(game, uid):
        return  # Only host can set payouts

    text = update.message.text.strip().replace("$", "").replace(",", "")
    try:
        payout_amount = float(text)
    except ValueError:
        await update.message.reply_text("Please type a valid dollar amount (e.g. 700).")
        return

    if payout_amount <= 0:
        await update.message.reply_text("Amount must be greater than 0.")
        return

    ap = game["awaiting_payout"]
    place = ap["place"]
    winner_uid = ap["uid"]
    winner_name = ap["name"]
    pot = get_total_pot(game)

    pct = payout_amount / pot if pot > 0 else 0

    place_emojis = {1: "\U0001f947", 2: "\U0001f948", 3: "\U0001f949"}
    place_labels = {1: "1st", 2: "2nd", 3: "3rd"}
    place_label = place_labels.get(place, f"{place}th")
    place_emoji = place_emojis.get(place, "")

    game["winners"][str(place)] = {
        "user_id": winner_uid,
        "name": winner_name,
        "payout": round(payout_amount, 2),
        "percentage": round(pct, 4),
    }
    del game["awaiting_payout"]
    save_data(data)

    await update.message.reply_text(
        f"{place_emoji} RECORDED: {place_label} Place\n\n"
        f"Winner: {winner_name}\n"
        f"Payout: ${payout_amount:.2f} ({int(pct * 100)}% of ${pot:.2f})"
    )
    await update_host_panel(game, chat_id, context)

    # Proceed to next place
    player_count = len(game["players"])
    payout_struct = get_payout_structure(player_count)
    max_places = len(payout_struct)
    already_picked = [game["winners"][str(p)]["user_id"] for p in range(1, place + 1) if str(p) in game["winners"]]

    if place < max_places:
        next_place = place + 1
        next_label = place_labels.get(next_place, f"{next_place}th")
        await update.message.reply_text(
            f"Now select {next_label} place:",
            reply_markup=winner_keyboard(game["players"], next_place, exclude_uids=already_picked),
        )
    else:
        summary_lines = ["\U0001f3c6 ALL WINNERS RECORDED!\n"]
        for p in range(1, max_places + 1):
            w = game["winners"][str(p)]
            emoji = place_emojis.get(p, "")
            summary_lines.append(
                f"  {emoji} {w['name']} - ${w['payout']:.2f} ({int(w['percentage'] * 100)}%)"
            )
        summary_lines.append(f"\n\U0001f4b0 Total Prize Pool: ${pot:.2f}")
        summary_lines.append("\nUse /settle to see the settlement dashboard.")
        await update.message.reply_text("\n".join(summary_lines))

    await update_host_panel(game, chat_id, context)


async def pnlgrid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    data = load_data()
    game = get_game(chat_id, data)

    game_labels, player_rows = build_pnl_grid(game)
    if not game_labels:
        await update.message.reply_text("No completed games yet. Play some poker first!")
        # Still post inactive panel if none exists
        if not game.get("host_panel_message_id") and not game.get("active"):
            panel_msg = await update.message.reply_text(
                host_panel_text(game),
                reply_markup=host_panel_keyboard(game),
            )
            game["host_panel_message_id"] = panel_msg.message_id
            save_data(data)
        return

    wl, wr = build_winners_grid(game)
    if wl and wr:
        winners_buf = generate_winners_grid_image(wl, wr)
        await context.bot.send_photo(chat_id=int(chat_id), photo=winners_buf)
    img_buf = generate_pnl_grid_image(game_labels, player_rows)
    await context.bot.send_photo(chat_id=int(chat_id), photo=img_buf)

    # Post inactive panel if none exists and no active game
    if not game.get("host_panel_message_id") and not game.get("active"):
        panel_msg = await update.message.reply_text(
            host_panel_text(game),
            reply_markup=host_panel_keyboard(game),
        )
        game["host_panel_message_id"] = panel_msg.message_id
        save_data(data)


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(False).build()

    # Host commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("newgame", newgame))
    app.add_handler(CommandHandler("kick", kick))
    app.add_handler(CommandHandler("nick", nick))
    app.add_handler(CommandHandler("winners", winners))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("settle", settle))
    app.add_handler(CommandHandler("endgame", endgame))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("reopen", reopen))
    app.add_handler(CommandHandler("settleall", settleall))
    app.add_handler(CommandHandler("lockrebuy", lockrebuy))
    app.add_handler(CommandHandler("unlockrebuy", unlockrebuy))
    app.add_handler(CommandHandler("pnlgrid", pnlgrid))

    # Player commands
    app.add_handler(CommandHandler("join", join))
    app.add_handler(CommandHandler("rebuy", rebuy))
    app.add_handler(CommandHandler("mystatus", mystatus))

    # Custom payout text input (must be before general callback handler)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_payout))

    # Final hand photo capture
    app.add_handler(MessageHandler(filters.PHOTO, handle_final_photo))

    # Inline button callbacks
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("Poker Tournament Bot (Host-Controlled) starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
