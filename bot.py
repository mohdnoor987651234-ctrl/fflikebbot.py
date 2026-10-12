import base64
import json
import asyncio
import aiohttp
import os
import threading
import random
import urllib.parse
import requests
import binascii
from datetime import datetime, date, time
from collections import defaultdict

from flask import Flask, request, jsonify
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from google.protobuf.json_format import MessageToJson

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

# Protobuf files check
try:
    import like_pb2
    import like_count_pb2
    import uid_generator_pb2
except ImportError:
    pass

# ============ UNIFIED FLASK SERVER (API + KEEPALIVE) ============
app_flask = Flask('')

@app_flask.route('/')
def home():
    return "Bot & Like API are active and running!"

# --- Like API Logic ---
KEY_LIMIT = 90
liked_cache = defaultdict(set)
token_cache = {}
TOKEN_MAX_AGE = 86400  # 24 hours

def load_accounts(server_name):
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        json_filename = os.path.join(base_dir, f"accounts-{server_name.upper()}.json")
        if os.path.exists(json_filename):
            with open(json_filename, "r", encoding="utf-8") as f:
                data = json.load(f)
                return [{"uid": str(item["uid"]), "password": item.get("password", ""), "token": item.get("jwt_token")} for item in data]
        
        if server_name == "IND":
            filename = "account_ind.txt"
        elif server_name in {"BR", "US", "SAC", "NA"}:
            filename = "account_br.txt"
        else:
            filename = "account_bd.txt"
                
        filepath = os.path.join(base_dir, filename)
        if not os.path.exists(filepath):
            filepath = os.path.join(base_dir, "account_ind.txt")
            if not os.path.exists(filepath):
                return []
                
        accounts = []
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if ':' in line:
                    parts = line.split(':', 1)
                    uid, password = parts[0].strip(), parts[1].strip()
                    if uid and password:
                        accounts.append({"uid": uid, "password": password, "token": None})
        return accounts
    except Exception as e:
        print(f"[-] Error loading accounts: {e}")
        return []

async def generate_jwt_token(uid, password):
    try:
        encoded_password = urllib.parse.quote(password)
        url = f"https://all-in-one-jwt.vercel.app/token?uid={uid}&password={encoded_password}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=24) as response:
                if response.status == 200:
                    data = await response.json()
                    if isinstance(data, dict):
                        return data.get('jwt_token') or data.get('token')
        return None
    except:
        return None

def encrypt_message(plaintext):
    key = b'Yg&tc%DEuh6%Zc^8'
    iv = b'6oyZDr22E3ychjM%'
    cipher = AES.new(key, AES.MODE_CBC, iv)
    padded_message = pad(plaintext, AES.block_size)
    return binascii.hexlify(cipher.encrypt(padded_message)).decode('utf-8')

def create_protobuf_message(user_id, region):
    message = like_pb2.like()
    message.uid = int(user_id)
    message.region = region
    return message.SerializeToString()

async def send_like(encrypted_uid, token, url):
    try:
        edata = bytes.fromhex(encrypted_uid)
        headers = {
            'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
            'Authorization': f"Bearer {token}",
            'Content-Type': "application/x-www-form-urlencoded",
            'X-GA': "v1 1",
            'ReleaseVersion': "OB55"
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=edata, headers=headers, timeout=5) as response:
                return response.status
    except:
        return 500

async def get_valid_token(uid, password):
    now = time.time()
    if uid in token_cache:
        token, cached_at = token_cache[uid]
        if now - cached_at < TOKEN_MAX_AGE:
            return token
    fresh_token = await generate_jwt_token(uid, password)
    if fresh_token:
        token_cache[uid] = (fresh_token, now)
        return fresh_token
    if uid in token_cache:
        return token_cache[uid][0]
    return None

async def process_account(target_uid, encrypted_uid, account, url, semaphore, server_name):
    async with semaphore:
        token = account.get('token')
        if not token:
            token = await get_valid_token(account['uid'], account['password'])
        if not token:
            return 500, account['uid']
        status = await send_like(encrypted_uid, token, url)
        if status == 200:
            liked_cache[target_uid].add(account['uid'])
        return status, account['uid']

async def send_all_likes(target_uid, server_name, url):
    region = server_name
    protobuf_message = create_protobuf_message(target_uid, region)
    encrypted_uid = encrypt_message(protobuf_message)
    
    accounts = load_accounts(server_name)
    if not accounts:
        return {'success': 0, 'failed': 0, 'total': 0}
        
    already_liked = liked_cache.get(target_uid, set())
    fresh_accounts = [acc for acc in accounts if acc['uid'] not in already_liked]
    if not fresh_accounts:
        return {'success': 0, 'failed': 0, 'total': len(accounts)}
        
    random.shuffle(fresh_accounts)
    semaphore = asyncio.Semaphore(25)
    tasks = [process_account(target_uid, encrypted_uid, acc, url, semaphore, server_name) for acc in fresh_accounts[:2000]]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    successful = sum(1 for r in results if isinstance(r, tuple) and r[0] == 200)
    failed = sum(1 for r in results if isinstance(r, tuple) and r[0] != 200)
    return {'success': successful, 'failed': failed, 'total': len(accounts)}

