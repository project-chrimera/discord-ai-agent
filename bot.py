#!/usr/bin/python3
import os
import discord
import asyncio
import subprocess
import json
import sys
import threading
import time
import re
from discord.ext import commands
from dotenv import load_dotenv
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import tempfile
from pathlib import Path
import websocket
import requests
import caldav
from datetime import datetime, timedelta, timezone
import pymysql.cursors

# -----------------------------
# Load environment
# -----------------------------
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
TARGET_GUILD_ID = int(os.getenv("DISCORD_GUILD"))
TARGET_ROLE_ID = int(os.getenv("DISCORD_ROLE_ID"))
WP_ROLE_ID = int(os.getenv("DISCORD_WP_ROLE_ID", "0"))
HA_URL = os.getenv("HAURL")
HA_TOKEN = os.getenv("HATOKEN")
DEFAULT_AGENT = os.getenv("DEFAULT_AGENT")
SSL = os.getenv("SSL", "0") == "1"
API_URL = os.getenv("API_URL")              # REST API base URL, e.g., http://127.0.0.1:5000
API_TOKEN = os.getenv("API_TOKEN")          # REST API token for authorization
MOD_IDS = [int(x.strip()) for x in os.getenv("MOD_IDS", "").split(",") if x.strip()]

#db
MYSQL_USER = os.getenv('MYSQL_USER')
MYSQL_HOST = os.getenv('MYSQL_HOST')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD')
MYSQL_DATABASE = os.getenv('MYSQL_DATABASE')

# Calendar & event channel
EVENT_CHANNEL_ID = int(os.getenv("EVENT_CHANNEL_ID", "0"))
NEXTCLOUD_URL = os.getenv("NEXTCLOUD_URL")
NEXTCLOUD_USER = os.getenv("NEXTCLOUD_USER")
NEXTCLOUD_PASS = os.getenv("NEXTCLOUD_PASS")
CALENDAR_NAME = os.getenv("CALENDAR_NAME")

# api
YAP2STW_API = os.getenv("YAP2STW_API")
YAP2STW_TOKEN = os.getenv("YAP2STW_TOKEN")


# -----------------------------
# Discord bot setup
# -----------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# -----------------------------
# AssistClient
# -----------------------------
class AssistClient:
    def __init__(self, ha_url, ha_token, default_agent=None, ssl=False):
        self.ha_url = ha_url
        self.ha_token = ha_token
        self.default_agent = default_agent
        self.protocol = "wss" if ssl else "ws"
        self.ws_url = f"{self.protocol}://{self.ha_url}/api/websocket"
        self.ws = None
        self.message_id_counter = 1
        self.conversation_id = None
        self.response_event = threading.Event()
        self.response_text = ""
        self.list_agents_mode = False
        self.agent_list = []
        self.conversation_file_path = None

    def _generate_message_id(self):
        mid = self.message_id_counter
        self.message_id_counter += 1
        return mid

    def _load_conversation_id(self, conv_id_override=None):
        if conv_id_override:
            self.conversation_file_path = Path(tempfile.gettempdir()) / f"assist_conversation_{conv_id_override}.txt"
            if self.conversation_file_path.exists():
                return self.conversation_file_path.read_text().strip()
        return None

    def _save_conversation_id(self, conv_id):
        if self.conversation_file_path:
            self.conversation_file_path.write_text(conv_id)

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            if data.get("type") == "auth_required":
                ws.send(json.dumps({"type":"auth","access_token":self.ha_token}))
            elif data.get("type") == "auth_ok":
                if self.list_agents_mode:
                    ws.send(json.dumps({"id": self._generate_message_id(),"type":"assist_pipeline/pipeline/list"}))
                else:
                    self.response_event.set()
            elif data.get("type") == "result" and data.get("success") and self.list_agents_mode:
                self.agent_list = data.get("result", {}).get("pipelines", [])
                self.response_event.set()
            elif data.get("type") == "event" and data.get("event", {}).get("type") == "intent-end":
                self.response_text = data["event"]["data"]["intent_output"]["response"]["speech"]["plain"]["speech"]
                self.conversation_id = data["event"]["data"]["intent_output"]["conversation_id"]
                self._save_conversation_id(self.conversation_id)
                self.response_event.set()
        except Exception as e:
            print(f"[AssistClient] Message parse error: {e}", file=sys.stderr)
            self.response_event.set()

    def _on_error(self, ws, error):
        print(f"[AssistClient] WS error: {error}", file=sys.stderr)
        self.response_event.set()

    def _send_intent(self, text, agent=None, conversation_id=None):
        payload = {
            "id": self._generate_message_id(),
            "type": "assist_pipeline/run",
            "start_stage": "intent",
            "end_stage": "intent",
            "input": {"text": text},
            "conversation_id": conversation_id if conversation_id else self.conversation_id
        }
        if agent:
            payload["pipeline"] = agent
        self.response_event.clear()
        self.ws.send(json.dumps(payload))

    def connect(self):
        if self.ws and self.ws.keep_running:
            return
        self.ws = websocket.WebSocketApp(
            self.ws_url,
            on_message=self._on_message,
            on_error=self._on_error,
        )
        self.thread = threading.Thread(target=self.ws.run_forever)
        self.thread.daemon = True
        self.thread.start()
        # wait for auth
        time_waited = 0
        while not self.ws.sock or not self.ws.sock.connected:
            time.sleep(0.1)
            time_waited += 0.1
            if time_waited > 5:
                raise RuntimeError("Failed to connect to Assist WebSocket")
        self.response_event.wait(timeout=5)

    def run_assist(self, text, agent=None, new=False, ci=None):
        self.connect()
        conversation_id = None if new else ci if ci else self.conversation_id
        self._send_intent(text, agent=agent, conversation_id=conversation_id)
        self.response_event.wait(timeout=15)
        return self.response_text

    def list_agents(self):
        self.list_agents_mode = True
        self.connect()
        self.response_event.wait(timeout=15)
        return self.agent_list

