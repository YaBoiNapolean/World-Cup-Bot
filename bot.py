import datetime
import asyncio
import discord
from discord import app_commands
from discord.ext import commands, tasks
import pytz
import requests
import os
import json

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_KEY = os.getenv("API_KEY")

API_URL = "https://v3.football.api-sports.io/fixtures"
CURRENT_WORLD_CUP_YEAR = "2026"
FIFA_LEAGUE_ID = "1"

API_HEADERS = {'x-rapidapi-key': API_KEY, 'x-rapidapi-host': 'v3.football.api-sports.io'}
API_PARAMS = {'league': FIFA_LEAGUE_ID, 'season': CURRENT_WORLD_CUP_YEAR}

intents = discord.Intents.default()
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)

cached_games = []
finished_matches_notified = {}

DATA_PATH = "/data/settings.json"
server_settings = {}

def load_settings():
    global server_settings
    if os.path.exists(DATA_PATH):
        try:
            with open(DATA_PATH, "r") as f:
                raw_data = json.load(f)
                server_settings = {int(k): v for k, v in raw_data.items()}
                print("Successfully loaded server configurations from Volume.")
        except Exception as e:
            print(f"Error loading settings file: {e}")
            server_settings = {}
    else:
        server_settings = {}

def save_settings():
    try:
        os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
        with open(DATA_PATH, "w") as f:
            json.dump(server_settings, f, indent=4)
        print("Settings backed up to Volume.")
    except Exception as e:
        print(f"Failed to save settings to Volume: {e}")

def get_server_data(guild_id: int) -> dict:
    if guild_id not in server_settings:
        server_settings[guild_id] = {
            "timezone": "America/Chicago",
            "channel_id": None,
            "role_id": None,
            "events": {}
        }
        save_settings()
    return server_settings[guild_id]

def fetch_world_cup_data():
    global cached_games
    try:
        response = requests.get(API_URL, headers=API_HEADERS, params=API_PARAMS, timeout=10)
        if response.status_code == 200:
            data = response.json()
            cached_games = data.get("response", [])
            print(f"Successfully loaded {len(cached_games)} matches.")
        else:
            print(f"API Error: {response.status_code}")
    except Exception as e:
        print(f"Error connecting to live sports database: {e}")

def parse_utc_to_tz(utc_string, target_tz_str):
    clean_utc = utc_string.split("+")[0].replace("Z", "")
    utc_dt = datetime.datetime.strptime(clean_utc, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=pytz.utc)
    return utc_dt.astimezone(pytz.timezone(target_tz_str))

@bot.event
async def on_ready():
    print(f"Bot connected as {bot.user.name}")
    load_settings()
    fetch_world_cup_data()
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")
    for guild in bot.guilds:
        get_server_data(guild.id)
        await create_world_cup_events(guild)
    if not check_live_matches.is_running():
        check_live_matches.start()
    if not update_match_results.is_running():
        update_match_results.start()

async def create_world_cup_events(guild):
    settings = get_server_data(guild.id)
    tz_str = settings["timezone"]
    try:
        existing_events = await guild.fetch_scheduled_events()
        existing_titles = [e.name for e in existing_events]
    except Exception:
        existing_titles, existing_events = [], []
    changes_made = False
    for item in cached_games:
        fixture = item["fixture"]
        teams = item["teams"]
        home = teams["home"]["name"]
        away = teams["away"]["name"]
        event_title = f"{home} vs. {away}"
        fixture_id = str(fixture["id"])
        if event_title in existing_titles:
            match_event = next(e for e in existing_events if e.name == event_title)
            if settings["events"].get(fixture_id) != match_event.url:
                settings["events"][fixture_id] = match_event.url
                changes_made = True
            continue
        local_start = parse_utc_to_tz(fixture["date"], tz_str)
        local_end = local_start + datetime.timedelta(hours=2, minutes=30)
        stadium_name = f"{fixture['venue']['name']} ({fixture['venue']['city']})"
        try:
            event = await guild.create_scheduled_event(
                name=event_title,
                description=f"🏆 FIFA World Cup {CURRENT_WORLD_CUP_YEAR} Match",
                start_time=local_start,
                end_time=local_end,
                location=stadium_name,
                privacy_level=discord.PrivacyLevel.guild_only,
                entity_type=discord.EntityType.external
            )
            settings["events"][fixture_id] = event.url
            changes_made = True
        except Exception as e:
            print(f"[{guild.name}] Skipped building event for {event_title}: {e}")
    if changes_made:
        save_settings()

