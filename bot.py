import asyncio
import json
import os
import random
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN in .env before starting the bot.")
LOG_WEBHOOK_URL = os.getenv("BLACKTEA_LOG_WEBHOOK_URL")

WORD_FILES = (ROOT / "wordlist.txt", ROOT / "wordlist-community-additions.txt")
WORDS = set()
for word_file in WORD_FILES:
    if word_file.exists():
        with word_file.open(encoding="utf-8") as word_stream:
            for line in word_stream:
                word = line.strip().lower()
                if word:
                    WORDS.add(word)
EDGE_NON_LETTERS = re.compile(r"^[^a-z]+|[^a-z]+$")
ASCII_WORD = re.compile(r"[a-z]+")
PROMPT_POOLS = json.loads((ROOT / "prompts.json").read_text(encoding="utf-8"))
DIFFICULTY_LABELS = {
    "easy": "Easy",
    "medium": "Medium",
    "hard": "Hard",
    "insane": "Insane",
}

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
DB = sqlite3.connect(ROOT / "blacktea.sqlite3")
DB.execute("PRAGMA journal_mode=WAL")
DB.execute("PRAGMA synchronous=NORMAL")
DB.execute("CREATE TABLE IF NOT EXISTS stats (guild_id INTEGER, user_id INTEGER, wins INTEGER DEFAULT 0, games INTEGER DEFAULT 0, best_streak INTEGER DEFAULT 0, PRIMARY KEY (guild_id,user_id))")
columns = {row[1] for row in DB.execute("PRAGMA table_info(stats)")}
if "correct_answers" not in columns:
    DB.execute("ALTER TABLE stats ADD COLUMN correct_answers INTEGER DEFAULT 0")
if "total_letters" not in columns:
    DB.execute("ALTER TABLE stats ADD COLUMN total_letters INTEGER DEFAULT 0")
DB.execute("CREATE INDEX IF NOT EXISTS stats_user_id_idx ON stats(user_id)")
DB.commit()

@dataclass
class Player:
    user: discord.Member
    hp: int
    survived: int = 0

@dataclass
class Game:
    channel: discord.TextChannel
    guild_id: int
    host_id: int
    turn_seconds: int
    starting_hp: int
    lobby_seconds: int = 20
    allow_prompt_word: bool = False
    difficulty: str = "easy"
    players: dict[int, Player] = field(default_factory=dict)
    used: set[str] = field(default_factory=set)
    prompt: str = ""
    prompt_deck: list[str] = field(default_factory=list)
    turn_order: list[int] = field(default_factory=list)
    turn_index: int = 0
    ended: bool = False
    lobby_open: bool = True
    turn_deadline: float = 0.0
    lobby_message_id: int | None = None
    answers: dict[int, list[tuple[str, str]]] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def alive_ids(self):
        return [uid for uid in self.turn_order if uid in self.players and self.players[uid].hp > 0]

active: dict[int, Game] = {}

def make_embed(description: str, title: str | None = None, color: discord.Color | None = None) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color or discord.Color.blurple())

def _embed_text_size(embed: discord.Embed) -> int:
    return (len(embed.title or "") + len(embed.description or "")
            + sum(len(field.name) + len(field.value) for field in embed.fields)
            + len(embed.footer.text or ""))

async def send_webhook_log(embeds: list[discord.Embed]):
    if not LOG_WEBHOOK_URL or not embeds:
        return
    try:
        webhook = discord.Webhook.from_url(LOG_WEBHOOK_URL, client=bot)
        batch: list[discord.Embed] = []
        batch_size = 0
        for embed in embeds:
            embed_size = _embed_text_size(embed)
            if batch and (len(batch) >= 10 or batch_size + embed_size > 5800):
                await asyncio.wait_for(webhook.send(embeds=batch, username="Blacktea Logs", allowed_mentions=discord.AllowedMentions.none()), timeout=10)
                batch = []
                batch_size = 0
            batch.append(embed)
            batch_size += embed_size
        if batch:
            await asyncio.wait_for(webhook.send(embeds=batch, username="Blacktea Logs", allowed_mentions=discord.AllowedMentions.none()), timeout=10)
    except Exception as exc:
        print(f"Webhook logging failed: {type(exc).__name__}")