def enc(uid):
    message = uid_generator_pb2.uid_generator()
    message.krishna_ = int(uid)
    message.teamXdarks = 1
    return encrypt_message(message.SerializeToString())

def decode_protobuf(binary):
    try:
        items = like_count_pb2.Info()
        items.ParseFromString(binary)
        return items
    except:
        return None

def get_player_info(encrypted_uid, server_name, token):
    if server_name == "IND":
        url = "https://client.ind.freefiremobile.com/GetPlayerPersonalShow"
    elif server_name in {"BR", "US", "SAC", "NA"}:
        url = "https://client.us.freefiremobile.com/GetPlayerPersonalShow"
    else:
        url = "https://clientbp.ggpolarbear.com/GetPlayerPersonalShow"
    edata = bytes.fromhex(encrypted_uid)
    headers = {
        'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
        'Authorization': f"Bearer {token}",
        'Content-Type': "application/x-www-form-urlencoded",
        'X-GA': "v1 1",
        'ReleaseVersion': "OB55"
    }
    try:
        response = requests.post(url, data=edata, headers=headers, verify=False, timeout=10)
        return decode_protobuf(response.content)
    except:
        return None

@app_flask.route('/like', methods=['GET'])
def handle_requests():
    uid = request.args.get("uid")
    server_name = request.args.get("server_name") or request.args.get("region", "").upper()
    if not uid or not server_name:
        return jsonify({"error": "UID and server_name are required"}), 400

    accounts = load_accounts(server_name)
    if not accounts:
        accounts = load_accounts("IND")
        if not accounts:
            return jsonify({"error": "No accounts found", "status": 0}), 200

    encrypted_uid = enc(uid)
    check_token = None
    before = None
    
    for account in accounts[:10]:
        token = account.get('token')
        if not token:
            token = asyncio.run(get_valid_token(account['uid'], account['password']))
        if token:
            before = get_player_info(encrypted_uid, server_name, token)
            if before is not None:
                check_token = token
                break

    if before is None or not check_token:
        return jsonify({"error": "Invalid UID or server", "status": 0}), 200

    try:
        before_data = json.loads(MessageToJson(before))
        before_like = int(before_data['AccountInfo'].get('Likes', 0))
    except:
        return jsonify({"error": "Data parsing failed", "status": 0}), 200

    if server_name == "IND":
        like_url = "https://client.ind.freefiremobile.com/LikeProfile"
    elif server_name in {"BR", "US", "SAC", "NA"}:
        like_url = "https://client.us.freefiremobile.com/LikeProfile"
    else:
        like_url = "https://clientbp.ggpolarbear.com/LikeProfile"

    asyncio.run(send_all_likes(uid, server_name, like_url))
    
    after = get_player_info(encrypted_uid, server_name, check_token)
    if after is None:
        return jsonify({"error": "Could not verify likes", "status": 0}), 200

    try:
        after_data = json.loads(MessageToJson(after))
        after_like = int(after_data['AccountInfo'].get('Likes', 0))
        player_id = int(after_data['AccountInfo']['UID'])
        player_name = str(after_data['AccountInfo']['PlayerNickname'])
        like_given = after_like - before_like
        status = 1 if like_given != 0 else 2
        return jsonify({
            "LikesGivenByAPI": like_given,
            "LikesafterCommand": after_like,
            "LikesbeforeCommand": before_like,
            "PlayerNickname": player_name,
            "UID": player_id,
            "status": status,
        })
    except Exception as e:
        return jsonify({"error": str(e), "status": 0}), 500

# ============ CONFIGURATION ============
PORT = int(os.environ.get('PORT', 10000))
API_URL = f"http://127.0.0.1:{PORT}/"
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8988399911:AAEAOVXEMmo_xhKqC_NP_RkH7vP8QWtczY8")
ADMIN_IDS = [int(i.strip()) for i in os.environ.get("ADMIN_IDS", "8438207365").split(",") if i.strip()]

DATA_FILES = {
    'allowed': 'allowed_groups.json',
    'stats': 'daily_stats.json',
    'users': 'user_limits.json',
    'config': 'bot_config.json'
}

bot_status = "on"
bot_mode = "public"
allowed_groups = {}
daily_stats = {}
user_limits = {}
daily_limit = 2

