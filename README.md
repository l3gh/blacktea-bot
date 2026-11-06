# Blacktea Bot

A single-purpose Discord bot for Blacktea word survival. It includes a bundled English wordlist of **332,502 distinct words** and cross-server stats.

## Features

- `/blacktea` posts a lobby embed. Everyone, including the host, must react with 🍵 to join; solo play is supported. If nobody joins, the game is cancelled.
- `/stats [member]` shows wins, games, best survival streak, correct answers, and average letters per correct answer across all servers where the bot has recorded a game, all in one embed.
- `/leaderboard` shows global standings across all servers where the bot has recorded a game.
- `/rules` explains the game.
- When `BLACKTEA_LOG_WEBHOOK_URL` is set in `.env`, game starts and ends are logged to that Discord webhook. End logs group each player's result and accepted answers by player, in the order they answered.
- On `/blacktea`, choose an 8–30 second join window (default 20 seconds), a 5–15 second turn timer (default 10 seconds), and 1–10 starting lives per player (default 3).
- Choose **Easy** (top 250 ranked sequences), **Medium** (top 1,000), **Hard** (top 2,500), or **Insane** (ranks 500–2,500).
- A game's selected prompt pool is shuffled and used without repeats until exhausted; then the pool is reshuffled for another cycle.
- The host can choose whether the exact three-letter prompt itself is accepted as an answer (default: no).
- Every prompt is exactly three letters. During the final three seconds, the bot reacts to its prompt message with 3️⃣, 2️⃣, then 1️⃣.
- Answers must be unique valid words that contain the displayed letter sequence.
- Only the current player's messages are checked; messages from other players do not affect the turn. The current player is pinged in plain text before the prompt embed.
- An incorrect or repeated answer does not remove a life. The active player loses a life only when the 10-second turn timer expires.
- All bot messages and command replies are sent as embeds. Blacktea embeds use 🍵 as their theme emoji. Correct answers receive a ✅ reaction on the answer, which is the confirmation.

The accepted-word set combines the ESDB/SCOWL en_US-large list, the full available English `wordfreq` list, and a curated internet/community vocabulary supplement. Entries are filtered to ASCII words with at least three letters and deduplicated; the bot loads them into a Python set for fast membership checks, with no dictionary API at runtime. Source notices are included in `WORDLIST-COPYRIGHT.txt` and `WORDFREQ-NOTICE.md`. The separate `wordlist-community-additions.txt` file is loaded at startup and is an easy place to add contemporary terms. It includes terms such as `rizz`, `hypergamy`, `skibidi`, and `delulu`. The list is broader, but it can still omit specialist, proper, and non-English terms. Prompt rankings aggregate the frequency of three-letter sequences across common English words. They are letter sequences rather than phonetic syllables. Each prompt occurs in at least five accepted words. Prompt sets are bundled in `prompts.json`.

## Setup

1. Create a Discord application and bot in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Enable **Message Content Intent** under the bot's privileged gateway intents. The bot needs this to read typed word answers.
3. Invite it with `bot` and `applications.commands` scopes, with permission to view channels, send messages, embed links, add reactions, and read message history. Add Reactions is needed for the lobby and countdown.
4. Install Python 3.10 or newer. In this folder, run `python -m pip install -r requirements.txt`.
5. Copy `.env.example` to `.env` and paste the bot token after `DISCORD_TOKEN=`. Keep `.env` private.
6. Run `python bot.py`. Slash commands sync when the bot connects.

The bot only implements Blacktea commands and game handling. It stores per-server game result rows and answer-length totals in `blacktea.sqlite3`, then aggregates them across servers for stats and leaderboard views. Existing databases are upgraded automatically when the bot starts.
