#!/usr/bin/env python3
"""
Telegram bot for Wingo Prediction Engines (1-min & 30-sec).
Commands:
  /start 1min      – start the 1‑minute engine
  /start 30sec     – start the 30‑second engine
  /stop 1min       – stop the 1‑minute engine
  /stop 30sec      – stop the 30‑second engine
  /stats           – show current statistics for both engines
"""

import os
import sys
import math
import time
import threading
import logging
import asyncio
from dataclasses import dataclass
from typing import Dict, Optional

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Import the two engine modules (they must be in the same directory)
import enginep1
import enginep30

# ============================================================
# Configuration
# ============================================================
TELEGRAM_TOKEN = os.getenv("8287229498:AAFWqPd3kB6nMqATo3zf8nN48GiWkCER36E")
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN environment variable not set.")

LOG_LEVEL = logging.INFO
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=LOG_LEVEL,
)
logger = logging.getLogger("WingoBot")

# ============================================================
# Statistics Class
# ============================================================
class Stats:
    """Track all metrics for one engine."""
    def __init__(self):
        self.total_rounds = 0
        self.wins = 0          # size correct
        self.losses = 0        # size incorrect
        self.number_hits = 0   # exact number correct
        self.max_win_streak = 0
        self.max_loss_streak = 0
        self.current_streak = 0  # positive = win streak, negative = loss streak
        self.win_rate = 0.0
        self.hit_rate = 0.0

    def update(self, size_correct: bool, number_correct: bool) -> None:
        """Update stats after one round."""
        self.total_rounds += 1

        if size_correct:
            self.wins += 1
            if self.current_streak >= 0:
                self.current_streak += 1
            else:
                self.current_streak = 1
            if self.current_streak > self.max_win_streak:
                self.max_win_streak = self.current_streak
        else:
            self.losses += 1
            if self.current_streak <= 0:
                self.current_streak -= 1
            else:
                self.current_streak = -1
            if abs(self.current_streak) > self.max_loss_streak:
                self.max_loss_streak = abs(self.current_streak)

        if number_correct:
            self.number_hits += 1

        if self.total_rounds > 0:
            self.win_rate = self.wins / self.total_rounds
            self.hit_rate = self.number_hits / self.total_rounds

    def to_message(self, engine_name: str) -> str:
        """Format stats as a readable message."""
        return (
            f"📊 *Stats for {engine_name}*\n"
            f"┌───────────────────\n"
            f"│ Total Rounds    : {self.total_rounds}\n"
            f"│ Wins (size)     : {self.wins}\n"
            f"│ Losses (size)   : {self.losses}\n"
            f"│ Win Rate        : {self.win_rate*100:.2f}%\n"
            f"│ Number Hits     : {self.number_hits}\n"
            f"│ Hit Rate        : {self.hit_rate*100:.2f}%\n"
            f"│ Max Win Streak  : {self.max_win_streak}\n"
            f"│ Max Loss Streak : {self.max_loss_streak}\n"
            f"│ Current Streak  : {self.current_streak}\n"
            f"└───────────────────"
        )

# ============================================================
# Engine Runner (runs the prediction loop in a separate thread)
# ============================================================
class EngineRunner:
    def __init__(
        self,
        config,
        service,
        name: str,
        interval: int,
        bot_app: Application,
        chat_id: int,
        update_interval: int = 100,
    ):
        self.config = config
        self.service = service
        self.name = name
        self.interval = interval          # seconds between rounds (30 or 60)
        self.bot_app = bot_app
        self.chat_id = chat_id
        self.update_interval = update_interval

        self.stats = Stats()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.round_counter = 0

    def start(self) -> bool:
        """Start the engine thread if not already running."""
        if self.thread and self.thread.is_alive():
            return False
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        return True

    def stop(self) -> bool:
        """Signal the engine to stop and wait for thread to finish."""
        if not self.thread or not self.thread.is_alive():
            return False
        self.stop_event.set()
        self.thread.join(timeout=5)
        self.thread = None
        return True

    def _send_stats(self) -> None:
        """Send a stats message via the bot (called from the engine thread)."""
        text = self.stats.to_message(self.name)
        # Schedule the send in the asyncio event loop
        asyncio.run_coroutine_threadsafe(
            self.bot_app.bot.send_message(chat_id=self.chat_id, text=text, parse_mode="Markdown"),
            self.bot_app.loop,
        )

    def _run_loop(self) -> None:
        """The main prediction loop – adapted from enginep1/enginep30 real_time_loop."""
        service = self.service
        user = "user_1"   # fixed user for this engine

        # Initialise history from API
        try:
            service.initialise_history(user)
        except Exception as e:
            logger.error(f"{self.name}: Failed to initialise history: {e}")
            self._send_error(f"Initialisation failed: {e}")
            return

        history = service.get_cumulative_history(user)
        logger.info(f"{self.name}: Initial history loaded, length={len(history)}")

        # Get latest issue for period numbering
        latest = service.data_fetcher.fetch_latest_outcome_with_issue()
        if latest:
            latest_num, latest_issue = latest
        else:
            latest_issue = "?"
        next_issue = enginep1.increment_issue_number(latest_issue) if latest_issue != "?" else "Next Round"

        # First prediction
        pred = service.predict_for_session(user)
        pred_number = pred['number']
        pred_size = pred['size']

        # Main loop
        while not self.stop_event.is_set():
            now = time.time()
            current_round = math.floor(now / self.interval) * self.interval
            next_round = current_round + self.interval
            wait_until = next_round + 2
            sleep_time = wait_until - time.time()

            # Sleep, but check stop_event periodically
            while sleep_time > 0 and not self.stop_event.is_set():
                time.sleep(min(1, sleep_time))
                sleep_time -= 1

            if self.stop_event.is_set():
                break

            # Fetch actual outcome
            try:
                actual_info = service.data_fetcher.fetch_latest_outcome_with_issue()
                if actual_info:
                    actual_num, actual_issue = actual_info
                else:
                    # Fallback: fetch only the number
                    actual_num = service.fetch_latest_outcome()
                    actual_issue = latest_issue
            except Exception as e:
                logger.error(f"{self.name}: Error fetching actual outcome: {e}")
                # Skip this round? For simplicity, we skip.
                continue

            if actual_num is None:
                logger.warning(f"{self.name}: No outcome received, skipping round.")
                continue

            actual_size = 'BIG' if actual_num >= self.config.BIG_THRESHOLD else 'SMALL'
            size_correct = (pred_size == actual_size)
            number_correct = (pred_number == actual_num)

            # Update stats
            self.stats.update(size_correct, number_correct)
            self.round_counter += 1

            logger.info(
                f"{self.name} Round {self.round_counter}: "
                f"Pred={pred_number}/{pred_size}, Actual={actual_num}/{actual_size}, "
                f"Win={size_correct}, Hit={number_correct}"
            )

            # Send periodic stats
            if self.round_counter % self.update_interval == 0:
                self._send_stats()

            # Provide feedback and get next prediction
            try:
                service.feedback(user, actual_num)
            except Exception as e:
                logger.error(f"{self.name}: Feedback error: {e}")

            next_issue = enginep1.increment_issue_number(actual_issue) if actual_issue != "?" else "Next Round"
            pred = service.predict_for_session(user)
            pred_number = pred['number']
            pred_size = pred['size']
            latest_issue = actual_issue

        logger.info(f"{self.name}: Engine stopped.")

    def _send_error(self, text: str) -> None:
        asyncio.run_coroutine_threadsafe(
            self.bot_app.bot.send_message(chat_id=self.chat_id, text=f"❌ {text}"),
            self.bot_app.loop,
        )

