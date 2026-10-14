#!/usr/bin/env python3
"""
Nobitex Portfolio — ربات تلگرام
همان منطق ابزار وب (سیگنال اسپات، تأیید قبل از سفارش، PnL، کیف‌پول).

راه‌اندازی:
  1) از @BotFather یک ربات بساز و توکن را بگیر
  2) آیدی عددی تلگرام خودت را پیدا کن (مثلاً از @userinfobot)
  3) در telegram_config.json بگذار:
       {"token": "TOKEN", "admin_id": 123456789}
     فقط همین آیدی می‌تواند از ربات استفاده کند — کسی با /start مالک نمی‌شود.
  4) python telegram_bot.py

نیاز: فقط پایتون ۳.۷+ (بدون pip). از main.py و ed25519_pure استفاده می‌کند.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import main as np  # noqa: E402  — منطق مشترک با نسخه وب

CONFIG_FILE = BASE_DIR / "telegram_config.json"
TG_API = "https://api.telegram.org/bot{token}/{method}"

# وضعیت داخلی ربات تلگرام
_offset = 0
_stop = threading.Event()
_last_pending_ids = set()
_last_notif_count = 0


# ───────────────────────── config ─────────────────────────
def load_tg_config():
    cfg = {
        "token": os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        "admin_id": None,   # آیدی عددی ادمین (اجباری) — فقط این شخص دسترسی دارد
        "chat_ids": [],     # اختیاری: لیست اضافی مجاز (علاوه بر admin_id)
        "notify_signals": True,
        "notify_pending": True,
    }
    env_admin = os.environ.get("TELEGRAM_ADMIN_ID", "").strip()
    if env_admin:
        try:
            cfg["admin_id"] = int(env_admin)
        except ValueError:
            pass
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            if data.get("token"):
                cfg["token"] = str(data["token"]).strip()
            # admin_id یا chat_id تکی
            for key in ("admin_id", "chat_id"):
                if data.get(key) is not None and str(data.get(key)).strip() != "":
                    try:
                        cfg["admin_id"] = int(data[key])
                    except (TypeError, ValueError):
                        pass
                    break
            ids = []
            if isinstance(data.get("chat_ids"), list):
                for x in data["chat_ids"]:
                    try:
                        ids.append(int(x))
                    except (TypeError, ValueError):
                        pass
            cfg["chat_ids"] = ids
            if "notify_signals" in data:
                cfg["notify_signals"] = bool(data["notify_signals"])
            if "notify_pending" in data:
                cfg["notify_pending"] = bool(data["notify_pending"])
    except Exception:
        pass
    return cfg


def save_tg_config(cfg):
    out = {
        "token": cfg.get("token") or "",
        "admin_id": cfg.get("admin_id"),
        "chat_ids": list(cfg.get("chat_ids") or []),
        "notify_signals": bool(cfg.get("notify_signals", True)),
        "notify_pending": bool(cfg.get("notify_pending", True)),
    }
    CONFIG_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except Exception:
        pass


def allowed_ids(cfg: dict) -> set:
    """مجموعه آیدی‌های مجاز: فقط admin_id + chat_ids از config (بدون ثبت خودکار)."""
    ids = set()
    admin = cfg.get("admin_id")
    if admin is not None:
        try:
            ids.add(int(admin))
        except (TypeError, ValueError):
            pass
    for x in cfg.get("chat_ids") or []:
        try:
            ids.add(int(x))
        except (TypeError, ValueError):
            pass
    return ids


def is_admin(chat_id: int, cfg: dict) -> bool:
    """فقط آیدی‌های تعریف‌شده در config اجازه دارند. کسی با /start مالک نمی‌شود."""
    allowed = allowed_ids(cfg)
    if not allowed:
        return False
    return int(chat_id) in allowed


# ───────────────────────── Telegram API ─────────────────────────
def tg_request(token: str, method: str, payload: dict | None = None, timeout: int = 35):
    url = TG_API.format(token=token, method=method)
    data = None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return {"ok": False, "description": str(e)}
    except Exception as e:
        return {"ok": False, "description": str(e)}


def send_message(token: str, chat_id: int, text: str, reply_markup=None, parse_mode="HTML"):
    payload = {
        "chat_id": chat_id,
        "text": text[:4000],
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return tg_request(token, "sendMessage", payload)


def answer_callback(token: str, callback_id: str, text: str = "", show_alert: bool = False):
    return tg_request(token, "answerCallbackQuery", {
        "callback_query_id": callback_id,
        "text": text[:200],
        "show_alert": show_alert,
    })


def edit_message(token: str, chat_id: int, message_id: int, text: str, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return tg_request(token, "editMessageText", payload)


def broadcast(cfg: dict, text: str, reply_markup=None):
    token = cfg.get("token") or ""
    for cid in allowed_ids(cfg):
        send_message(token, int(cid), text, reply_markup=reply_markup)


# ───────────────────────── قالب پیام‌ها ─────────────────────────
def fmt_num(n, digits=0):
    try:
        n = float(n)
    except Exception:
        return str(n)
    if digits == 0:
        return f"{n:,.0f}"
    return f"{n:,.{digits}f}"


def msg_help():
    return (
        "🤖 <b>Nobitex Portfolio Bot</b>\n\n"
        "دستورات:\n"
        "/start — شروع و راهنما\n"
        "/balance — موجودی تومانی + خلاصه کیف\n"
        "/portfolio — لیست دارایی‌ها\n"
        "/signals — آخرین سیگنال‌های اسکن\n"
        "/pending — پیشنهادهای در انتظار تأیید\n"
        "/bot_on — روشن کردن ربات اسپات (سیگنال + تأیید)\n"
        "/bot_off — خاموش کردن ربات\n"
        "/status — وضعیت ربات و تنظیمات\n"
        "/setkey — ثبت API Key + Private Key\n"
        "/settoken — ثبت Token کلاسیک نوبیتکس\n"
        "/clearcred — پاک کردن کلیدها\n"
        "/help — همین راهنما\n\n"
        "🔑 کلیدها فقط روی همین سرور در <code>credentials.json</code> ذخیره می‌شوند.\n"
        "✅ سفارش خرید فقط بعد از تأیید شما (دکمه زیر پیام) اجرا می‌شود.\n"
        "⚠ اگر موجودی کم باشد سیگنال باز هم می‌آید؛ موقع تأیید در صورت کمبود موجودی اجرا نمی‌شود."
    )


def msg_balance():
    wallets, err = np.fetch_user_wallets_server()
    if err:
        return f"❌ خطا دریافت کیف: {err}"
    toman = np.parse_toman_balance(wallets)
    held = np.held_currencies_from_wallets(wallets)
    lines = [
        f"💰 <b>موجودی تومان:</b> {fmt_num(toman)} ت",
        f"📦 ارزهای نگهداری‌شده: {', '.join(x.upper() for x in sorted(held)) or '—'}",
        "",
    ]
    amounts = np.wallet_amount_map(wallets, min_value_toman=1000)
    if amounts:
        lines.append("<b>دارایی‌های قابل توجه:</b>")
        for cur, bal in sorted(amounts.items(), key=lambda x: -x[1])[:15]:
            lines.append(f"• {cur.upper()}: {bal}")
    else:
        lines.append("دارایی قابل توجهی یافت نشد.")
    return "\n".join(lines)


def msg_portfolio():
    wallets, err = np.fetch_user_wallets_server()
    if err:
        return f"❌ خطا: {err}"
    if not wallets:
        return "کیف خالی است یا کلید تنظیم نشده."
    lines = ["📊 <b>پرتفوی</b>\n"]
    for w in wallets:
        cur = (w.get("currency") or w.get("currencyCode") or "").lower()
        if not cur:
            continue
        try:
            bal = float(w.get("activeBalance") or w.get("balance") or 0)
        except Exception:
            bal = 0
        if bal <= 0:
            continue
        rial = w.get("rialBalanceSell") or w.get("rialBalance")
        extra = ""
        if rial is not None:
            try:
                extra = f" ≈ {fmt_num(float(rial) / 10)} ت"
            except Exception:
                pass
        lines.append(f"• <b>{cur.upper()}</b>: {bal}{extra}")
    if len(lines) == 1:
        return "هیچ موجودی فعالی نیست."
    return "\n".join(lines)


def msg_signals():
    b = np.load_bot()
    sig = b.get("rt_last_signal") or b.get("last_signal") or {}
    if not sig:
        return "هنوز سیگنالی محاسبه نشده. ربات را با /bot_on روشن کنید."
    lines = [
        f"📡 <b>آخرین اسکن</b> — {sig.get('ts') or '—'}",
        f"سیگنال: <b>{(sig.get('signal') or 'hold').upper()}</b>"
        + (f" | {str(sig.get('src') or '').upper()}" if sig.get("src") else ""),
    ]
    if sig.get("reason"):
        lines.append(f"دلیل: {sig['reason']}")
    if sig.get("toman") is not None:
        lines.append(f"موجودی هنگام اسکن: {fmt_num(sig['toman'])} ت")
    if sig.get("scan_count") is not None:
        lines.append(f"اسکن: {sig.get('scan_count')}/{sig.get('scan_total', '?')}")
    top = sig.get("top") or []
    if top:
        lines.append("\n<b>برترین‌ها:</b>")
        for c in top[:8]:
            lines.append(
                f"• {str(c.get('src') or '').upper()} | {c.get('signal')} | "
                f"خرید {c.get('buy_votes')} / فروش {c.get('sell_votes')} | RSI {c.get('rsi')}"
            )
    return "\n".join(lines)


def pending_keyboard(pid: str):
    return {
        "inline_keyboard": [[
            {"text": "✅ تأیید و ارسال سفارش", "callback_data": f"confirm:{pid}"},
            {"text": "❌ رد", "callback_data": f"reject:{pid}"},
        ]]
    }


def format_pending_item(p: dict) -> str:
    side = "خرید" if p.get("side") == "buy" else "فروش"
    src = str(p.get("src") or "").upper()
    risk = p.get("risk") or {}
    level = risk.get("level") or p.get("risk_level") or "—"
    lines = [
        f"⏳ <b>پیشنهاد {side}</b> {src}",
        f"مقدار: <code>{p.get('amount')}</code> @ {fmt_num(p.get('price'))}",
    ]
    if p.get("alloc_toman"):
        lines.append(f"بودجه: {fmt_num(p['alloc_toman'])} ت")
    if p.get("buy_votes") is not None:
        lines.append(f"رأی خرید: {p.get('buy_votes')}")
    lines.append(f"ریسک: {level}" + (f" ({risk.get('score')}/10)" if risk.get("score") is not None else ""))
    if p.get("reason"):
        lines.append(p["reason"])
    if p.get("warning") or p.get("balance_ok") is False:
        lines.append(f"⚠ {p.get('warning') or 'موجودی فعلی کمتر از بودجه پیشنهادی است'}")
    if p.get("time"):
        lines.append(f"زمان: {p['time']}")
    lines.append(f"id: <code>{p.get('id')}</code>")
    return "\n".join(lines)


def msg_pending():
    b = np.load_bot()
    pending = [p for p in (b.get("rt_pending") or []) if p.get("status") == "pending"]
    if not pending:
        return "پیشنهادی در انتظار نیست.", None
    # فقط اولی را با دکمه می‌فرستیم؛ بقیه متنی
    first = pending[0]
    text = format_pending_item(first)
    if len(pending) > 1:
        text += f"\n\n(+{len(pending) - 1} پیشنهاد دیگر — دوباره /pending بزن)"
    return text, pending_keyboard(first["id"])


def msg_status():
    b = np.load_bot()
    creds = np.load_creds()
    mode = creds.get("mode") or "—"
    has_key = bool(creds.get("api_key") or creds.get("token"))
    lines = [
        "📋 <b>وضعیت</b>",
        f"کلید API: {'✅ ثبت شده' if has_key else '❌ ثبت نشده'} ({mode})",
        f"ربات اسپات (Realtime): {'🟢 روشن' if b.get('rt_enabled') else '🔴 خاموش'}",
        f"شبیه‌سازی: {'🟢' if b.get('enabled') else '🔴'}",
        f"نیاز به تأیید: {'بله' if b.get('rt_require_confirm', True) else 'خیر'}",
        f"حداکثر ٪ هر پوزیشن: {b.get('rt_max_position_pct', '—')}",
        f"حد ضرر / حد سود: {b.get('rt_stop_loss_pct', '—')}٪ / {b.get('rt_take_profit_pct', '—')}٪",
        f"حداقل رأی خرید: {b.get('rt_min_votes', '—')}",
        f"پیشنهادهای باز: {len([p for p in (b.get('rt_pending') or []) if p.get('status') == 'pending'])}",
        f"موقعیت‌های باز: {len(b.get('rt_positions') or [])}",
        f"موجودی کش‌شده: {fmt_num(b.get('rt_toman_balance') or 0)} ت",
    ]
    logs = (b.get("rt_logs") or [])[-5:]
    if logs:
        lines.append("\n<b>آخرین لاگ:</b>")
        for L in logs:
            lines.append(f"• {L}")
    return "\n".join(lines)


# ───────────────────────── state برای مکالمه setkey ─────────────────────────
_user_state = {}  # chat_id -> {"step": "...", ...}


def handle_setkey_flow(chat_id: int, text: str, cfg: dict) -> str | None:
    st = _user_state.get(chat_id) or {}
    step = st.get("step")
    if step == "wait_api_key":
        st["api_key"] = text.strip()
        st["step"] = "wait_private_key"
        _user_state[chat_id] = st
        return "حالا <b>Private Key</b> را بفرست (همان یک‌بار نمایش‌داده‌شده در نوبیتکس):"
    if step == "wait_private_key":
        api_key = st.get("api_key") or ""
        private_key = text.strip()
        try:
            np.parse_private_key(private_key)  # اعتبارسنجی اولیه
        except Exception as e:
            _user_state.pop(chat_id, None)
            return f"❌ Private Key نامعتبر: {e}\nدوباره /setkey را بزن."
        np.save_creds({
            "mode": "apikey",
            "api_key": api_key,
            "private_key": private_key,
        })
        _user_state.pop(chat_id, None)
        return "✅ کلید API ذخیره شد. با /balance موجودی را چک کن."
    if step == "wait_token":
        token = text.strip()
        if not token:
            return "توکن خالی است. دوباره بفرست یا /clearcred"
        np.save_creds({"mode": "token", "token": token})
        _user_state.pop(chat_id, None)
        return "✅ توکن ذخیره شد. با /balance تست کن."
    return None


# ───────────────────────── اجرای تأیید / رد ─────────────────────────
def do_confirm(pid: str) -> str:
    """همان منطق rt_confirm در main.py — با چک موجودی."""
    b = np.load_bot()
    pending = b.get("rt_pending") or []
    found = None
    for p in pending:
        if p.get("id") == pid and p.get("status") == "pending":
            found = p
            break
    if not found:
        return "پیشنهاد یافت نشد یا منقضی شده."

    side = found["side"]
    src = found["src"]
    amount = found["amount"]
    price = found["price"]

    if side == "buy":
        wallets_now, werr = np.fetch_user_wallets_server()
        if werr:
            return f"خطا در دریافت موجودی: {werr}"
        toman_now = np.parse_toman_balance(wallets_now)
        needed = float(found.get("alloc_toman") or 0)
        if needed <= 0 and price and amount:
            needed = (float(price) / 10.0) * float(amount)
        if toman_now < needed * 0.98:
            return (
                f"❌ موجودی کافی نیست.\n"
                f"موجودی فعلی: {fmt_num(toman_now)} ت\n"
                f"مورد نیاز حدود: {fmt_num(needed)} ت\n"
                f"سفارش اجرا نشد."
            )

    st, data = np.bot_place_order(side, src, "rls", amount, price)
    if st == 200 and data.get("status") == "ok":
        oid = (data.get("order") or {}).get("id")
        found["status"] = "executed"
        found["order_id"] = oid
        found["executed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        b["rt_pending"] = [p for p in pending if p.get("id") != pid]
        trades = b.get("rt_trades") or []
        trades.append(found)
        b["rt_trades"] = trades[-80:]
        if side == "buy":
            pos = {
                "src": src, "amount": amount, "entry": price,
                "time": found["executed_at"], "cost_toman": found.get("alloc_toman"),
                "order_id": oid,
            }
            b.setdefault("rt_positions", []).append(pos)
        elif side == "sell":
            b["rt_positions"] = [p for p in (b.get("rt_positions") or []) if p.get("src") != src]
            np.save_bot(b)
            np.set_rt_cooldown(src, b.get("rt_cooldown_hours") or 8)
        np.save_bot(b)
        np.rt_log(f"✅ [TG] تأیید شد و سفارش {side} {src.upper()} ثبت شد #{oid}")
        return f"✅ سفارش {side} {src.upper()} ثبت شد\norder_id: {oid}"
    return f"❌ خطا: {data.get('message') or data}"


def do_reject(pid: str) -> str:
    b = np.load_bot()
    pending = b.get("rt_pending") or []
    rejected = next((p for p in pending if p.get("id") == pid), None)
    b["rt_pending"] = [p for p in pending if p.get("id") != pid]
    np.save_bot(b)
    if rejected and rejected.get("src"):
        np.set_rt_cooldown(rejected["src"], b.get("rt_cooldown_hours") or 8)
        np.rt_log(f"❌ [TG] پیشنهاد {rejected.get('src', '').upper()} رد شد")
        return f"رد شد. cooldown برای {rejected['src'].upper()} فعال شد."
    return "پیشنهاد رد شد."


# ───────────────────────── هندلر پیام / کال‌بک ─────────────────────────
def handle_message(cfg: dict, chat_id: int, text: str):
    text = (text or "").strip()
    if not text:
        return

    if not is_admin(chat_id, cfg):
        # عمداً جزئیات نمی‌دهیم؛ فقط رد دسترسی
        send_message(
            cfg["token"], chat_id,
            "⛔ دسترسی مجاز نیست.\nفقط ادمین تعریف‌شده در تنظیمات ربات می‌تواند استفاده کند."
        )
        return

    # جریان چندمرحله‌ای setkey
    if chat_id in _user_state:
        reply = handle_setkey_flow(chat_id, text, cfg)
        if reply:
            send_message(cfg["token"], chat_id, reply)
            return

    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""

    if cmd in ("/start", "/help"):
        send_message(cfg["token"], chat_id, msg_help())
    elif cmd == "/balance":
        send_message(cfg["token"], chat_id, msg_balance())
    elif cmd == "/portfolio":
        send_message(cfg["token"], chat_id, msg_portfolio())
    elif cmd == "/signals":
        send_message(cfg["token"], chat_id, msg_signals())
    elif cmd == "/pending":
        body, kb = msg_pending()
        send_message(cfg["token"], chat_id, body, reply_markup=kb)
    elif cmd == "/status":
        send_message(cfg["token"], chat_id, msg_status())
    elif cmd == "/bot_on":
        np.bot_start(which="rt")
        b = np.load_bot()
        b["rt_require_confirm"] = True
        np.save_bot(b)
        send_message(
            cfg["token"], chat_id,
            "🟢 ربات اسپات روشن شد.\nسیگنال‌ها اسکن می‌شوند و پیشنهادها برای تأیید اینجا می‌آیند."
        )
    elif cmd == "/bot_off":
        np.bot_stop(cancel_orders=False, which="rt")
        send_message(cfg["token"], chat_id, "🔴 ربات اسپات خاموش شد.")
    elif cmd == "/setkey":
        _user_state[chat_id] = {"step": "wait_api_key"}
        send_message(
            cfg["token"], chat_id,
            "API Key عمومی (Public Key) را بفرست:\n"
            "<i>نوبیتکس → تنظیمات → API Key — فقط مجوز READ کافی است؛ برای معامله TRADE هم لازم است. WITHDRAW نده.</i>"
        )
    elif cmd == "/settoken":
        _user_state[chat_id] = {"step": "wait_token"}
        send_message(cfg["token"], chat_id, "Token کلاسیک نوبیتکس را بفرست:")
    elif cmd == "/clearcred":
        np.clear_creds()
        send_message(cfg["token"], chat_id, "کلیدها پاک شدند.")
    else:
        send_message(cfg["token"], chat_id, "دستور ناشناخته. /help را ببین.")


def handle_callback(cfg: dict, cq: dict):
    data = cq.get("data") or ""
    chat_id = cq["message"]["chat"]["id"]
    msg_id = cq["message"]["message_id"]
    cb_id = cq["id"]

    if not is_admin(chat_id, cfg):
        answer_callback(cfg["token"], cb_id, "⛔ دسترسی مجاز نیست", show_alert=True)
        return

    if data.startswith("confirm:"):
        pid = data.split(":", 1)[1]
        result = do_confirm(pid)
        answer_callback(cfg["token"], cb_id, result[:180], show_alert=True)
        edit_message(cfg["token"], chat_id, msg_id, result)
    elif data.startswith("reject:"):
        pid = data.split(":", 1)[1]
        result = do_reject(pid)
        answer_callback(cfg["token"], cb_id, result[:180])
        edit_message(cfg["token"], chat_id, msg_id, result)
    else:
        answer_callback(cfg["token"], cb_id, "ok")


# ───────────────────────── پایش پس‌زمینه (سیگنال جدید → تلگرام) ─────────────────────────
def watcher_loop(cfg_holder: dict):
    """هر ۳۰ ثانیه pending جدید را به تلگرام می‌فرستد."""
    global _last_pending_ids
    while not _stop.is_set():
        try:
            cfg = cfg_holder["cfg"]
            if not cfg.get("token") or not allowed_ids(cfg):
                _stop.wait(15)
                continue
            if not cfg.get("notify_pending", True):
                _stop.wait(30)
                continue
            b = np.load_bot()
            pending = [p for p in (b.get("rt_pending") or []) if p.get("status") == "pending"]
            current_ids = {p.get("id") for p in pending if p.get("id")}
            new_ids = current_ids - _last_pending_ids
            for p in pending:
                if p.get("id") in new_ids:
                    text = "🔔 <b>پیشنهاد جدید</b>\n\n" + format_pending_item(p)
                    broadcast(cfg, text, reply_markup=pending_keyboard(p["id"]))
            _last_pending_ids = current_ids
        except Exception as e:
            print(f"[watcher] {e}")
        _stop.wait(30)


# ───────────────────────── long polling ─────────────────────────
def poll_loop(cfg_holder: dict):
    global _offset
    token = cfg_holder["cfg"]["token"]
    print("Long-polling تلگرام شروع شد…")
    while not _stop.is_set():
        cfg = cfg_holder["cfg"]
        token = cfg.get("token") or token
        res = tg_request(token, "getUpdates", {
            "offset": _offset,
            "timeout": 25,
            "allowed_updates": ["message", "callback_query"],
        }, timeout=35)
        if not res.get("ok"):
            err = res.get("description") or res
            print(f"[tg] getUpdates error: {err}")
            _stop.wait(5)
            continue
        for upd in res.get("result") or []:
            _offset = max(_offset, int(upd["update_id"]) + 1)
            try:
                if "callback_query" in upd:
                    handle_callback(cfg, upd["callback_query"])
                elif "message" in upd:
                    msg = upd["message"]
                    chat_id = msg["chat"]["id"]
                    text = msg.get("text") or ""
                    handle_message(cfg, chat_id, text)
            except Exception as e:
                print(f"[tg] handle error: {e}")


def main():
    cfg = load_tg_config()
    if not cfg.get("token"):
        print(
            "توکن ربات تنظیم نشده.\n"
            "۱) از @BotFather توکن بگیر\n"
            "۲) آیدی عددی خودت را از @userinfobot بگیر\n"
            "۳) فایل telegram_config.json بساز:\n"
            '   {"token": "123456:ABC...", "admin_id": 123456789}\n'
            "   یا TELEGRAM_BOT_TOKEN و TELEGRAM_ADMIN_ID را ست کن.\n"
        )
        if not CONFIG_FILE.exists():
            save_tg_config({
                "token": "",
                "admin_id": None,
                "chat_ids": [],
                "notify_signals": True,
                "notify_pending": True,
            })
            print(f"قالب خالی ساخته شد: {CONFIG_FILE}")
        sys.exit(1)

    if not allowed_ids(cfg):
        print(
            "admin_id تنظیم نشده — ربات بدون ادمین اجرا نمی‌شود.\n"
            "در telegram_config.json بگذار:  \"admin_id\": 123456789\n"
            "آیدی عددی را از @userinfobot در تلگرام بگیر.\n"
        )
        if not CONFIG_FILE.exists():
            save_tg_config({
                "token": cfg.get("token") or "",
                "admin_id": None,
                "chat_ids": [],
                "notify_signals": True,
                "notify_pending": True,
            })
        sys.exit(1)

    me = tg_request(cfg["token"], "getMe")
    if not me.get("ok"):
        print("توکن نامعتبر:", me.get("description") or me)
        sys.exit(1)
    username = (me.get("result") or {}).get("username")
    print(f"ربات آماده است: @{username}")
    print(f"ادمین‌های مجاز (فقط این آیدی‌ها): {sorted(allowed_ids(cfg))}")

    cfg_holder = {"cfg": cfg}

    # رفرش دوره‌ای config از فایل (اگر کاربر توکن/chat عوض کرد)
    def reload_cfg():
        while not _stop.is_set():
            cfg_holder["cfg"] = load_tg_config()
            _stop.wait(20)

    threading.Thread(target=reload_cfg, daemon=True).start()
    threading.Thread(target=watcher_loop, args=(cfg_holder,), daemon=True).start()

    try:
        poll_loop(cfg_holder)
    except KeyboardInterrupt:
        print("\nتوقف…")
    finally:
        _stop.set()
        np.bot_stop(cancel_orders=False, which="all")


if __name__ == "__main__":
    main()
