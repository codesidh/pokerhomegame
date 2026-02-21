# Poker Night Tournament Bot — Host-Controlled

A Telegram bot for managing home poker **tournaments**. Host controls everything — player joins, rebuys, and winner recording. Payouts auto-calculate based on player count.

## Payout Structure

| Players | Places Paid | Split |
|---------|------------|-------|
| > 6     | Top 3      | 60% / 30% / 10% |
| ≤ 6     | Top 2      | 70% / 30% |

## Multi-Game Support

Each Telegram group chat runs its own independent tournament. You can add the bot to multiple groups and they won't interfere with each other.

## Setup (5 minutes)

```bash
# 1. Create bot via @BotFather on Telegram -> copy token
# 2. Install & run
pip install python-telegram-bot
export BOT_TOKEN="your-token"
python poker_bot.py
# 3. Add bot to your group chat(s)
```

## Tournament Flow

```
HOST                                 PLAYERS
────                                 ───────
/newgame 20
  (sets $20 buy-in, host auto-joins)
                                     /join
  [Accept] [Reject]
  Host taps Accept                   "Player joins! Pot: $40"

                                     /rebuy
  [Accept] [Reject]
  Host taps Accept                   Rebuy added to pot

/status
  Shows pot, players, payout structure

  ── Tournament plays out ──

/winners
  Bot shows player buttons:
  [Alice] [Bob] [Charlie] [Dave]
  Host taps 1st place winner          "1st: Alice -> $120 (60%)"
  Bot auto-prompts 2nd place          "2nd: Bob -> $60 (30%)"
  Bot auto-prompts 3rd (if >6)        "3rd: Charlie -> $20 (10%)"

/settle
  Shows who pays whom (minimal transfers)

/endgame
  Archives everything to /history
```

## Command Reference

| Command | Who | What |
|---------|-----|------|
| `/newgame 20` | Host | Start tournament, set buy-in |
| `/status` | Anyone | Pot, players, payout structure |
| `/winners` | Host | Record 1st/2nd/3rd via buttons |
| `/settle` | Host | Calculate payments |
| `/endgame` | Host | End & archive |
| `/history` | Anyone | Past 5 tournaments |
| `/kick Name` | Host | Remove player |
| `/join` | Player | Request to join → host approves |
| `/rebuy [amt]` | Player | Request rebuy → host approves |
| `/mystatus` | Player | Check own position |

## Example Settlement (8 players, $20 buy-in)

```
Pot: $200 (includes 2 rebuys)

WINNERS:
  1st: Alice  -> $120.00 (60%)
  2nd: Bob    -> $60.00  (30%)
  3rd: Charlie -> $20.00 (10%)

PAYMENTS:
  Dave   -> pays $20.00 -> Alice
  Eve    -> pays $20.00 -> Alice
  Frank  -> pays $20.00 -> Alice
  Grace  -> pays $20.00 -> Alice
  Henry  -> pays $20.00 -> Bob

P&L:
  1. Alice:   +$100.00
  2. Bob:     +$40.00
  3. Charlie: +$0.00
  4. Dave:    -$20.00
  ...
```
