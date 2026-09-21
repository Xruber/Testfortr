#!/usr/bin/env python3
"""
================================================================================
 TELEGRAM REPORTER FOR THE CONSCIOUS MINDS ENGINE
================================================================================
 Runs the prediction engine for N rounds and reports live stats to Telegram.

 SETUP:
   1. Save the engine file as `engine.py` in the same folder as this file.
   2. Create a Telegram bot via @BotFather and get the BOT TOKEN.
   3. Get your CHAT_ID by messaging @userinfobot on Telegram.
   4. Paste both values below.
   5. Run:  python bot.py

 The bot will:
   - Send a compact update after every round.
   - Send a full report every REPORT_EVERY rounds.
   - Send a final report when the run finishes.
   - Save the final report to `final_report.txt`.
================================================================================
"""

import time
import math
import urllib.request
import urllib.parse
import traceback
from collections import Counter

#  Import the engine (must be saved as engine.py in the same folder) 
from engine import (
    Fetcher, Engine, increment_issue, CONFIG,
    render_chart, render_topk, render_topk_rolling,
    render_admin_table, render_bet_sim, render_meta,
)

# 
# TELEGRAM CONFIG — FILL THESE IN
# 
TELEGRAM_TOKEN   = "8287229498:AAFWqPd3kB6nMqATo3zf8nN48GiWkCER36E"
TELEGRAM_CHAT_ID = "6239774927"
TOTAL_ROUNDS     = 100
REPORT_EVERY     = 10          # send a full report every N rounds
SEND_EVERY_ROUND = True        # if False, only send full reports


