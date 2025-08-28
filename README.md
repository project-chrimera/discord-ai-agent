Discord AI Agent Bot
====================

This project is a Discord bot that connects to Home Assistant's Assist pipelines
via WebSocket and allows interaction through Discord.

Setup
-----

1. Clone the repository:
   git clone https://github.com/project-chrimera/discord-ai-agent.git
   cd discord-ai-agent

2. Create a Python virtual environment (recommended):
   python3 -m venv venv
   source venv/bin/activate

3. Install dependencies:
   pip install -r requirements.txt

4. Configure your environment:
   - Copy the provided .env.example to .env:
     cp .env.example .env
   - Edit .env and set your Discord token, guild ID, role ID, and Home Assistant details.

5. Run the bot:
   python3 bot.py

Usage
-----

- The bot listens for mentions in Discord within the configured guild.
- Only members with the specified role ID can interact with the bot.
- It uses Home Assistant Assist pipelines to generate responses.

Extra
-----

- You can list available Assist agents (pipelines) without running the bot:

  python3 bot.py --list-agents

Requirements
------------

- Python 3.9+
- Discord bot token
- Access to a Home Assistant instance with Assist enabled