# ============================================================
# Bot Command Handlers
# ============================================================
class WingoBot:
    def __init__(self):
        self.engines: Dict[str, EngineRunner] = {}
        self.chat_id: Optional[int] = None

    def get_engine(self, name: str) -> Optional[EngineRunner]:
        return self.engines.get(name)

    def start_engine(self, name: str, app: Application, chat_id: int) -> bool:
        if name in self.engines and self.engines[name].thread and self.engines[name].thread.is_alive():
            return False

        if name == "1min":
            config = enginep1.CONFIG
            service = enginep1.WingoService(config)
            interval = 60
        elif name == "30sec":
            config = enginep30.CONFIG
            service = enginep30.WingoService(config)
            interval = 30
        else:
            return False

        runner = EngineRunner(config, service, name, interval, app, chat_id)
        runner.start()
        self.engines[name] = runner
        return True

    def stop_engine(self, name: str) -> bool:
        runner = self.engines.get(name)
        if runner is None:
            return False
        if runner.stop():
            del self.engines[name]
            return True
        return False

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start with optional argument."""
        if not context.args:
            await update.message.reply_text(
                "Please specify which engine to start:\n"
                "/start 1min   – start 1‑minute engine\n"
                "/start 30sec  – start 30‑second engine"
            )
            return

        arg = context.args[0].lower()
        if arg not in ("1min", "30sec"):
            await update.message.reply_text("Invalid argument. Use '1min' or '30sec'.")
            return

        chat_id = update.effective_chat.id
        if chat_id != self.chat_id:
            self.chat_id = chat_id  # store for periodic messages

        success = self.start_engine(arg, context.application, chat_id)
        if success:
            await update.message.reply_text(f"✅ {arg} engine started.")
        else:
            await update.message.reply_text(f"⚠️ {arg} engine is already running.")

    async def stop_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /stop with argument."""
        if not context.args:
            await update.message.reply_text(
                "Please specify which engine to stop:\n"
                "/stop 1min   – stop 1‑minute engine\n"
                "/stop 30sec  – stop 30‑second engine"
            )
            return

        arg = context.args[0].lower()
        if arg not in ("1min", "30sec"):
            await update.message.reply_text("Invalid argument. Use '1min' or '30sec'.")
            return

        success = self.stop_engine(arg)
        if success:
            await update.message.reply_text(f"🛑 {arg} engine stopped.")
        else:
            await update.message.reply_text(f"⚠️ {arg} engine is not running.")

    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show stats for both engines (or a specific one if argument given)."""
        if context.args:
            arg = context.args[0].lower()
            if arg in ("1min", "30sec"):
                runner = self.engines.get(arg)
                if runner:
                    text = runner.stats.to_message(arg)
                else:
                    text = f"Engine '{arg}' is not running."
                await update.message.reply_text(text, parse_mode="Markdown")
                return
            else:
                await update.message.reply_text("Invalid argument. Use '1min' or '30sec' or no argument for both.")
                return

        # Show both
        if not self.engines:
            await update.message.reply_text("No engines are currently running.")
            return

        for name, runner in self.engines.items():
            text = runner.stats.to_message(name)
            await update.message.reply_text(text, parse_mode="Markdown")

# ============================================================
# Main
# ============================================================
def main() -> None:
    """Start the bot."""
    bot = WingoBot()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", bot.start_command))
    app.add_handler(CommandHandler("stop", bot.stop_command))
    app.add_handler(CommandHandler("stats", bot.stats_command))

    # Store bot instance for access from threads
    app.bot_data["bot_instance"] = bot

    logger.info("Bot started. Listening for commands...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()