#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
سیگنال‌یاب هوشمند - نسخهٔ خودکار با زرین‌پال و پشتیبانی از پلن‌های چندماهه
مالک: رحمان رحیمی
"""

import os, json, logging, requests
from datetime import datetime, timedelta
from typing import Dict, List

import ccxt, pandas as pd, numpy as np, redis
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import RedirectResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from contextlib import asynccontextmanager

# -------------------- تنظیمات از متغیرهای محیطی --------------------
TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "123456789"))
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
ZARINPAL_MERCHANT_ID = os.environ.get("ZARINPAL_MERCHANT_ID", "")
SANDBOX = os.environ.get("ZARINPAL_SANDBOX", "true").lower() == "true"
CRON_SECRET = os.environ.get("CRON_SECRET", "mysecret")

OWNER_NAME = "رحمان رحیمی"
CARD_NUMBER = "6104337889877823"
BANK_NAME = "بانک ملت"
DEFAULT_TIMEFRAME = "4h"
TOP_N_COINS = 10
SCORE_THRESHOLD_BUY = 60
SCORE_THRESHOLD_SELL = 40

TEHRAN_TZ = "Asia/Tehran"

# -------------------- تعرفه‌های اشتراک --------------------
PLANS = {
    "plan_1":  {"months": 1,  "price": 500000,  "label": "۱ ماهه - ۵۰۰ هزار تومان"},
    "plan_3":  {"months": 3,  "price": 1300000, "label": "۳ ماهه - ۱.۳ میلیون تومان"},
    "plan_6":  {"months": 6,  "price": 2500000, "label": "۶ ماهه - ۲.۵ میلیون تومان"},
    "plan_12": {"months": 12, "price": 4500000, "label": "۱ ساله - ۴.۵ میلیون تومان"},
}

# -------------------- Redis --------------------
r = redis.Redis.from_url(url=UPSTASH_URL, password=UPSTASH_TOKEN, decode_responses=True)

# -------------------- Binance --------------------
exchange = ccxt.binance({'enableRateLimit': True})

def get_top_symbols(n=TOP_N_COINS) -> List[str]:
    try:
        markets = exchange.fetch_markets()
        usdt_pairs = [m['symbol'] for m in markets if m['symbol'].endswith('/USDT') and m['active']]
        tickers = exchange.fetch_tickers()
        sorted_pairs = sorted(usdt_pairs, key=lambda s: tickers.get(s, {}).get('quoteVolume', 0) or 0, reverse=True)
        return sorted_pairs[:n]
    except:
        return ["BTC/USDT","ETH/USDT","BNB/USDT","SOL/USDT","XRP/USDT",
                "ADA/USDT","DOGE/USDT","AVAX/USDT","DOT/USDT","LINK/USDT"]

def calculate_score(symbol: str, timeframe: str) -> float:
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=100)
        df = pd.DataFrame(ohlcv, columns=['time','open','high','low','close','volume'])
        df['ema20'] = df['close'].ewm(span=20).mean()
        df['ema50'] = df['close'].ewm(span=50).mean()
        df['ema200'] = df['close'].ewm(span=200).mean()
        trend_score = 0
        if df['ema20'].iloc[-1] > df['ema50'].iloc[-1] > df['ema200'].iloc[-1]:
            trend_score = 30
        elif df['ema20'].iloc[-1] < df['ema50'].iloc[-1] < df['ema200'].iloc[-1]:
            trend_score = -20
        elif df['ema20'].iloc[-1] > df['ema50'].iloc[-1]:
            trend_score = 15
        elif df['ema20'].iloc[-1] < df['ema50'].iloc[-1]:
            trend_score = -10

        delta = df['close'].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(14).mean()
        avg_loss = loss.rolling(14).mean()
        rs = avg_gain / avg_loss
        df['rsi'] = 100 - (100/(1+rs))
        rsi = df['rsi'].iloc[-1]
        if 40 <= rsi <= 70:
            momentum_score = 20
        elif 30 <= rsi < 40:
            momentum_score = 10
        elif rsi > 70:
            momentum_score = -10
        else:
            momentum_score = -20

        df['volume_ma'] = df['volume'].rolling(20).mean()
        vol_ratio = df['volume'].iloc[-1] / df['volume_ma'].iloc[-1] if df['volume_ma'].iloc[-1] else 1
        volume_score = 20 if vol_ratio > 1.5 else (10 if vol_ratio > 1.2 else 0)

        ema12 = df['close'].ewm(span=12).mean()
        ema26 = df['close'].ewm(span=26).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9).mean()
        histogram = macd_line - signal_line
        divergence_score = 15 if histogram.iloc[-1] > 0 and histogram.iloc[-2] <= 0 else (-15 if histogram.iloc[-1] < 0 and histogram.iloc[-2] >= 0 else 0)

        df['tr'] = np.maximum(df['high']-df['low'], np.maximum(abs(df['high']-df['close'].shift(1)), abs(df['low']-df['close'].shift(1))))
        df['atr'] = df['tr'].rolling(14).mean()
        atr_ratio = df['atr'].iloc[-1] / df['close'].iloc[-1] if df['close'].iloc[-1] else 0
        volatility_score = 15 if atr_ratio < 0.02 else (5 if atr_ratio < 0.05 else -10)

        now_tehran = datetime.now(pd.Timestamp.now(tz=TEHRAN_TZ).tzinfo)
        time_score = 10 if 12 <= now_tehran.hour < 20 else 0

        return trend_score + momentum_score + volume_score + divergence_score + volatility_score + time_score
    except:
        return 0

def generate_signals(symbols, timeframe):
    results = {}
    for sym in symbols:
        score = calculate_score(sym, timeframe)
        signal = "خرید ✅" if score >= SCORE_THRESHOLD_BUY else ("فروش ❌" if score <= SCORE_THRESHOLD_SELL else "خنثی ⚪")
        results[sym] = {"score": score, "signal": signal}
    return dict(sorted(results.items(), key=lambda item: item[1]['score'], reverse=True))

def format_signal_message(signals):
    now = datetime.now(pd.Timestamp.now(tz=TEHRAN_TZ).tzinfo)
    text = f"📊 **سیگنال‌های هوشمند**\n⏰ {now.strftime('%Y-%m-%d %H:%M')}\n\n"
    for rank, (sym, data) in enumerate(signals.items(), 1):
        emoji = "🟢" if "خرید" in data['signal'] else ("🔴" if "فروش" in data['signal'] else "⚪")
        text += f"{rank}. {emoji} {sym.replace('/USDT','')}: {data['signal']} (امتیاز: {data['score']:.0f})\n"
    text += "\n⚠️ این سیگنال‌ها بر اساس تحلیل الگوریتمی صادر شده و احتمال سود و زیان دارد."
    return text

# -------------------- مدیریت کاربران --------------------
def add_user(uid, phone, full_name):
    r.hset(f"user:{uid}", mapping={
        "phone": phone,
        "full_name": full_name,
        "register_date": datetime.now().strftime("%Y-%m-%d %H:%M")
    })

def approve_user(uid, months=1):
    now = datetime.now()
    current = r.hget(f"user:{uid}", "approved_until")
    if current:
        try:
            current_expiry = datetime.strptime(current, "%Y-%m-%d %H:%M")
            start = current_expiry if current_expiry > now else now
        except:
            start = now
    else:
        start = now
    new_expiry = start + timedelta(days=30*months)
    r.hset(f"user:{uid}", "approved_until", new_expiry.strftime("%Y-%m-%d %H:%M"))

def is_user_approved(uid):
    approved = r.hget(f"user:{uid}", "approved_until")
    if approved:
        try:
            return datetime.strptime(approved, "%Y-%m-%d %H:%M") > datetime.now()
        except:
            pass
    return False

def get_user_settings(uid):
    s = r.hget(f"user:{uid}", "settings")
    if s:
        return json.loads(s)
    return {"timeframe": DEFAULT_TIMEFRAME, "symbols": None}

def set_user_settings(uid, settings):
    r.hset(f"user:{uid}", "settings", json.dumps(settings))

def get_all_approved_users():
    users = []
    for key in r.scan_iter("user:*"):
        uid = int(key.split(":")[1])
        if is_user_approved(uid):
            users.append(uid)
    return users

# -------------------- درگاه زرین‌پال --------------------
ZP_API_REQUEST = "https://api.zarinpal.com/pg/v4/payment/request.json"
ZP_API_VERIFY  = "https://api.zarinpal.com/pg/v4/payment/verify.json"
ZP_START_PAY   = "https://www.zarinpal.com/pg/StartPay/{authority}"
ZP_SANDBOX_PAY = "https://sandbox.zarinpal.com/pg/StartPay/{authority}"

if SANDBOX:
    ZP_API_REQUEST = "https://sandbox.zarinpal.com/pg/v4/payment/request.json"
    ZP_API_VERIFY  = "https://sandbox.zarinpal.com/pg/v4/payment/verify.json"

def create_payment(amount_toman: int, description: str, user_id: int):
    data = {
        "merchant_id": ZARINPAL_MERCHANT_ID,
        "amount": amount_toman,
        "description": description,
        "callback_url": f"https://{os.environ.get('VERCEL_URL', '')}/api/payment/callback?user_id={user_id}",
        "metadata": {"user_id": str(user_id)}
    }
    resp = requests.post(ZP_API_REQUEST, json=data, timeout=10)
    result = resp.json()
    if result.get("data") and result["data"]["code"] == 100:
        authority = result["data"]["authority"]
        pay_url = (ZP_SANDBOX_PAY if SANDBOX else ZP_START_PAY).format(authority=authority)
        return pay_url, authority
    return None, result.get("errors", "خطا در اتصال به زرین‌پال")

def verify_payment(authority: str, amount_toman: int) -> bool:
    data = {
        "merchant_id": ZARINPAL_MERCHANT_ID,
        "authority": authority,
        "amount": amount_toman
    }
    resp = requests.post(ZP_API_VERIFY, json=data, timeout=10)
    result = resp.json()
    if result.get("data") and result["data"]["code"] == 100:
        return True
    return False

# -------------------- ربات تلگرام --------------------
ptb_app = Application.builder().token(TOKEN).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid == ADMIN_USER_ID:
        await update.message.reply_text("🔐 ادمین: /subscribers, /approve, /remove, /broadcast")
        return
    if r.exists(f"user:{uid}"):
        if is_user_approved(uid):
            await update.message.reply_text("✅ اشتراک فعال. /settings برای تنظیمات.")
        else:
            await update.message.reply_text("⛔ اشتراک غیرفعال. /buy برای مشاهدهٔ پلن‌ها.")
        return
    await update.message.reply_text("👤 لطفاً شماره موبایل خود را به اشتراک بگذارید.",
                                    reply_markup=InlineKeyboardMarkup([[
                                        InlineKeyboardButton("📱 اشتراک‌گذاری شماره", request_contact=True)
                                    ]]))

async def contact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact = update.effective_message.contact
    uid = update.effective_user.id
    phone = contact.phone_number.replace("+", "")
    full_name = f"{contact.first_name or ''} {contact.last_name or ''}".strip()
    add_user(uid, phone, full_name)
    await context.bot.send_message(ADMIN_USER_ID, f"🔔 کاربر جدید:\nنام: {full_name}\nشماره: {phone}\nآیدی: {uid}")
    await show_plans(update, context)

async def buy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not r.exists(f"user:{uid}"):
        await update.message.reply_text("⛔ ابتدا /start را بزنید.")
        return
    await show_plans(update, context)

async def show_plans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = []
    for plan_id, plan in PLANS.items():
        keyboard.append([InlineKeyboardButton(plan["label"], callback_data=f"select_plan:{plan_id}")])
    await update.message.reply_text(
        "🎯 لطفاً طرح اشتراک مورد نظر را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def plan_selection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    plan_id = query.data.split(":")[1]
    plan = PLANS.get(plan_id)
    if not plan:
        await query.message.reply_text("❌ پلن نامعتبر.")
        return

    # ذخیره پلن انتخابی در Redis
    r.hset(f"user:{uid}", "pending_plan", json.dumps({"months": plan["months"], "amount": plan["price"]}))

    pay_url, authority = create_payment(plan["price"], f"اشتراک {plan['label']} (user {uid})", uid)
    if not pay_url:
        await query.message.reply_text(f"❌ خطا در اتصال به درگاه:\n{authority}")
        return

    r.hset(f"user:{uid}", "pending_authority", authority)
    keyboard = [[InlineKeyboardButton("🔗 رفتن به درگاه پرداخت", url=pay_url)]]
    await query.message.reply_text(
        f"💰 مبلغ قابل پرداخت: **{plan['price']:,} تومان**\n"
        f"📆 مدت اشتراک: {plan['months']} ماه\n"
        "برای پرداخت روی دکمهٔ زیر کلیک کنید:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_user_approved(uid):
        await update.message.reply_text("⛔ اشتراک شما فعال نیست.")
        return
    keyboard = [
        [InlineKeyboardButton("⏱ تغییر تایم‌فریم", callback_data="set_timeframe")],
        [InlineKeyboardButton("🪙 تغییر ارزها", callback_data="set_symbols")],
    ]
    await update.message.reply_text("⚙️ تنظیمات:", reply_markup=InlineKeyboardMarkup(keyboard))

async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "set_timeframe":
        keyboard = [
            [InlineKeyboardButton("۱۵ دقیقه", callback_data="tf_15m"),
             InlineKeyboardButton("۱ ساعت", callback_data="tf_1h")],
            [InlineKeyboardButton("۴ ساعت", callback_data="tf_4h"),
             InlineKeyboardButton("روزانه", callback_data="tf_1d")],
        ]
        await query.message.reply_text("⏱ تایم‌فریم تحلیل:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif query.data == "set_symbols":
        await query.message.reply_text("🪙 نماد ارزها را با فرمت BTC/USDT,ETH/USDT بفرستید.\n(برای ۱۰ ارز برتر، کلمه TOP را بفرستید.)")
        context.user_data["awaiting_symbols"] = True

async def timeframe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tf = query.data.split("_")[1]
    uid = query.from_user.id
    settings = get_user_settings(uid)
    settings["timeframe"] = tf
    set_user_settings(uid, settings)
    await query.message.reply_text(f"✅ تایم‌فریم به {tf} تغییر کرد.")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()
    if context.user_data.get("awaiting_symbols"):
        if text.upper() == "TOP":
            settings = get_user_settings(uid)
            settings["symbols"] = None
            set_user_settings(uid, settings)
            await update.message.reply_text("✅ از ۱۰ ارز برتر بازار استفاده می‌شود.")
        else:
            symbols = [s.strip() for s in text.split(",") if s.strip().endswith("/USDT")]
            if symbols:
                settings = get_user_settings(uid)
                settings["symbols"] = symbols
                set_user_settings(uid, settings)
                await update.message.reply_text(f"✅ ارزهای انتخابی: {', '.join(symbols)}")
            else:
                await update.message.reply_text("❌ فرمت اشتباه. مثال: BTC/USDT,ETH/USDT")
        context.user_data["awaiting_symbols"] = False
        return

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📸 در نسخهٔ جدید نیازی به ارسال فیش نیست. لطفاً از دکمهٔ پرداخت آنلاین استفاده کنید.")

# -------------------- دستورات ادمین --------------------
async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: return
    try:
        uid = int(context.args[0])
        months = int(context.args[1]) if len(context.args) > 1 else 1
        approve_user(uid, months)
        await update.message.reply_text(f"✅ کاربر {uid} برای {months} ماه تأیید شد.")
        await context.bot.send_message(chat_id=uid, text="🎉 اشتراک شما فعال شد!")
    except Exception as e:
        await update.message.reply_text(f"❌ خطا. فرمت: /approve user_id months")

async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: return
    try:
        uid = int(context.args[0])
        r.delete(f"user:{uid}")
        await update.message.reply_text(f"✅ کاربر {uid} حذف شد.")
    except:
        await update.message.reply_text("فرمت: /remove user_id")

async def subscribers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: return
    users = get_all_approved_users()
    if not users:
        await update.message.reply_text("هیچ کاربر فعالی وجود ندارد.")
        return
    msg = "👥 کاربران فعال:\n\n"
    for uid in users:
        info = r.hgetall(f"user:{uid}")
        name = info.get("full_name", "نامشخص")
        phone = info.get("phone", "")
        msg += f"• {name} - {phone} (ID: {uid})\n"
    await update.message.reply_text(msg)

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: return
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("متنی بنویسید: /broadcast متن")
        return
    for uid in get_all_approved_users():
        try:
            await context.bot.send_message(chat_id=uid, text=text)
        except:
            pass
    await update.message.reply_text("✅ ارسال شد.")

# ثبت هندلرها
ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(CommandHandler("buy", buy_cmd))
ptb_app.add_handler(CommandHandler("settings", settings_command))
ptb_app.add_handler(CommandHandler("approve", approve_cmd))
ptb_app.add_handler(CommandHandler("remove", remove_cmd))
ptb_app.add_handler(CommandHandler("subscribers", subscribers_cmd))
ptb_app.add_handler(CommandHandler("broadcast", broadcast_cmd))
ptb_app.add_handler(MessageHandler(filters.CONTACT, contact_handler))
ptb_app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
ptb_app.add_handler(CallbackQueryHandler(plan_selection_callback, pattern=r"^select_plan:"))
ptb_app.add_handler(CallbackQueryHandler(settings_callback, pattern="set_timeframe|set_symbols"))
ptb_app.add_handler(CallbackQueryHandler(timeframe_callback, pattern=r"tf_.*"))
ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

# -------------------- FastAPI --------------------
app = FastAPI()

@asynccontextmanager
async def lifespan(app: FastAPI):
    await ptb_app.initialize()
    webhook_url = f"https://{os.environ.get('VERCEL_URL', '')}/api/telegram"
    await ptb_app.bot.set_webhook(webhook_url)
    logging.info(f"Webhook set to {webhook_url}")
    yield
    await ptb_app.shutdown()

app.lifespan = lifespan

@app.post("/api/telegram")
async def telegram(request: Request):
    data = await request.json()
    update = Update.de_json(data, ptb_app.bot)
    await ptb_app.process_update(update)
    return {"status": "ok"}

@app.get("/api/payment/callback")
async def payment_callback(
    request: Request,
    Authority: str = Query(...),
    Status: str = Query(...),
    user_id: str = Query(...)
):
    if Status != "OK":
        return RedirectResponse(url="https://t.me/your_support_username")
    uid = int(user_id)
    # بازیابی پلن ذخیره‌شده
    pending_plan_json = r.hget(f"user:{uid}", "pending_plan")
    if not pending_plan_json:
        await ptb_app.bot.send_message(chat_id=uid, text="❌ اطلاعات پرداخت یافت نشد. لطفاً دوباره اقدام کنید.")
        return RedirectResponse(url="https://t.me/RahimiSignalBot")
    pending_plan = json.loads(pending_plan_json)
    amount = pending_plan["amount"]
    months = pending_plan["months"]

    if verify_payment(Authority, amount):
        approve_user(uid, months)
        # پاک کردن داده‌های موقت
        r.hdel(f"user:{uid}", "pending_plan", "pending_authority")
        await ptb_app.bot.send_message(chat_id=uid, text=f"✅ پرداخت شما تأیید شد. اشتراک {months} ماهه فعال گردید!")
        await ptb_app.bot.send_message(ADMIN_USER_ID, f"💰 پرداخت موفق {amount:,} تومان توسط کاربر {uid} - طرح {months} ماهه")
        return RedirectResponse(url="https://t.me/RahimiSignalBot?start=paid")
    else:
        await ptb_app.bot.send_message(chat_id=uid, text="❌ پرداخت ناموفق. لطفاً دوباره تلاش کنید.")
        return RedirectResponse(url="https://t.me/RahimiSignalBot?start=failed")

@app.post("/api/cron")
async def cron_signals(request: Request):
    auth = request.headers.get("Authorization")
    if auth != f"Bearer {CRON_SECRET}":
        raise HTTPException(status_code=403)
    for uid in get_all_approved_users():
        try:
            settings = get_user_settings(uid)
            tf = settings.get("timeframe", DEFAULT_TIMEFRAME)
            syms = settings.get("symbols") or get_top_symbols()
            signals = generate_signals(syms, tf)
            msg = format_signal_message(signals)
            await ptb_app.bot.send_message(chat_id=uid, text=msg, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Signal error {uid}: {e}")
    for uid in get_all_approved_users():
        approved = r.hget(f"user:{uid}", "approved_until")
        if approved:
            try:
                expiry = datetime.strptime(approved, "%Y-%m-%d %H:%M")
                if expiry - datetime.now() <= timedelta(days=3):
                    await ptb_app.bot.send_message(uid, "⏳ تنها ۳ روز تا پایان اشتراک مانده. تمدید کنید: /buy")
            except:
                pass
    return {"status": "ok"}