assist_client = AssistClient(HA_URL, HA_TOKEN, default_agent=DEFAULT_AGENT, ssl=SSL)



# -----------------------------
# Discord events
# -----------------------------
@bot.event
async def on_message(message):
    # Ignore messages from the bot itself
    if message.author == bot.user:
        return

    # Ensure message is from the right guild
    if not message.guild or message.guild.id != TARGET_GUILD_ID:
        return

    # Check required role
    author_role_ids = [role.id for role in message.author.roles]
    if TARGET_ROLE_ID not in author_role_ids:
       return

    # Respond only if bot is mentioned
    if bot.user in message.mentions:
        user_question = message.clean_content.replace(f"<@{bot.user.id}>", "").strip()
        if not user_question:
            await message.channel.send("🤔 You mentioned me, but said nothing...")
            return

        author_name = message.author.display_name
        full_input = ""
        if message.reference and isinstance(message.reference.resolved, discord.Message):
            parent_msg = message.reference.resolved
            parent_author = parent_msg.author.display_name
            parent_content = parent_msg.content
            parent_text = f"Previous message:\n{parent_author} said:\n{parent_content}"
            full_input = f"{parent_text}\n\n{author_name} asks \n{user_question}"
        else:
            full_input = f"{author_name} asks \n{user_question}"

        # Use user ID only → ensures per-user conversation memory
        ci_token = str(message.author.id)

        response = await asyncio.get_event_loop().run_in_executor(
            None, lambda: assist_client.run_assist(full_input, ci=ci_token, new=False)
        )

        for chunk in [response[i:i + 2000] for i in range(0, len(response), 2000)]:
            await message.channel.send(chunk)

    await bot.process_commands(message)


HEADERS = {"X-API-Key": API_TOKEN}
# ---- Helper: mod check decorator ----
def is_mod():
    async def predicate(ctx):
        if any(role.id in MOD_IDS for role in ctx.author.roles):
            print(f"✅ [MOD] {ctx.author} ran `{ctx.command}`")
            return True
        else:
            print(f"❌ [DENY] {ctx.author} tried `{ctx.command}`")
            return False
    return commands.check(predicate)

# ---- Commands using REST API ----
@bot.command(name="softban")
@is_mod()
async def softban(ctx, user: discord.Member = None):
    if not user:
        await ctx.send("Usage: `!softban @user`")
        return
    
    url = f"{API_URL}/api/soft_ban/{user.id}"
    try:
        resp = requests.post(url, headers=HEADERS)
        if resp.status_code == 200:
            await ctx.send(f"✅ User `{user.name}` soft-banned via API.")
        else:
            await ctx.send(f"❌ API error: {resp.json().get('error', {}).get('message', 'Unknown error')}")
    except Exception as e:
        await ctx.send(f"❌ Request failed: {e}")

