# Personal Course Uploader Bot

## Render Deployment Steps (Docker)
1. Extract this ZIP and upload `bot.py`, `requirements.txt`, `Dockerfile`, and `README.md` to your GitHub repository.
2. Create a new **Web Service** on Render connected to your GitHub repo.
3. Choose **Runtime: Docker** and **Instance Type: Free**.
4. Add these **Environment Variables** in Render:
   - `API_ID`: Your Telegram API ID
   - `API_HASH`: Your Telegram API Hash
   - `BOT_TOKEN`: Your new Bot Token from @BotFather
   - `OWNER_ID`: `8283878643`
5. Deploy! Once live, copy your Render URL (`https://xxxx.onrender.com`) and add it to **UptimeRobot** or **BetterStack** (5-minute HTTP check) for 24/7 uptime.