async def log_game_start(game: Game):
    players = sorted((p.user.display_name for p in game.players.values()), key=str.casefold)
    player_text = "\n".join(discord.utils.escape_markdown(name) for name in players) or "No players"
    description = (
        f"Server: **{discord.utils.escape_markdown(game.channel.guild.name)}** (`{game.guild_id}`)\n"
        f"Channel: **#{discord.utils.escape_markdown(game.channel.name)}**\n"
        f"Players ({len(players)}):\n{player_text}"
    )
    embed = discord.Embed(title="🍵 Blacktea game started", description=description, color=discord.Color.green())
    embed.add_field(name="Join window", value=f"{game.lobby_seconds}s", inline=True)
    embed.add_field(name="Turn time", value=f"{game.turn_seconds}s", inline=True)
    embed.add_field(name="Starting lives", value=str(game.starting_hp), inline=True)
    embed.add_field(name="Difficulty", value=DIFFICULTY_LABELS[game.difficulty], inline=True)
    embed.add_field(name="Exact prompt answer", value="Allowed" if game.allow_prompt_word else "Not allowed", inline=True)
    await send_webhook_log([embed])

def _split_log_lines(lines: list[str], limit: int = 3500) -> list[str]:
    pages: list[str] = []
    page = ""
    for line in lines:
        addition = ("\n" if page else "") + line
        if page and len(page) + len(addition) > limit:
            pages.append(page)
            page = line
        else:
            page += addition
    if page or not pages:
        pages.append(page or "No answers recorded.")
    return pages

async def log_game_end(game: Game, reason: str, winner_id: int | None):
    embeds: list[discord.Embed] = []
    players = sorted(game.players.values(), key=lambda p: (p.user.display_name.casefold(), p.user.id))
    for player in players:
        uid = player.user.id
        result = "Winner" if uid == winner_id else ("Eliminated" if player.hp <= 0 else "Still alive")
        lines = [
            f"Server: **{discord.utils.escape_markdown(game.channel.guild.name)}** (`{game.guild_id}`) · **#{discord.utils.escape_markdown(game.channel.name)}**",
            f"Game ended: {discord.utils.escape_markdown(reason)}",
            f"Game result: **{result}** · Lives remaining: **{max(player.hp, 0)}** · Turns survived: **{player.survived}**",
            f"Correct answers: **{len(game.answers.get(uid, []))}**",
            "Answers in this player's turn order:",
        ]
        lines.extend(f"`{prompt}` → `{word}`" for prompt, word in game.answers.get(uid, []))
        pages = _split_log_lines(lines)
        for page_num, page in enumerate(pages, 1):
            title = f"🍵 Blacktea results — {discord.utils.escape_markdown(player.user.display_name)}"
            if len(pages) > 1:
                title = f"{title} ({page_num}/{len(pages)})"
            embeds.append(discord.Embed(title=title[:256], description=page, color=discord.Color.green()))
    if not embeds:
        embeds.append(discord.Embed(title="🍵 Blacktea game ended", description="No players were recorded.", color=discord.Color.green()))
    await send_webhook_log(embeds)

def pick_prompt(game: Game) -> str:
    if not game.prompt_deck:
        game.prompt_deck = PROMPT_POOLS[game.difficulty].copy()
        random.shuffle(game.prompt_deck)
    return game.prompt_deck.pop()

def update_game_stats(game: Game, winner_id: int | None):
    for uid, player in game.players.items():
        won = int(uid == winner_id)
        DB.execute("INSERT INTO stats(guild_id,user_id,wins,games,best_streak) VALUES(?,?,?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET wins=wins+excluded.wins,games=games+1,best_streak=MAX(best_streak,excluded.best_streak)", (game.guild_id, uid, won, 1, player.survived))
    DB.commit()