@bot.command(name="timeout")
@is_mod()
async def timeout(ctx, user: discord.Member = None, duration: int = None):
    if not user or not duration:
        await ctx.send("Usage: `!timeout @user duration_in_seconds`")
        return
    
    url = f"{API_URL}/api/timeout/{user.id}/{duration}"
    try:
        resp = requests.post(url, headers=HEADERS)
        if resp.status_code == 200:
            await ctx.send(f"⏱ User `{user.name}` timed out for {duration} seconds via API.")
        else:
            await ctx.send(f"❌ API error: {resp.json().get('error', {}).get('message', 'Unknown error')}")
    except Exception as e:
        await ctx.send(f"❌ Request failed: {e}")



# ------------------------------
# HASS
# ------------------------------
# ---------------- DB connection ----------------
def get_db_connection():
    return pymysql.connect(
        host=MYSQL_HOST,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True
    )

# ---------------- HASS helpers ----------------
async def get_user_roles(member: discord.Member):
    return [r.name for r in member.roles]

async def get_available_requests(member: discord.Member):
    roles = await get_user_roles(member)
    conn = get_db_connection()
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM hass_requests ORDER BY id DESC")
        all_requests = cursor.fetchall()
    conn.close()
    # Filter by required_role if defined
    filtered = [r for r in all_requests if not r.get('required_role') or r['required_role'] in roles]
    return filtered

# ---------------- Commands ----------------
@bot.command(name="haget")
async def haget(ctx, *, request_name: str = None):
    """Fetch Home Assistant entity state for a request."""
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            if request_name:
                cursor.execute(
                    "SELECT * FROM hass_requests WHERE name LIKE %s",
                    (f"%{request_name}%",)
                )
            else:
                cursor.execute("SELECT * FROM hass_requests")
            requests_list = cursor.fetchall()
        conn.close()

        if not requests_list:
            await ctx.send("❌ No matching HASS requests found.")
            return

        messages = []
        for req in requests_list:
            url = f"{'https' if SSL else 'http'}://{HA_URL}/api/states/{req['entity_id']}"
            headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
            r = requests.get(url, headers=headers, timeout=5)
            if r.status_code == 200:
                data = r.json()
                value = data['attributes'].get(req['attribute']) if req['attribute'] else data['state']
                messages.append(f"**{req['name']}**: `{value}`")
            else:
                messages.append(f"**{req['name']}**: ❌ Failed to fetch (HTTP {r.status_code})")

        await ctx.send("\n".join(messages[:10]))  # limit to 10 messages to avoid Discord spam

    except Exception as e:
        await ctx.send(f"❌ Error: {e}")


