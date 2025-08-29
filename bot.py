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
import tempfile
from pathlib import Path
import websocket

# -----------------------------
# Load environment
# -----------------------------
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
TARGET_GUILD_ID = int(os.getenv("DISCORD_GUILD"))
WP_ROLE_ID = int(os.getenv("DISCORD_WP_ROLE_ID", "0"))
ASSIST_ID = os.getenv("ASSIST_ID")
HA_URL = os.getenv("HAURL")
HA_TOKEN = os.getenv("HATOKEN")
DEFAULT_AGENT = os.getenv("DEFAULT_AGENT")
SSL = os.getenv("SSL", "0") == "1"
API_URL = os.getenv("API_URL")              # REST API base URL, e.g., http://127.0.0.1:5000
API_TOKEN = os.getenv("API_TOKEN")          # REST API token for authorization
MOD_IDS = [int(x.strip()) for x in os.getenv("MOD_IDS", "").split(",") if x.strip()]


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