@bot.tree.command(name="settimezone", description="Set your server's local timezone for World Cup events.")
@app_commands.describe(tz_name="Standard timezone name (e.g., America/Chicago, UTC)")
@app_commands.checks.has_permissions(manage_guild=True)
async def set_timezone(interaction: discord.Interaction, tz_name: str):
    if tz_name in pytz.all_timezones:
        settings = get_server_data(interaction.guild_id)
        settings["timezone"] = tz_name
        save_settings()
        await interaction.response.send_message(f"✅ **Timezone updated to:** `{tz_name}`")
        await create_world_cup_events(interaction.guild)
    else:
        await interaction.response.send_message(f"❌ `{tz_name}` is invalid. Here is a list of all of the valid timezones: https://docs.google.com/document/d/1agvFSDUScWmEkvVKQ13YNUYRTC2FDnjNL65cDjSkVXU/edit?usp=sharing", ephemeral=True)

@bot.tree.command(name="setpingrole", description="Set the role to ping when a World Cup game starts.")
@app_commands.describe(role="The role to mention during announcements")
@app_commands.checks.has_permissions(manage_guild=True)
async def set_ping_role(interaction: discord.Interaction, role: discord.Role):
    settings = get_server_data(interaction.guild_id)
    settings["role_id"] = role.id
    save_settings()
    await interaction.response.send_message(f"✅ **Ping role updated to:** {role.mention}")

@bot.tree.command(name="setchannel", description="Set the channel where live matches and results are sent.")
@app_commands.describe(channel="The text channel for broadcasts")
@app_commands.checks.has_permissions(manage_guild=True)
async def set_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    settings = get_server_data(interaction.guild_id)
    settings["channel_id"] = channel.id
    save_settings()
    await interaction.response.send_message(f"✅ **Announcements channel updated to:** {channel.mention}")

@bot.tree.command(name="Test", description="Test to see if bot is functioning correctly.")
@app_commands.describe(channel="Test to see if the bot is functioning correctly")
async def test(interaction: discord.Interaction):
   await interaction.response.send_message("Test command executed :)")

@tasks.loop(minutes=1)
async def check_live_matches():
    for guild in bot.guilds:
        settings = get_server_data(guild.id)
        channel_id = settings["channel_id"]
        role_id = settings["role_id"]
        tz_str = settings["timezone"]
        if not channel_id:
            continue
        channel = guild.get_channel(channel_id)
        if not channel:
            continue
        tz = pytz.timezone(tz_str)
        now_local = datetime.datetime.now(tz).replace(second=0, microsecond=0)
        for item in cached_games:
            fixture = item["fixture"]
            game_start_local = parse_utc_to_tz(fixture["date"], tz_str).replace(second=0, microsecond=0)
            if now_local == game_start_local:
                teams = item["teams"]
                home = teams["home"]["name"]
                away = teams["away"]["name"]
                fixture_id = str(fixture["id"])
                event_link = settings["events"].get(fixture_id, "Check Server Events tab!")
                ping_mention = f"<@&{role_id}>" if role_id else "@everyone"
                announcement = (
                    f"{ping_mention} **({home} v. {away}) has started!** "
                    f"Tune in to watch some amazing Fútbol!\n"
                    f"👉 **Event Link:** {event_link}"
                )
                await channel.send(announcement)

@tasks.loop(minutes=5)
async def update_match_results():
    fetch_world_cup_data()
    for guild in bot.guilds:
        settings = get_server_data(guild.id)
        channel_id = settings["channel_id"]
        if not channel_id:
            continue
        channel = guild.get_channel(channel_id)
        if not channel:
            continue
        if guild.id not in finished_matches_notified:
            finished_matches_notified[guild.id] = set()
        for item in cached_games:
            fixture = item["fixture"]
            status = fixture["status"]["short"]
            fixture_id = str(fixture["id"])
            if status in ["FT", "AET", "PEN"] and fixture_id not in finished_matches_notified[guild.id]:
                teams = item["teams"]
                home = teams["home"]["name"]
                away = teams["away"]["name"]
                goals = item["goals"]
                home_score = goals["home"]
                away_score = goals["away"]
                if home_score > away_score:
                    outcome_text = f"**{home}** has won the match!"
                elif away_score > home_score:
                    outcome_text = f"**{away}** has won the match!"
                else:
                    outcome_text = "The match ended in a draw!"
                if teams["home"].get("winner") is True:
                    outcome_text = f"🎉 **{home}** wins and advances to the next round!"
                elif teams["away"].get("winner") is True:
                    outcome_text = f"🎉 **{away}** wins and advances to the next round!"
                result_message = (
                    f"🏁 **The match has concluded!**\n"
                    f"⚽ {home} `{home_score}` - `{away_score}` {away}\n"
                    f"{outcome_text}"
                )
                await channel.send(result_message)
                finished_matches_notified[guild.id].add(fixture_id)

@check_live_matches.before_loop
@update_match_results.before_loop
async def before_loops():
    await bot.wait_until_ready()

bot.run(BOT_TOKEN)