@bot.command(name="hacall")
async def hacall(ctx, action_name: str = None, *args):
    """Call a Home Assistant action with required fields."""
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            if not action_name:
                # List available actions
                cursor.execute("SELECT name, description FROM hass_actions ORDER BY name")
                actions = cursor.fetchall()
                if not actions:
                    await ctx.send("❌ No available HASS actions.")
                    return

                msg_lines = ["**Available HASS actions:**"]
                for a in actions:
                    desc = a['description'] or "No description"
                    msg_lines.append(f"- **{a['name']}**: {desc}")
                
                # Discord messages have 2000 char limit
                for chunk_start in range(0, len(msg_lines), 20):
                    await ctx.send("\n".join(msg_lines[chunk_start:chunk_start+20]))
                return

            # Fetch action by name
            cursor.execute("SELECT * FROM hass_actions WHERE name=%s", (action_name,))
            action = cursor.fetchone()
            if not action:
                await ctx.send(f"❌ Action `{action_name}` not found.")
                return

            # Fetch fields
            cursor.execute("SELECT * FROM hass_action_fields WHERE action_id=%s ORDER BY id", (action['id'],))
            fields = cursor.fetchall()

        # Validate args
        missing_fields = []
        invalid_fields = []

        if len(args) < len(fields):
            missing_fields = [f['parameter_name'] for f in fields[len(args):]]

        # Map provided args to fields
        for f, value in zip(fields, args):
            item_id = f['item_id']
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM hass_items WHERE id=%s", (item_id,))
                item = cursor.fetchone()

            if item['type'] == 'select':
                try:
                    options = json.loads(item.get('options') or '[]')
                except json.JSONDecodeError:
                    options = [o.strip() for o in (item.get('options') or '').split(',')]
                val = value.strip().strip('"').strip("'")
                if val not in options:
                    invalid_fields.append(f"{f['parameter_name']} (invalid: {value}, options: {options})")
            elif item['type'] in ('text', 'number', 'checkbox'):
                if not value:
                    missing_fields.append(f['parameter_name'])

        if missing_fields or invalid_fields:
            msg_parts = []
            if missing_fields:
                msg_parts.append(f"⚠ Fields required for `{action_name}`:\n" + ", ".join(missing_fields))
            if invalid_fields:
                msg_parts.append(f"❌ Invalid field values:\n" + "\n".join(invalid_fields))
            await ctx.send("\n".join(msg_parts))
            return

        # Prepare payload
        payload = {}
        for f, value in zip(fields, args):
            item_id = f['item_id']
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM hass_items WHERE id=%s", (item_id,))
                item = cursor.fetchone()

            if item['type'] == 'number':
                payload[f['parameter_name']] = float(value)
            elif item['type'] == 'checkbox':
                payload[f['parameter_name']] = value.lower() in ('1', 'true', 'yes')
            else:  # text/select
                payload[f['parameter_name']] = value

        # Call HA service
        domain, service = action['ha_domain'], action['ha_service']
        url = f"http://{HA_URL}/api/services/{domain}/{service}" if not SSL else f"https://{HA_URL}/api/services/{domain}/{service}"
        headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
        r = requests.post(url, headers=headers, json=payload, timeout=10)

        if r.status_code in (200, 201):
            await ctx.send(f"✅ Action `{action_name}` executed successfully.")
        else:
            await ctx.send(f"❌ Failed to execute `{action_name}` (HTTP {r.status_code})")

    except Exception as e:
        await ctx.send(f"❌ Error executing action: {e}")



# -----------------------------
# Nextcloud calendar events (restart-safe)
# -----------------------------

SENT_FILE = "sent_notifications.json"

# Load sent notifications at startup
try:
    with open(SENT_FILE, "r") as f:
        sent_notifications = set(tuple(x) for x in json.load(f))
except Exception:
    sent_notifications = set()

def save_sent_notifications():
    try:
        with open(SENT_FILE, "w") as f:
            json.dump(list(sent_notifications), f)
    except Exception as e:
        print(f"[DEBUG][Event] Failed to save sent_notifications: {e}")

def format_event_message(title, description, start, now):
    start_utc = start.astimezone(timezone.utc)
    start_str = start_utc.strftime("%Y-%m-%d %H:%M UTC")
    delta = start - now
    total_seconds = int(delta.total_seconds())

    if total_seconds <= 0:
        relative = "NOW"
    elif total_seconds < 60:
        relative = f"in {total_seconds} seconds"
    elif total_seconds < 3600:
        minutes = total_seconds // 60
        relative = f"in {minutes} minute{'s' if minutes != 1 else ''}"
    elif total_seconds < 86400:
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        relative = f"in {hours}h {minutes}m" if minutes else f"in {hours}h"
    else:
        days = total_seconds // 86400
        relative = f"in {days} day{'s' if days != 1 else ''}"

    msg = f"⏰ Event reminder: **{title}**\n🕒 Starts {relative} at: {start_str}"
    if description:
        msg += f"\n📅 Description: {description}"
    return msg

async def _send_discord_message(discord_id, message):
    await bot.wait_until_ready()
    guild = discord.utils.get(bot.guilds, id=TARGET_GUILD_ID)
    if not guild:
        print("[DEBUG][Discord] Guild not found")
        return

    if discord_id:
        user = guild.get_member(int(discord_id))
        if user:
            try:
                await user.send(message)
                print(f"[DEBUG][Discord] Sent DM to {user.display_name}")
            except Exception as e:
                print(f"[DEBUG][Discord] Failed to send DM to {discord_id}: {e}")
    else:
        if not EVENT_CHANNEL_ID:
            print("[DEBUG][Discord] EVENT_CHANNEL_ID not set")
            return
        channel = guild.get_channel(int(EVENT_CHANNEL_ID))
        if channel:
            try:
                await channel.send(message)
                print(f"[DEBUG][Discord] Sent message to event channel #{channel.name}")
            except Exception as e:
                print(f"[DEBUG][Discord] Failed to send message to channel: {e}")