# 
# TELEGRAM SENDER
# 
class Telegram:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self.endpoint = f"https://api.telegram.org/bot{token}/sendMessage"

    def send(self, text):
        # Telegram hard limit is 4096 chars; split if needed
        for chunk in self._chunks(text, 3900):
            self._send_one(chunk)

    def _send_one(self, text):
        data = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
        try:
            req = urllib.request.Request(
                self.endpoint,
                data=urllib.parse.urlencode(data).encode("utf-8"),
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                if r.status != 200:
                    print(f"[TG] non-200 status {r.status}")
        except Exception as e:
            print(f"[TG ERROR] {e}")

    @staticmethod
    def _chunks(text, size):
        for i in range(0, len(text), size):
            yield text[i:i + size]


# 
# REPORT BUILDERS
# 
def build_round_message(round_num, engine, actual_num, prev_pred):
    lines = []
    lines.append(f" ROUND {round_num}/{TOTAL_ROUNDS}")
    lines.append("")

    # Previous prediction result
    if prev_pred is not None:
        hit = " HIT" if prev_pred == actual_num else " MISS"
        lines.append(f"Prev: {prev_pred}  actual {actual_num} [{hit}]")
    else:
        lines.append(f"Actual: {actual_num}")

    # Accuracy so far
    if engine.total_rounds > 0:
        acc = engine.correct / engine.total_rounds * 100
        lines.append(f"Accuracy: {engine.correct}/{engine.total_rounds} ({acc:.1f}%)")

    # Top-K rolling 10
    lines.append("")
    lines.append(" Top-K (last 10):")
    lines.append(render_topk_rolling(engine.topk_history))

    # Bet simulator P&L
    lines.append("")
    lines.append(" Bet P&L (9.8x):")
    for k, hits, total, hr, pnl in engine.bet_sim.summary():
        sign = "+" if pnl >= 0 else ""
        lines.append(f"  Top-{k}: {hits}/{total}  ({hr*100:.0f}%)  {sign}{pnl:.1f}u")

    return "\n".join(lines)


def build_full_report(engine, final=False, round_num=0):
    lines = []
    header = " FINAL REPORT" if final else f" MID REPORT (round {round_num})"
    lines.append(header)
    lines.append("" * 32)

    # Overall
    total = engine.total_rounds
    acc = engine.correct / total * 100 if total else 0
    lines.append(f"Rounds: {total}")
    lines.append(f"Correct: {engine.correct} ({acc:.1f}%)")

    # Top-K 10
    lines.append("")
    lines.append(" Top-K (last 10):")
    lines.append(render_topk_rolling(engine.topk_history))

    # Top-K 50
    if len(engine.topk_history_50) > 0:
        lines.append("")
        lines.append(" Top-K (last 50):")
        lines.append(render_topk_rolling(engine.topk_history_50))

    # Bet simulator
    lines.append("")
    lines.append(" Bet Simulator (9.8x payout):")
    lines.append(render_bet_sim(engine.bet_sim))

    # Admin strategy table
    lines.append("")
    lines.append(" Admin strategy table:")
    lines.append(render_admin_table(engine.admin))

    # Meta-learner
    lines.append("")
    lines.append(" Meta-learner:")
    lines.append(render_meta(engine.admin))

    # Top minds
    lines.append("")
    lines.append(" Top minds:")
    for name, weight, hits, tot, mood, energy, streak in engine.top_minds(8):
        a = hits / tot if tot > 0 else 0
        lines.append(f"  {name}: w={weight:.2f} "
                     f"acc={hits}/{tot} ({a*100:.0f}%) "
                     f"[{mood} {streak:+d}]")

    # Mood distribution
    lines.append("")
    lines.append(" Mood distribution:")
    moods = Counter(m.mood for m in engine.minds)
    for m, c in moods.most_common():
        lines.append(f"  {m}: {c}")

    if final:
        lines.append("")
        lines.append("" * 32)
        lines.append("Run complete.")

    return "\n".join(lines)


# 
# MAIN LOOP
# 
def main():
    print("=" * 70)
    print("  TELEGRAM REPORTER  —  Starting up")
    print("=" * 70)

    tg = Telegram(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)
    fetcher = Fetcher()
    engine = Engine()

    tg.send(f" Bot starting — running {TOTAL_ROUNDS} rounds")

    # Fetch initial history
    print("\nFetching initial history …")
    nums = None
    while nums is None or len(nums) == 0:
        nums = fetcher.fetch_history_oldest_first()
        if nums is None:
            print("  Fetch failed — retry in 10 s.")
            time.sleep(10)
    engine.history = nums[-CONFIG.MAX_HISTORY:]
    print(f"Loaded: {engine.history}")
    tg.send(f" Loaded history: {engine.history}")

    round_num = 0
    try:
        while round_num < TOTAL_ROUNDS:
            round_num += 1

            # Wait for the next round boundary
            now = time.time()
            nb = (math.floor(now / CONFIG.ROUND_SECONDS) * CONFIG.ROUND_SECONDS
                  + CONFIG.ROUND_SECONDS)
            wait = nb - now + 3
            if wait > 0:
                time.sleep(wait)

            # Fetch the settled round
            actual_num, settled_issue = None, None
            for _ in range(5):
                actual_num, settled_issue = fetcher.fetch_latest()
                if actual_num is not None:
                    break
                time.sleep(2)
            if actual_num is None:
                print(f"Round {round_num}: fetch failed")
                tg.send(f" Round {round_num}: fetch failed")
                round_num -= 1
                continue

            prev_pred = engine.last_prediction
            engine.observe(actual_num)

            upcoming = increment_issue(settled_issue)
            engine.set_issue(upcoming)

            decision = engine.run_round()

            print(f"Round {round_num}: actual={actual_num} "
                  f"prev_pred={prev_pred} next={decision['pick']} "
                  f"strategy={decision['strategy']}")

            # Send compact per-round message
            if SEND_EVERY_ROUND:
                msg = build_round_message(round_num, engine, actual_num, prev_pred)
                tg.send(msg)

            # Send full report every N rounds
            if round_num % REPORT_EVERY == 0:
                time.sleep(1)
                report = build_full_report(engine, final=False, round_num=round_num)
                tg.send(report)

        # Final report
        time.sleep(1)
        final_report = build_full_report(engine, final=True)
        tg.send(final_report)

        # Save to file
        try:
            with open("final_report.txt", "w", encoding="utf-8") as f:
                f.write(final_report)
            print("\nSaved final report to final_report.txt")
        except Exception as e:
            print(f"Could not save report: {e}")

    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        partial = build_full_report(engine, final=False, round_num=round_num)
        tg.send(" Stopped early.\n\n" + partial)
    except Exception as e:
        print(f"\nCRASH: {e}")
        traceback.print_exc()
        tg.send(f" CRASH: {e}\n\nRound {round_num}")


if __name__ == "__main__":
    main()