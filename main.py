import asyncio
import html
import json
import logging
import os
from threading import Thread
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify
from telegram import InputFile, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ChatJoinRequestHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

# فایل .env باید کنار همین main.py باشد؛ مسیر صریح برای اجرای مطمئن روی Discloud.
load_dotenv(Path(__file__).with_name('.env'))
logging.basicConfig(format='%(asctime)s | %(levelname)s | %(message)s', level=logging.INFO)
log = logging.getLogger(__name__)

OWNER_ID = int(os.getenv('OWNER_ID', '1232259973'))
TOKEN = os.getenv('BOT_TOKEN')
DATA_DIR = Path(os.getenv('DATA_DIR', str(Path(__file__).parent)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE = DATA_DIR / 'config.json'
BACKUP_DIR = DATA_DIR / 'backups'
LOCK = asyncio.Lock()


def new_db():
    return {
        'version': 1, 'publicChatId': None, 'vipChatId': None,
        'vipInviteLink': '', 'requiredCount': 10,
        'users': {}, 'countedMembers': {}, 'notifiedQualified': {},
    }


db = new_db()


def load_db():
    global db
    try:
        db = {**new_db(), **json.loads(DATA_FILE.read_text(encoding='utf-8'))}
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.error('خواندن config.json ناموفق: %s', exc)
    BACKUP_DIR.mkdir(exist_ok=True)


async def save_db():
    async with LOCK:
        temp = DATA_FILE.with_suffix('.tmp')
        temp.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(DATA_FILE)


def owner(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == OWNER_ID)


async def owner_only(update: Update) -> bool:
    if owner(update):
        return True
    if update.effective_message:
        await update.effective_message.reply_text('⛔ این دستور فقط برای مالک ربات مجاز است.')
    return False


def record(user_id: int):
    return db['users'].get(str(user_id), {'count': 0, 'qualified': False})


def qualified(user_id: int) -> bool:
    return record(user_id).get('qualified') is True


def user_mention(user) -> str:
    name = ' '.join(x for x in [user.first_name, user.last_name] if x) or 'کاربر'
    return f'<a href="tg://user?id={user.id}">{html.escape(name)}</a>'


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text('✅ ربات فعال است. تنظیمات فقط توسط مالک انجام می‌شود.')


async def setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update) or update.effective_chat.type == 'private':
        return
    if not context.args:
        await update.effective_message.reply_text('نمونه:\n/setup public\n/setup vip https://t.me/+LINK')
        return
    kind = context.args[0].lower()
    if kind == 'public':
        db['publicChatId'] = update.effective_chat.id
        await save_db()
        await update.effective_message.reply_text('✅ این گروه به‌عنوان گروه عمومی ثبت شد.')
    elif kind == 'vip':
        if len(context.args) < 2 or not context.args[1].startswith('https://t.me/'):
            await update.effective_message.reply_text('❌ نمونه درست: /setup vip https://t.me/+LINK')
            return
        db['vipChatId'] = update.effective_chat.id
        db['vipInviteLink'] = context.args[1]
        await save_db()
        await update.effective_message.reply_text('✅ گروه VIP و لینک درخواست عضویت ثبت شد. مطمئن شوید لینک از نوع Request Admin Approval است.')
    else:
        await update.effective_message.reply_text('نمونه:\n/setup public\n/setup vip https://t.me/+LINK')


async def setcount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return
    try:
        amount = int(context.args[0])
        if not 1 <= amount <= 10000:
            raise ValueError
    except (IndexError, ValueError):
        await update.effective_message.reply_text('❌ نمونه درست: /setcount 10')
        return
    db['requiredCount'] = amount
    for item in db['users'].values():
        item['qualified'] = item.get('count', 0) >= amount
    await save_db()
    await update.effective_message.reply_text(f'✅ حد نصاب روی {amount} اد تنظیم شد.')


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return
    total = sum(1 for item in db['users'].values() if item.get('qualified'))
    await update.effective_message.reply_text(
        f'📊 وضعیت ربات\nحد نصاب: {db["requiredCount"]}\n'
        f'گروه عمومی: {db["publicChatId"] or "تنظیم نشده"}\n'
        f'VIP: {db["vipChatId"] or "تنظیم نشده"}\n'
        f'کاربران تأییدشده: {total}'
    )


async def make_backup(context: ContextTypes.DEFAULT_TYPE, reason='۳۰ دقیقه‌ای'):
    await save_db()
    BACKUP_DIR.mkdir(exist_ok=True)
    filename = f'vip-backup-{datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")}.json'
    filepath = BACKUP_DIR / filename
    filepath.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        with filepath.open('rb') as f:
            await context.bot.send_document(
                OWNER_ID, InputFile(f, filename=filename),
                caption=f'📦 بکاپ {reason}\nبرای بازگردانی، این فایل را با کپشن /restore به پی‌وی ربات بفرستید.'
            )
    except Exception as exc:
        log.warning('ارسال بکاپ ناموفق است؛ مالک باید /start را در پی‌وی بزند: %s', exc)
    files = sorted(BACKUP_DIR.glob('*.json'))
    for old in files[:-48]:
        old.unlink(missing_ok=True)


async def scheduled_backup(context: ContextTypes.DEFAULT_TYPE):
    await make_backup(context, '۳۰ دقیقه‌ای')


async def backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return
    await make_backup(context, 'دستی')
    await update.effective_message.reply_text('✅ بکاپ به پی‌وی مالک ارسال شد.')


async def restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return
    message = update.effective_message
    if update.effective_chat.type != 'private' or not message.document:
        await message.reply_text('❌ فایل JSON بکاپ را با کپشن /restore در پی‌وی ربات ارسال کنید.')
        return
    if not message.document.file_name.lower().endswith('.json'):
        await message.reply_text('❌ فقط فایل JSON معتبر است.')
        return
    try:
        tg_file = await context.bot.get_file(message.document.file_id)
        content = await tg_file.download_as_bytearray()
        restored = json.loads(content.decode('utf-8'))
        needed = {'users', 'countedMembers', 'requiredCount'}
        if not isinstance(restored, dict) or not needed.issubset(restored):
            raise ValueError('ساختار فایل درست نیست')
        global db
        db = {**new_db(), **restored}
        db['requiredCount'] = int(db['requiredCount'])
        await save_db()
        await message.reply_text('✅ بکاپ با موفقیت بازگردانی شد.')
    except Exception as exc:
        log.warning('restore ناموفق: %s', exc)
        await message.reply_text('❌ فایل بکاپ معتبر نیست یا قابل خواندن نیست.')


async def new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if update.effective_chat.id != db['publicChatId'] or not message.new_chat_members:
        return
    inviter = message.from_user
    if not inviter or inviter.is_bot:
        return
    changed = False
    for member in message.new_chat_members:
        if member.is_bot or member.id == inviter.id or str(member.id) in db['countedMembers']:
            continue
        db['countedMembers'][str(member.id)] = inviter.id
        item = record(inviter.id)
        item['count'] += 1
        item['qualified'] = item['count'] >= db['requiredCount']
        db['users'][str(inviter.id)] = item
        changed = True
    if not changed:
        return
    await save_db()
    if qualified(inviter.id) and not db['notifiedQualified'].get(str(inviter.id)) and db['vipInviteLink']:
        db['notifiedQualified'][str(inviter.id)] = True
        await save_db()
        await message.reply_html(
            f'✅ تبریک {user_mention(inviter)}!\n'
            f'شما تعداد لازم، یعنی <b>{db["requiredCount"]} عضو</b> را به گروه اضافه کردید.\n\n'
            f'برای تأیید عضویت در گروه فیلم VIP، از لینک زیر درخواست عضویت بدهید:\n{db["vipInviteLink"]}'
        )


async def delete_warning(context: ContextTypes.DEFAULT_TYPE):
    try:
        await context.bot.delete_message(context.job.chat_id, context.job.data)
    except Exception:
        pass


async def gate_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if update.effective_chat.id != db['publicChatId'] or not user or user.is_bot or owner(update):
        return
    if qualified(user.id):
        return
    item = record(user.id)
    remaining = max(0, db['requiredCount'] - item['count'])
    try:
        await message.delete()
    except Exception as exc:
        log.warning('حذف پیام کاربر ناموفق: %s', exc)
    try:
        notice = await context.bot.send_message(
            update.effective_chat.id,
            f'❌ {user_mention(user)}\n'
            f'برای تأیید عضویت در گروه فیلم VIP، باید ابتدا <b>{db["requiredCount"]} عضو</b> را به گروه اضافه کنید.\n\n'
            f'👥 تعداد اد شما: <b>{item["count"]} از {db["requiredCount"]}</b>\n'
            f'⏳ تعداد باقی‌مانده: <b>{remaining} نفر</b>',
            parse_mode=ParseMode.HTML,
        )
        context.job_queue.run_once(delete_warning, 60, chat_id=notice.chat_id, data=notice.message_id)
    except Exception as exc:
        log.warning('ارسال هشدار ناموفق: %s', exc)


async def join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req = update.chat_join_request
    if not req or req.chat.id != db['vipChatId']:
        return
    try:
        if qualified(req.from_user.id):
            await context.bot.approve_chat_join_request(req.chat.id, req.from_user.id)
            try:
                await context.bot.send_message(req.from_user.id, '✅ درخواست عضویت شما در گروه فیلم VIP تأیید شد. خوش آمدید 🌟')
            except Exception:
                pass
        else:
            await context.bot.decline_chat_join_request(req.chat.id, req.from_user.id)
            item = record(req.from_user.id)
            try:
                await context.bot.send_message(req.from_user.id, f'❌ درخواست شما تأیید نشد. برای تأیید عضویت در گروه فیلم VIP، باید ابتدا {db["requiredCount"]} عضو را به گروه عمومی اضافه کنید.\n\n👥 تعداد اد شما: {item["count"]} از {db["requiredCount"]}')
            except Exception:
                pass
    except Exception as exc:
        log.error('مدیریت درخواست عضویت ناموفق: %s', exc)


def run_health_server():
    web = Flask(__name__)

    @web.get('/')
    @web.get('/health')
    def health_check():
        return jsonify(status='ok', service='vip-film-gate-bot')

    port = int(os.getenv('PORT', '10000'))
    web.run(host='0.0.0.0', port=port, use_reloader=False)


def main():
    if not TOKEN:
        raise RuntimeError('BOT_TOKEN در فایل .env تنظیم نشده است.')
    load_db()
    # برای Render Web Service؛ مسیر /health قابل Ping است.
    Thread(target=run_health_server, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('setup', setup))
    app.add_handler(CommandHandler('setcount', setcount))
    app.add_handler(CommandHandler('status', status))
    app.add_handler(CommandHandler('backup', backup))
    # ریستور فقط با سند JSON که کپشن آن /restore است.
    app.add_handler(MessageHandler(filters.Document.ALL & filters.CAPTION & filters.Regex(r'^/restore(?:\s|$)'), restore))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_members))
    app.add_handler(ChatJoinRequestHandler(join_request))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND & ~filters.StatusUpdate.ALL, gate_messages))
    app.job_queue.run_repeating(scheduled_backup, interval=1800, first=1800)
    app.run_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