def record_answer(game: Game, user_id: int, word: str):
    DB.execute("UPDATE stats SET correct_answers=correct_answers+1,total_letters=total_letters+? WHERE guild_id=? AND user_id=?", (len(word), game.guild_id, user_id))
    DB.commit()

async def finish(game: Game, reason: str):
    if game.ended:
        return
    game.ended = True
    active.pop(game.channel.id, None)
    alive = [p for p in game.players.values() if p.hp > 0]
    winner = alive[0] if len(alive) == 1 else None
    update_game_stats(game, winner.user.id if winner else None)
    if winner:
        await game.channel.send(embed=make_embed(f"{reason}\nFinal streak: **{winner.survived}** turns.", title=f"🏆 {winner.user.display_name} wins Blacktea!", color=discord.Color.gold()))
    else:
        await game.channel.send(embed=make_embed(reason, title="🍵 Blacktea ended", color=discord.Color.green()))
    await log_game_end(game, reason, winner.user.id if winner else None)

async def run_turn(game: Game):
    while not game.ended:
        alive = game.alive_ids()
        if not alive or (len(alive) == 1 and len(game.players) > 1):
            await finish(game, "Last player standing.")
            return
        game.turn_index %= len(alive)
        uid = alive[game.turn_index]
        game.current_player_id = uid
        player = game.players[uid]
        game.prompt = pick_prompt(game)
        prompt_embed = make_embed(f"Your word must contain: **{game.prompt}**. You have {game.turn_seconds} seconds to respond!", title=f"🍵 {player.user.display_name}’s turn", color=discord.Color.green())
        prompt_embed.add_field(name="Lives", value=str(player.hp), inline=True)
        prompt_message = await game.channel.send(content=player.user.mention, embed=prompt_embed)
        deadline = asyncio.get_running_loop().time() + game.turn_seconds
        game.turn_deadline = time.time() + game.turn_seconds
        answered = False
        countdown_reactions = ((3, "3️⃣"), (2, "2️⃣"), (1, "1️⃣"))
        for remaining, emoji in countdown_reactions:
            wait_for = max(0, deadline - remaining - asyncio.get_running_loop().time())
            try:
                await asyncio.wait_for(game.events[uid].wait(), timeout=wait_for)
                answered = True
                break
            except asyncio.TimeoutError:
                try:
                    await prompt_message.add_reaction(emoji)
                except discord.HTTPException:
                    pass
        if not answered:
            try:
                # Briefly allow an already-sent, pre-deadline message to finish
                # dispatching through Discord. on_message checks its creation time.
                await asyncio.wait_for(game.events[uid].wait(), timeout=max(0, deadline + 0.75 - asyncio.get_running_loop().time()))
                answered = True
            except asyncio.TimeoutError:
                pass
        if answered:
            game.events[uid].clear()
            game.turn_index += 1
        else:
            player.hp -= 1
            await game.channel.send(embed=make_embed(f"{player.user.mention} ran out of time and lost a life. **{max(player.hp, 0)}** lives left.", title="⏱️ Time is up", color=discord.Color.red()))
            if player.hp <= 0:
                await game.channel.send(embed=make_embed(f"{player.user.display_name} has no lives left.", title="💀 Player eliminated", color=discord.Color.dark_grey()))
            else:
                game.turn_index += 1

# Slash-command and message handlers route into each channel's active game.
commands_synced = False
startup_presence_started = False

async def show_restart_status():
    try:
        await bot.change_presence(activity=discord.Game(name="✅ just restarted! ✅"))
        await asyncio.sleep(5)
        await bot.change_presence(activity=None)
    except discord.HTTPException as exc:
        print(f"Restart status update failed: {type(exc).__name__}")