def send_discord_message(discord_id, message):
    return asyncio.run_coroutine_threadsafe(_send_discord_message(discord_id, message), bot.loop)

def check_events():
    global calendar, sent_notifications
    if not calendar:
        print("[DEBUG][Calendar] No calendar found, skipping check.")
        return

    now = datetime.now(timezone.utc)
    upcoming = now + timedelta(minutes=5)
    margin = timedelta(seconds=30)

    for event in calendar.events():
        try:
            vevent = event.vobject_instance.vevent
            title = getattr(vevent.summary, "value", "No title")
            start = getattr(vevent.dtstart, "value", None)
            if not start:
                continue
            if isinstance(start, str):
                try:
                    start = datetime.fromisoformat(start)
                except Exception:
                    continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)

            description = getattr(vevent, "description", None)
            description = description.value if description else ""

            notify_times = [(start, "Event start")]
            if hasattr(vevent, "valarm_list"):
                for idx, alarm in enumerate(vevent.valarm_list):
                    trigger = getattr(alarm.trigger, "value", None)
                    if isinstance(trigger, timedelta):
                        reminder_time = start + trigger
                        notify_times.append((reminder_time, f"VALARM {idx}"))

            event_uid = getattr(event, "uid", title)

            for notify_time, reason in notify_times:
                if (now - margin) <= notify_time <= upcoming:
                    notification_key = (event_uid, reason)
                    if notification_key in sent_notifications:
                        continue

                    message = format_event_message(title, description, start, now)

                    participants = []
                    if hasattr(vevent, "attendee_list"):
                        for att in vevent.attendee_list:
                            email = att.value
                            if email.startswith("mailto:"):
                                email = email[7:]
                            discord_id = None
                            try:
                                r = requests.get(YAP2STW_API, params={"email": email, "token": YAP2STW_TOKEN}, timeout=5)
                                data = r.json()
                                if data.get("status") == "success":
                                    discord_id = data.get("discord_id")
                            except Exception as e:
                                print(f"[DEBUG][Event] Error fetching Discord ID for {email}: {e}")
                            participants.append(discord_id)

                    for discord_id in participants:
                        if discord_id:
                            send_discord_message(discord_id, message)
                    send_discord_message(None, message)

                    sent_notifications.add(notification_key)
                    save_sent_notifications()

        except Exception as e:
            print(f"[DEBUG][Event] Exception while processing event: {e}")

# -----------------------------
# Nextcloud calendar setup
# -----------------------------
client = caldav.DAVClient(url=NEXTCLOUD_URL, username=NEXTCLOUD_USER, password=NEXTCLOUD_PASS)
principal = client.principal()
calendars = principal.calendars()
calendar = None
if CALENDAR_NAME:
    for c in calendars:
        if c.name == CALENDAR_NAME:
            calendar = c
            break
else:
    if calendars:
        calendar = calendars[0]

if not calendar:
    print("⚠ No calendar found, skipping events")

# -----------------------------
# Event checker loop
# -----------------------------
def start_event_checker():
    def loop():
        while True:
            check_events()
            time.sleep(60)
    t = threading.Thread(target=loop, daemon=True)
    t.start()



# -----------------------------
# Discord events
# -----------------------------
@bot.event
async def on_ready():
    print(f"[DEBUG] {bot.user} has connected to Discord!")
    start_event_checker()

# -----------------------------
# MOD commands via REST API
# -----------------------------
HEADERS = {"X-API-Key": API_TOKEN}
def is_mod():
    async def predicate(ctx):
        if any(role.id in MOD_IDS for role in ctx.author.roles):
            return True
        return False
    return commands.check(predicate)


# -----------------------------
# CLI list agents
# -----------------------------
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("-l", "--list-agents", action="store_true")
cli_args = parser.parse_args()
if cli_args.list_agents:
    agents = assist_client.list_agents()
    for agent in agents:
        print(f"{agent['name']} (ID: {agent['id']})")
    sys.exit(0)

# -----------------------------
# Run bot
# -----------------------------
if __name__ == "__main__":
    if not TOKEN:
        print("[ERROR] DISCORD_TOKEN not set.")
        sys.exit(1)
    print("[DEBUG] Starting Discord bot...")
    bot.run(TOKEN)