def load_data():
    global allowed_groups, daily_stats, user_limits, bot_status, bot_mode, daily_limit
    try:
        with open(DATA_FILES['allowed'], 'r') as f: allowed_groups = json.load(f)
    except: allowed_groups = {}
    try:
        with open(DATA_FILES['stats'], 'r') as f: daily_stats = json.load(f)
    except: daily_stats = {}
    try:
        with open(DATA_FILES['users'], 'r') as f: user_limits = json.load(f)
    except: user_limits = {}
    try:
        with open(DATA_FILES['config'], 'r') as f:
            cfg = json.load(f)
            bot_status = cfg.get('status', 'on')
            bot_mode = cfg.get('mode', 'public')
            daily_limit = cfg.get('limit', 2)
    except:
        bot_status, bot_mode, daily_limit = 'on', 'public', 2

def save_all():
    with open(DATA_FILES['allowed'], 'w') as f: json.dump(allowed_groups, f, indent=2)
    with open(DATA_FILES['stats'], 'w') as f: json.dump(daily_stats, f, indent=2)
    with open(DATA_FILES['users'], 'w') as f: json.dump(user_limits, f, indent=2)
    with open(DATA_FILES['config'], 'w') as f: json.dump({'status': bot_status, 'mode': bot_mode, 'limit': daily_limit}, f, indent=2)

def is_admin(uid): return uid in ADMIN_IDS
def today_str(): return str(date.today())

def can_user_like(uid):
    if is_admin(uid): return True
    t = today_str()
    if uid not in user_limits or user_limits[uid]['date'] != t:
        user_limits[uid] = {'date': t, 'count': 0}
        return True
    return user_limits[uid]['count'] < daily_limit

def update_user_like(uid):
    if is_admin(uid): return
    t = today_str()
    if uid not in user_limits or user_limits[uid]['date'] != t:
        user_limits[uid] = {'date': t, 'count': 0}
    user_limits[uid]['count'] += 1
    if t not in daily_stats:
        daily_stats[t] = {'total': 0, 'users': {}}
    daily_stats[t]['total'] += 1
    uid_str = str(uid)
    if uid_str not in daily_stats[t]['users']:
        daily_stats[t]['users'][uid_str] = 0
    daily_stats[t]['users'][uid_str] += 1
    save_all()

async def call_like_api(region, uid):
    try:
        url = f"{API_URL}like?uid={uid}&server_name={region}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    return {"error": f"HTTP {resp.status}"}
    except Exception as e:
        return {"error": str(e)}

def is_group_allowed(chat_id, chat_type):
    if chat_type == "private" or bot_mode == "public":
        return True
    return str(chat_id) in allowed_groups

async def reset_midnight(context: ContextTypes.DEFAULT_TYPE):
    global user_limits
    user_limits = {}
    save_all()

async def reply(update, text):
    await update.message.reply_text(text, parse_mode='Markdown')

# ============ TELEGRAM COMMANDS ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_type = update.effective_chat.type
    if chat_type == "private":
        welcome_msg = (
            "📖 *𝑪𝑶𝑴𝑴𝑨𝑵𝑫 𝑳𝑰𝑺𝑻*\n\n"
            "🔹 `/like REGION UID` – get like (any region)\n"
            "🔹 `/info` – Check your remaining likes\n\n"
            "*Example:* `/like IND 9786176569`\n\n"
            "Power by @NOORXMODS"
        )
        await reply(update, welcome_msg)
        return

    if bot_status == "off":
        await reply(update, "🔴 *Bot is currently OFF*")
        return
    msg = (
        "✨ *𝑭𝑹𝑬𝑬 𝑭𝑰𝑹𝑬 𝑳𝑰𝑲𝑬 𝑩𝑶𝑻* ✨\n"
        "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
        "💬 `/like REGION UID` – Send a like\n"
        "💬 `/help` – Show all commands\n"
        "💬 `/info` – Your remaining likes\n\n"
        "📌 *Example:* `/like IND 14160011100`\n"
        f"🔥 Your daily limit: `{daily_limit}` likes\n"
        "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯"
    )
    await reply(update, msg)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if bot_status == "off":
        await reply(update, "🔴 Bot is OFF")
        return
    user_id = update.effective_user.id
    msg = (
        "📖 *𝑪𝑶𝑴𝑴𝑨𝑵𝑫 𝑳𝑰𝑺𝑻*\n\n"
        "🔹 `/like REGION UID` – Send 1 like (any region)\n"
        "🔹 `/info` – Check your remaining likes\n"
        "🔹 `/start` – Welcome message\n\n"
        "*Example:* `/like IND 1234567890`\n"
        "*Supports any region* – just type the code."
    )
    if is_admin(user_id):
        msg += (
            "\n\n👑 *Admin commands:*\n"
            "`/allow` – Allow current group\n"
            "`/off` / `/on` – Turn bot off/on\n"
            "`/stats` – Today's usage\n"
            "`/setprivate` / `/setpublic` – Change group mode\n"
            "`/setlimit <num>` – Set daily limit per user"
        )
    await reply(update, msg)

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if bot_status == "off":
        await reply(update, "🔴 Bot is OFF")
        return
    uid = update.effective_user.id
    if is_admin(uid):
        await reply(update, "👑 *Admin Account*\n🔥 *Unlimited likes* – No daily limit.")
        return
    t = today_str()
    used = user_limits.get(uid, {}).get('count', 0) if uid in user_limits and user_limits[uid]['date'] == t else 0
    remaining = daily_limit - used
    msg = (
        "🤖 *𝑩𝑶𝑻 𝑰𝑵𝑭𝑶*\n\n"
        f"⚙️ Mode: `{bot_mode.upper()}`\n"
        f"🟢 Status: `{bot_status.upper()}`\n"
        f"📅 Daily limit: `{daily_limit}` likes\n"
        f"✅ Used today: `{used}`\n"
        f"🟢 Remaining: `{remaining}`\n"
        f"👥 Allowed groups: `{len(allowed_groups)}`"
    )
    await reply(update, msg)