@bot.event
async def on_ready():
    global commands_synced, startup_presence_started
    if not startup_presence_started:
        startup_presence_started = True
        asyncio.create_task(show_restart_status())
    if not commands_synced:
        await bot.tree.sync()
        commands_synced = True
    print(f"Blacktea bot ready as {bot.user} | loaded {len(WORDS):,} words")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    game = active.get(message.channel.id)
    if game and not game.ended:
        uid = message.author.id
        if (uid in game.players and game.players[uid].hp > 0
                and uid == game.current_player_id
                and message.created_at.timestamp() <= game.turn_deadline
                and not game.events[uid].is_set()):
            word = EDGE_NON_LETTERS.sub("", message.content.lower())
            if ASCII_WORD.fullmatch(word):
                prompt_word_allowed = game.allow_prompt_word or word != game.prompt
                if game.prompt in word and word in WORDS and word not in game.used and prompt_word_allowed:
                    game.used.add(word)
                    game.players[uid].survived += 1
                    record_answer(game, uid, word)
                    game.answers.setdefault(uid, []).append((game.prompt, word))
                    game.events[uid].set()
                    # Signal the turn immediately; Discord's reaction API can take
                    # long enough to race the 10-second timeout.
                    await message.add_reaction("✅")
                elif word in game.used:
                    await message.add_reaction("🔁")
    await bot.process_commands(message)

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id or str(payload.emoji) != "🍵":
        return
    game = active.get(payload.channel_id)
    if not game or not game.lobby_open or payload.message_id != game.lobby_message_id:
        return
    member = payload.member or (game.channel.guild.get_member(payload.user_id))
    if member is None or member.bot or payload.user_id in game.players:
        return
    game.players[member.id] = Player(member, game.starting_hp)
    game.turn_order.append(member.id)
    game.events[member.id] = asyncio.Event()

@bot.tree.command(name="blacktea", description="Start a Blacktea word survival game")
@app_commands.describe(lobby_time="Seconds players have to join (8–30)", turn_time="Seconds per turn (5–15)", lives="Starting lives per player (1–10)", allow_prompt_word="Allow answering with the exact three-letter prompt", difficulty="Choose a difficulty tier")
@app_commands.choices(difficulty=[
    app_commands.Choice(name="Easy", value="easy"),
    app_commands.Choice(name="Medium", value="medium"),
    app_commands.Choice(name="Hard", value="hard"),
    app_commands.Choice(name="Insane", value="insane"),
])
async def blacktea(interaction: discord.Interaction, lobby_time: app_commands.Range[int, 8, 30] = 20, turn_time: app_commands.Range[int, 5, 15] = 10, lives: app_commands.Range[int, 1, 10] = 3, allow_prompt_word: bool = False, difficulty: str = "easy"):
    if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message(embed=make_embed("Blacktea runs in a server text channel.", title="Blacktea"), ephemeral=True); return
    channel = interaction.channel
    gid = interaction.guild_id
    if channel.id in active:
        await interaction.response.send_message(embed=make_embed("A Blacktea game is already running in this channel.", title="Game already running"), ephemeral=True); return
    game = Game(channel, gid, interaction.user.id, turn_time, lives, lobby_seconds=lobby_time, allow_prompt_word=allow_prompt_word, difficulty=difficulty)
    game.events = {}
    # Allow other players to join during a short lobby, then begin turns.
    active[channel.id] = game
    embed = discord.Embed(title="🍵 Blacktea lobby", description=f"{interaction.user.mention} is hosting a game.\n\nReact with 🍵 to join before the lobby closes.", color=discord.Color.green())
    embed.add_field(name="Join window", value=f"{lobby_time} seconds", inline=True)
    embed.add_field(name="Starting lives", value=str(lives), inline=True)
    embed.add_field(name="Turn timer", value=f"{turn_time} seconds", inline=True)
    embed.add_field(name="Exact prompt as answer", value="Allowed" if allow_prompt_word else "Not allowed", inline=True)
    embed.add_field(name="Difficulty", value=DIFFICULTY_LABELS[difficulty], inline=True)
    await interaction.response.send_message(embed=embed)
    lobby_message = await interaction.original_response()
    game.lobby_message_id = lobby_message.id
    await lobby_message.add_reaction("🍵")
    await asyncio.sleep(lobby_time)
    if game.ended: return
    game.lobby_open = False
    if not game.players:
        game.ended = True
        active.pop(channel.id, None)
        await channel.send(embed=make_embed("No one joined, so the game was cancelled.", title="🍵 Blacktea cancelled"))
        return
    if len(game.players) > 1:
        first_player = game.players[game.turn_order[0]]
        await channel.send(embed=make_embed(f"**{len(game.players)} players** joined. First turn: {first_player.user.mention}.", title="🍵 Blacktea is starting"))
    asyncio.create_task(log_game_start(game))
    asyncio.create_task(run_turn(game))

@bot.tree.command(name="stats", description="Show your Blacktea stats across all servers")
@app_commands.describe(member="Player to look up; omit to show your own stats")
async def stats(interaction: discord.Interaction, member: discord.Member | None = None):
    if not interaction.guild:
        await interaction.response.send_message(embed=make_embed("Use this command in a server.", title="Blacktea stats", color=discord.Color.green()), ephemeral=True); return
    target = member or interaction.user
    row = DB.execute(
        "SELECT COALESCE(SUM(wins),0),COALESCE(SUM(games),0),COALESCE(MAX(best_streak),0),COALESCE(SUM(correct_answers),0),COALESCE(SUM(total_letters),0) FROM stats WHERE user_id=?",
        (target.id,),
    ).fetchone()
    wins, games, best, answers, letters = row or (0, 0, 0, 0, 0)
    average = f"{letters / answers:.1f}" if answers else "—"
    stats_embed = make_embed(
        f"**{wins}** wins / **{games}** games\nBest survival streak: **{best}** turns\nCorrect answers: **{answers}**\nAverage letters per correct answer: **{average}**\n\n*Stats combined across all servers.*",
        title=f"📊 {target.display_name}’s Blacktea stats",
        color=discord.Color.green(),
    )
    await interaction.response.send_message(embed=stats_embed)

@bot.tree.command(name="leaderboard", description="Show the global Blacktea leaderboard")
async def leaderboard(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message(embed=make_embed("Use this command in a server.", title="Blacktea leaderboard", color=discord.Color.green()), ephemeral=True); return
    rows = DB.execute("SELECT user_id,SUM(wins),SUM(games),MAX(best_streak) FROM stats GROUP BY user_id HAVING SUM(games)>0 ORDER BY SUM(wins) DESC,MAX(best_streak) DESC LIMIT 10").fetchall()
    if not rows:
        await interaction.response.send_message(embed=make_embed("No Blacktea stats yet. Start a game with `/blacktea`!", title="Blacktea global leaderboard", color=discord.Color.green())); return
    lines = []
    for pos, (uid, wins, games, best) in enumerate(rows, 1):
        user = interaction.guild.get_member(uid)
        lines.append(f"**{pos}.** {user.display_name if user else f'<@{uid}>'} — {wins} wins / {games} games · streak {best}")
    await interaction.response.send_message(embed=make_embed("\n".join(lines) + "\n\n*Rankings combined across all servers.*", title="🏆 Blacktea global leaderboard", color=discord.Color.green()))

@bot.tree.command(name="rules", description="Show how to play Blacktea")
async def rules(interaction: discord.Interaction):
    await interaction.response.send_message(embed=make_embed("Players take turns. Only the player whose turn it is can score an answer; other players’ messages are ignored. Type a valid English word containing the shown three-letter sequence before the selected turn timer expires. The bot counts down with 3️⃣, 2️⃣, 1️⃣ reactions during the final three seconds. Reused words and invalid answers do not count. You lose one life when time runs out; the last player with lives wins. The host chooses whether the exact three-letter prompt can count as an answer. React 🍵 to the lobby embed to join.", title="🍵 Blacktea rules", color=discord.Color.green()))

bot.run(TOKEN)