async def like_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if bot_status == "off":
        await reply(update, "🔴 *Bot is currently OFF*")
        return
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type
    if chat_type != "private" and not is_group_allowed(chat_id, chat_type):
        await reply(update, "🚫 *This bot works only in allowed groups!*")
        return
    if len(context.args) != 2:
        await reply(update, "❌ *Usage:* `/like REGION UID`\nExample: `/like IND 14160011100`")
        return
    region = context.args[0].upper()
    uid = context.args[1]
    if not uid.isdigit():
        await reply(update, "❌ *UID must contain only numbers!*")
        return
    user_id = update.effective_user.id
    if not can_user_like(user_id):
        used = user_limits.get(user_id, {}).get('count', 0)
        await reply(update, f"⚠️ *Daily limit reached!*\nYou have used `{used}/{daily_limit}` likes today.")
        return
    proc_msg = await update.message.reply_text(f"🔄 *Processing...*\nUID: `{uid}`\nRegion: `{region}`", parse_mode='Markdown')
    data = await call_like_api(region, uid)
    if data is None or "error" in data:
        error_msg = data.get("error", "Unknown error") if data else "No response"
        await proc_msg.edit_text(f"❌ *API Error!*\n{error_msg}", parse_mode='Markdown')
        return
    status = data.get('status')
    if status is None:
        await proc_msg.edit_text("❌ *Invalid API response*", parse_mode='Markdown')
        return
    player = data.get('PlayerNickname', 'Unknown')
    uid_resp = data.get('UID', uid)
    region_resp = data.get('Region', region)
    before = data.get('LikesbeforeCommand', 0)
    after = data.get('LikesafterCommand', 0)
    given = data.get('LikesGivenByAPI', 0)
    if status == 1:
        update_user_like(user_id)
        user_used = user_limits.get(user_id, {}).get('count', 0) if not is_admin(user_id) else "Unlimited"
        result = (
            f"✅ *𝑳𝑰𝑲𝑬 𝑺𝑬𝑵𝑻* ✅\n\n"
            f"👤 *Player:* {player}\n"
            f"🏷️ *UID:* `{uid_resp}`\n"
            f"🌍 *Region:* {region_resp}\n"
            f"❤️ *Likes given:* +{given}\n"
            f"📊 *Total:* {before} → {after}\n\n"
            f"📅 *Your usage:* {user_used}/{daily_limit if not is_admin(user_id) else '∞'}\n"
            f"🎉 *Success!*"
        )
    elif status == 2:
        result = (
            f"⚠️ *𝑳𝑰𝑴𝑰𝑻 𝑹𝑬𝑨𝑪𝑯𝑬𝑫* ⚠️\n\n"
            f"👤 *Player:* {player}\n"
            f"🏷️ *UID:* `{uid_resp}`\n"
            f"📊 *Total likes:* {before} (unchanged)\n"
            f"😔 *Could not send like to {player}*"
        )
    else:
        result = f"❓ *Unknown API response*"
    await proc_msg.edit_text(result, parse_mode='Markdown')

# Admin Commands
async def allow_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    chat = update.effective_chat
    if chat.type == "private": return
    allowed_groups[str(chat.id)] = {'name': chat.title}
    save_all()
    await reply(update, f"✅ *Group allowed:* {chat.title}")

async def off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    global bot_status
    bot_status = "off"
    save_all()
    await reply(update, "🔴 *Bot is now OFF*")

async def on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    global bot_status
    bot_status = "on"
    save_all()
    await reply(update, "🟢 *Bot is now ON*")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    t = today_str()
    if t not in daily_stat
