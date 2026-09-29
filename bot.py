import asyncio
import os
import re
import sqlite3
from datetime import date, datetime, time as dt_time, timedelta, timezone

import discord
from discord.ext import commands, tasks
from discord.ui import Button, Modal, Select, TextInput, View
from dotenv import load_dotenv

# =====================
# 設定
# =====================
load_dotenv()  # 一定要在 os.getenv 之前
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("找不到 DISCORD_TOKEN，請檢查 .env")

MAX_DAYS_AHEAD = 180          # 練團預約(不輸入年份)最多可預約幾天後
CHECK_INTERVAL_SEC = 30       # 每幾秒檢查一次「活動開始 / 結束」
MAX_FUTURE_SHOWN = 25         # 「未來」欄位最多顯示幾筆，避免超過 Embed 上限
TZ = timezone(timedelta(hours=8))  # 台灣時間 (UTC+8)，不受主機時區影響
WEEK = "一二三四五六日"

# =====================
# 時間工具
# =====================
def now() -> datetime:
    """台灣現在時間 (不帶時區資訊，方便和資料庫時間比較)"""
    return datetime.now(TZ).replace(tzinfo=None)


def parse_time(text: str):
    """'9:00' / '09:00' / '９：００' -> (分鐘數, 'HH:MM')；格式錯誤回傳 None"""
    m = re.fullmatch(r"(\d{1,2})[:：](\d{2})", text.strip())
    if not m:
        return None
    h, mi = int(m[1]), int(m[2])
    if h > 23 or mi > 59:
        return None
    return h * 60 + mi, f"{h:02d}:{mi:02d}"


def parse_md(text: str):
    """'9/15' / '9-15' / '9.15' -> (9, 15)；不存在的日期回傳 None"""
    m = re.fullmatch(r"(\d{1,2})\s*[/\-.]\s*(\d{1,2})", text.strip())
    if not m:
        return None
    mo, d = int(m[1]), int(m[2])
    try:
        date(2024, mo, d)  # 用閏年檢查，2/29 才不會被誤判
    except ValueError:
        return None
    return mo, d


def infer_date(mo: int, d: int, today: date):
    """
    沒有年份時推算年份：
    今年這天還沒過 -> 今年；已經過了 -> 明年。
    但如果推算結果離今天超過 MAX_DAYS_AHEAD 天，視為「輸入了過去的日期」回傳 None。
    (例: 今天 12/20 輸入 1/5 -> 明年 1/5 OK；今天 9/29 輸入 9/15 -> 明年 9/15 太遠，拒絕)
    """
    for y in (today.year, today.year + 1):
        try:
            cand = date(y, mo, d)
        except ValueError:
            continue
        if cand >= today:
            return cand if (cand - today).days <= MAX_DAYS_AHEAD else None
    return None


def fmt_date(d: date, with_year: bool = False) -> str:
    base = f"{d.month}/{d.day} ({WEEK[d.weekday()]})"
    return f"{d.year}/{base}" if with_year else base


# =====================
# DB
# =====================
conn = sqlite3.connect("booking.db")
conn.row_factory = sqlite3.Row

# 新版資料表：日期存完整 YYYY-MM-DD，並區分 practice(練團) / event(重要活動)
conn.execute("""
CREATE TABLE IF NOT EXISTS bookings_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'practice',
    date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    event TEXT NOT NULL,
    user TEXT NOT NULL,
    user_name TEXT
)
""")
# 記住每個伺服器的行事曆訊息在哪裡 (機器人重啟後也找得回來)
conn.execute("""
CREATE TABLE IF NOT EXISTS calendar_refs (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL
)
""")
conn.commit()


def migrate_old_table():
    """把舊的 bookings (日期是 '9/15' 沒年份) 轉成新版；已經過期的舊資料會被丟掉"""
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='bookings'"
    ).fetchone()
    if not exists:
        return
    today_d = now().date()
    for r in conn.execute("SELECT * FROM bookings").fetchall():
        try:
            mo, d = map(int, r["date"].split("/"))
            full = infer_date(mo, d, today_d)
            s, e = parse_time(r["start_time"]), parse_time(r["end_time"])
            if not (full and s and e):
                continue
            conn.execute(
                "INSERT INTO bookings_v2 (kind,date,start_time,end_time,event,user,user_name) "
                "VALUES ('practice',?,?,?,?,?,NULL)",
                (full.isoformat(), s[1], e[1], r["event"], r["user"]),
            )
        except Exception:
            continue
    conn.execute("ALTER TABLE bookings RENAME TO bookings_old_backup")
    conn.commit()
    print("已將舊資料表轉換為新格式 (舊表備份為 bookings_old_backup)")


migrate_old_table()


def load_bookings():
    """讀出所有預約，並附上完整的開始 / 結束 datetime"""
    out = []
    for r in conn.execute("SELECT * FROM bookings_v2 ORDER BY date, start_time, id"):
        b = dict(r)
        d = date.fromisoformat(b["date"])
        b["d"] = d
        b["start_dt"] = datetime.combine(d, dt_time.fromisoformat(b["start_time"]))
        b["end_dt"] = datetime.combine(d, dt_time.fromisoformat(b["end_time"]))
        out.append(b)
    return out


def cleanup_expired() -> int:
    """刪除所有「結束時間已過」的預約 (包含過去日期)，回傳刪除筆數"""
    cutoff = now().strftime("%Y-%m-%d %H:%M")
    cur = conn.execute(
        "DELETE FROM bookings_v2 WHERE (date || ' ' || end_time) <= ?", (cutoff,)
    )
    conn.commit()
    return cur.rowcount


def find_conflict(d: date, s_min: int, e_min: int):
    for r in conn.execute(
        "SELECT event, start_time, end_time FROM bookings_v2 WHERE date = ?",
        (d.isoformat(),),
    ):
        if s_min < parse_time(r["end_time"])[0] and e_min > parse_time(r["start_time"])[0]:
            return r
    return None


def insert_booking(kind, d: date, s: str, e: str, event: str, user):
    conn.execute(
        "INSERT INTO bookings_v2 (kind,date,start_time,end_time,event,user,user_name) "
        "VALUES (?,?,?,?,?,?,?)",
        (kind, d.isoformat(), s, e, event, str(user.id), user.display_name),
    )
    conn.commit()


# =====================
# BOT
# =====================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


def is_admin(member) -> bool:
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and perms.administrator)


async def safe_delete(msg):
    try:
        await msg.delete()
    except (discord.HTTPException, AttributeError):
        pass


async def reject(interaction: discord.Interaction, text: str):
    await interaction.response.send_message(f"❌ {text}", ephemeral=True, delete_after=5)


# =====================
# 行事曆 Embed
# =====================
def disp_name(guild, b) -> str:
    member = guild.get_member(int(b["user"])) if guild else None
    if member:
        return member.display_name
    return b.get("user_name") or "未知用戶"


def esc(text: str) -> str:
    return discord.utils.escape_markdown(text)


def add_field_chunked(embed: discord.Embed, name: str, items: list, sep: str = "\n"):
    """把一堆項目塞進欄位；超過 1024 字元就自動拆成多個欄位"""
    chunks, cur = [], ""
    for it in items:
        it = it[:1000]
        if cur and len(cur) + len(sep) + len(it) > 1000:
            chunks.append(cur)
            cur = it
        else:
            cur = cur + sep + it if cur else it
    if cur:
        chunks.append(cur)
    for i, ch in enumerate(chunks):
        embed.add_field(name=name if i == 0 else "\u200b", value=ch, inline=False)


def practice_line(b, guild, show_date: bool) -> str:
    when = f"📅 {fmt_date(b['d'])}　" if show_date else ""
    return (
        f"🎵 **{esc(b['event'])}**\n"
        f"　{when}⏰ `{b['start_time']}–{b['end_time']}`　👤 {esc(disp_name(guild, b))}"
    )


def ongoing_line(b, guild) -> str:
    name = esc(disp_name(guild, b))
    if b["kind"] == "event":
        return (
            f"⭐ **{esc(b['event'])}**　`重要活動`\n"
            f"　📅 {fmt_date(b['d'], True)}　⏰ `{b['start_time']}–{b['end_time']}`　👤 {name}"
        )
    return (
        f"🔴 **{esc(b['event'])}**\n"
        f"　⏰ `{b['start_time']}–{b['end_time']}`　👤 {name}"
    )


def event_block(b, guild) -> str:
    title = b["event"].replace("`", "'")
    name = disp_name(guild, b).replace("`", "'")
    return (
        "```fix\n"
        f"✨ {title} ✨\n"
        f"📅 {fmt_date(b['d'], True)}\n"
        f"⏰ {b['start_time']}–{b['end_time']}\n"
        f"👤 {name}\n"
        "```"
    )


def build_embed(guild) -> discord.Embed:
    n = now()
    today_d = n.date()
    rows = [b for b in load_bookings() if b["end_dt"] > n]  # 已結束的不顯示

    ongoing = [b for b in rows if b["start_dt"] <= n]
    upcoming = [b for b in rows if b["start_dt"] > n]
    events = [b for b in upcoming if b["kind"] == "event"]
    today_p = [b for b in upcoming if b["kind"] == "practice" and b["d"] == today_d]
    future_p = [b for b in upcoming if b["kind"] == "practice" and b["d"] > today_d]

    embed = discord.Embed(
        title="🎸 熱音社行事曆",
        color=0xE74C3C if ongoing else 0x2ECC71,
    )
    header = f"📍 今天是 **{fmt_date(today_d)}**"

    if not rows:
        embed.description = f"{header}\n\n📭 目前沒有任何活動"
    else:
        embed.description = (
            f"{header}　·　共 **{len(rows)}** 筆預約\n"
            "🔴 進行中　🎵 練團　⭐ 重要活動"
        )

        if ongoing:
            add_field_chunked(embed, "🔴 進行中", [ongoing_line(b, guild) for b in ongoing])

        if events:
            add_field_chunked(
                embed, "🌟 重要活動", [event_block(b, guild) for b in events]
            )

        add_field_chunked(
            embed,
            "📌 今天",
            [practice_line(b, guild, False) for b in today_p] or ["└ 今天沒有其他預約"],
        )

        shown = future_p[:MAX_FUTURE_SHOWN]
        lines = [practice_line(b, guild, True) for b in shown] or ["└ 沒有未來預約"]
        if len(future_p) > len(shown):
            lines.append(f"…還有 {len(future_p) - len(shown)} 筆")
        add_field_chunked(embed, "📅 未來", lines)

    embed.set_footer(text="!book 預約　!cancel 取消　!schedule 更新行事曆")
    embed.timestamp = discord.utils.utcnow()  # 顯示「最後更新」時間 (自動轉成各人本地時間)
    return embed


# =====================
# 行事曆訊息：更新 / 建立
# =====================
calendar_lock = asyncio.Lock()


def get_ref(guild_id: int):
    return conn.execute(
        "SELECT * FROM calendar_refs WHERE guild_id = ?", (guild_id,)
    ).fetchone()


def save_ref(guild_id: int, channel_id: int, message_id: int):
    conn.execute(
        "INSERT INTO calendar_refs (guild_id, channel_id, message_id) VALUES (?,?,?) "
        "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id, "
        "message_id=excluded.message_id",
        (guild_id, channel_id, message_id),
    )
    conn.commit()


async def _upsert_in_channel(guild, channel):
    """在「指令所在頻道」更新行事曆；有舊的就更新，沒有就新建；舊的在別的頻道就移過來"""
    embed = build_embed(guild)
    ref = get_ref(guild.id)

    if ref and ref["channel_id"] == channel.id:
        try:
            msg = await channel.fetch_message(ref["message_id"])
            await msg.edit(embed=embed)
            return
        except discord.NotFound:
            pass  # 原訊息被刪了 -> 往下重新建立
    elif ref:
        # 舊行事曆在別的頻道 -> 刪掉舊的，搬到這個頻道
        try:
            old_ch = bot.get_channel(ref["channel_id"]) or await bot.fetch_channel(
                ref["channel_id"]
            )
            old_msg = await old_ch.fetch_message(ref["message_id"])
            await old_msg.delete()
        except discord.HTTPException:
            pass

    msg = await channel.send(embed=embed)
    save_ref(guild.id, channel.id, msg.id)


async def _refresh_saved(ref):
    """自動更新用：只更新已存在的行事曆，不在別處亂發"""
    guild = bot.get_guild(ref["guild_id"])
    if guild is None:
        return
    try:
        channel = guild.get_channel(ref["channel_id"]) or await bot.fetch_channel(
            ref["channel_id"]
        )
    except discord.NotFound:
        conn.execute("DELETE FROM calendar_refs WHERE guild_id = ?", (guild.id,))
        conn.commit()
        return
    except discord.HTTPException:
        return

    embed = build_embed(guild)
    try:
        msg = await channel.fetch_message(ref["message_id"])
        await msg.edit(embed=embed)
    except discord.NotFound:
        msg = await channel.send(embed=embed)  # 被刪掉了 -> 補一個新的
        save_ref(guild.id, channel.id, msg.id)
    except discord.HTTPException as e:
        print(f"更新行事曆失敗: {e}")


async def refresh_calendar(guild=None, channel=None):
    """
    有給 guild + channel：在該頻道更新 / 建立行事曆 (指令用)
    都不給：更新所有已存在的行事曆 (自動排程用)
    """
    async with calendar_lock:
        if guild is not None and channel is not None:
            await _upsert_in_channel(guild, channel)
        else:
            for ref in conn.execute("SELECT * FROM calendar_refs").fetchall():
                await _refresh_saved(ref)


# =====================
# 預約表單
# =====================
async def finish_booking(interaction, kind, d, start_s, end_s, event, is_event=False):
    """共用流程：檢查時間 -> 合理性 -> 衝突 -> 存入 -> 提示 3 秒 -> 更新行事曆"""
    # 1. 時間格式
    s = parse_time(start_s)
    e = parse_time(end_s)
    if not s or not e:
        return await reject(interaction, "時間格式錯誤，請用 18:00 這種格式")

    # 2. 合理性
    if e[0] <= s[0]:
        return await reject(interaction, "結束時間必須晚於開始時間")
    end_dt = datetime.combine(d, dt_time.fromisoformat(e[1]))
    if end_dt <= now():
        return await reject(interaction, "這個時段已經過了")

    # 3. 衝突
    clash = find_conflict(d, s[0], e[0])
    if clash:
        return await reject(
            interaction,
            f"時間衝突：與「{clash['event']}」({clash['start_time']}-{clash['end_time']}) 重疊",
        )

    # 4. 存入 (以 Discord 使用者 ID 儲存)
    insert_booking(kind, d, s[1], e[1], event, interaction.user)

    # 5. 成功提示 3 秒後自動刪除
    label = "重要活動已新增" if is_event else "預約成功"
    await interaction.response.send_message(
        f"✅ {label}", ephemeral=True, delete_after=3
    )

    # 6. 更新行事曆 (沒有就建立)
    await refresh_calendar(interaction.guild, interaction.channel)


class PracticeModal(Modal, title="預約練團"):
    event = TextInput(label="活動名稱", placeholder="例: 團練 / 個人練習", max_length=50)
    date_in = TextInput(label="日期", placeholder="例: 6/20", max_length=10)
    start = TextInput(label="開始時間", placeholder="例: 18:00", max_length=5)
    end = TextInput(label="結束時間", placeholder="例: 21:00", max_length=5)

    async def on_submit(self, interaction: discord.Interaction):
        md = parse_md(self.date_in.value)
        if not md:
            return await reject(interaction, "日期格式錯誤，請用 6/20 這種格式")

        d = infer_date(md[0], md[1], now().date())
        if d is None:
            return await reject(
                interaction, f"日期已經過了，或超過 {MAX_DAYS_AHEAD} 天後，無法預約"
            )

        await finish_booking(
            interaction, "practice", d,
            self.start.value, self.end.value, self.event.value.strip(),
        )


class EventModal(Modal, title="新增重要活動"):
    event = TextInput(label="活動名稱", placeholder="例: 期末成發", max_length=50)
    year = TextInput(label="年份", placeholder="例: 2026", max_length=4)
    date_in = TextInput(label="日期", placeholder="例: 12/25", max_length=10)
    start = TextInput(label="開始時間", placeholder="例: 18:00", max_length=5)
    end = TextInput(label="結束時間", placeholder="例: 21:00", max_length=5)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            return await reject(interaction, "只有管理員可以新增重要活動")

        try:
            y = int(self.year.value.strip())
            if not 2000 <= y <= 2100:
                raise ValueError
        except ValueError:
            return await reject(interaction, "年份格式錯誤，請輸入像 2026 的四位數字")

        md = parse_md(self.date_in.value)
        if not md:
            return await reject(interaction, "日期格式錯誤，請用 12/25 這種格式")
        try:
            d = date(y, md[0], md[1])
        except ValueError:
            return await reject(interaction, f"{y} 年沒有 {md[0]}/{md[1]} 這一天")

        await finish_booking(
            interaction, "event", d,
            self.start.value, self.end.value, self.event.value.strip(),
            is_event=True,
        )


# =====================
# !book 面板
# =====================
class BookView(View):
    def __init__(self, owner_id: int, admin: bool):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.message = None

        practice_btn = Button(label="🎵 預約練團", style=discord.ButtonStyle.green)
        practice_btn.callback = self.open_practice
        self.add_item(practice_btn)

        if admin:  # 只有管理員看得到「重要活動」按鈕
            event_btn = Button(label="🌟 新增重要活動", style=discord.ButtonStyle.blurple)
            event_btn.callback = self.open_event
            self.add_item(event_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ 這不是你的預約面板，請自己輸入 !book", ephemeral=True
            )
            return False
        return True

    async def open_practice(self, interaction: discord.Interaction):
        await interaction.response.send_modal(PracticeModal())
        self.stop()
        await safe_delete(interaction.message)

    async def open_event(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            return await reject(interaction, "只有管理員可以新增重要活動")
        await interaction.response.send_modal(EventModal())
        self.stop()
        await safe_delete(interaction.message)

    async def on_timeout(self):
        await safe_delete(self.message)


# =====================
# !cancel 選單
# =====================
class CancelView(View):
    def __init__(self, owner, rows):
        super().__init__(timeout=60)
        self.owner_id = owner.id
        self.selected_id = None
        self.message = None

        options = []
        for b in rows:
            is_event = b["kind"] == "event"
            label = f"{'⭐ ' if is_event else ''}{b['event']} | {fmt_date(b['d'])} {b['start_time']}-{b['end_time']}"
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(b["id"]),
                    description=f"重要活動 · {fmt_date(b['d'], True)}" if is_event else None,
                )
            )

        self.select = Select(placeholder="選擇要取消的項目", options=options, row=0)
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ 這不是你的選單", ephemeral=True
            )
            return False
        return True

    async def on_select(self, interaction: discord.Interaction):
        self.selected_id = int(self.select.values[0])
        for o in self.select.options:
            o.default = o.value == self.select.values[0]
        self.confirm.disabled = False  # 選了項目才能按確認
        await interaction.response.edit_message(view=self)

    @discord.ui.button(
        label="確認刪除", emoji="🗑️", style=discord.ButtonStyle.danger,
        disabled=True, row=1,
    )
    async def confirm(self, interaction: discord.Interaction, button: Button):
        row = conn.execute(
            "SELECT * FROM bookings_v2 WHERE id = ?", (self.selected_id,)
        ).fetchone()

        if row is None:
            text = "❌ 這個項目已經不存在了"
        else:
            uid = str(interaction.user.id)
            allowed = (row["kind"] == "practice" and row["user"] == uid) or (
                row["kind"] == "event" and is_admin(interaction.user)
            )
            if not allowed:
                text = "❌ 你沒有權限取消這個項目"
            else:
                conn.execute("DELETE FROM bookings_v2 WHERE id = ?", (row["id"],))
                conn.commit()
                text = f"🗑️ 已取消：{row['event']}"

        self.stop()
        await interaction.response.edit_message(content=text, view=None)
        await refresh_calendar(interaction.guild, interaction.channel)

        await asyncio.sleep(3)
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            await safe_delete(interaction.message)

    async def on_timeout(self):
        await safe_delete(self.message)


# =====================
# 指令
# =====================
@bot.command()
@commands.guild_only()
async def schedule(ctx):
    await safe_delete(ctx.message)
    cleanup_expired()
    await refresh_calendar(ctx.guild, ctx.channel)


@bot.command()
@commands.guild_only()
async def book(ctx):
    await safe_delete(ctx.message)
    admin = is_admin(ctx.author)
    view = BookView(ctx.author.id, admin)
    text = "📅 **預約系統**\n請選擇要新增的類型" if admin else "📅 **預約系統**"
    view.message = await ctx.send(text, view=view)


@bot.command()
@commands.guild_only()
async def cancel(ctx):
    await safe_delete(ctx.message)

    admin = is_admin(ctx.author)
    uid = str(ctx.author.id)
    n = now()
    rows = [
        b for b in load_bookings()
        if b["end_dt"] > n
        and ((b["kind"] == "practice" and b["user"] == uid)
             or (b["kind"] == "event" and admin))
    ][:25]  # Discord 下拉選單最多 25 項

    if not rows:
        await ctx.send("❌ 你目前沒有可以取消的項目", delete_after=3)
        return

    view = CancelView(ctx.author, rows)
    view.message = await ctx.send("🗑️ 請選擇要取消的項目，再按「確認刪除」", view=view)


@bot.command()
@commands.guild_only()
async def clearall(ctx, confirm=None):
    await safe_delete(ctx.message)

    if not is_admin(ctx.author):
        await ctx.send("❌ 你沒有權限使用此指令", delete_after=3)
        return

    if confirm != "YES":
        await ctx.send(
            "⚠️ 此操作會刪除所有預約\n請輸入：`!clearall YES` 確認", delete_after=8
        )
        return

    conn.execute("DELETE FROM bookings_v2")
    conn.commit()
    await ctx.send("🗑️ 已清空所有預約", delete_after=3)
    await refresh_calendar(ctx.guild, ctx.channel)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, (commands.CommandNotFound, commands.NoPrivateMessage)):
        return
    print(f"指令錯誤: {error}")


# =====================
# 自動排程
# =====================
last_ongoing_ids = None


@tasks.loop(seconds=CHECK_INTERVAL_SEC)
async def watcher():
    """活動開始 -> 出現在「進行中」；活動結束 -> 自動刪除。有變化就更新行事曆"""
    global last_ongoing_ids
    try:
        removed = cleanup_expired()
        n = now()
        ids = frozenset(
            b["id"] for b in load_bookings() if b["start_dt"] <= n < b["end_dt"]
        )
        if removed or ids != last_ongoing_ids:
            last_ongoing_ids = ids
            await refresh_calendar()
    except Exception as e:
        print(f"watcher 錯誤: {e}")


@watcher.before_loop
async def before_watcher():
    await bot.wait_until_ready()


@tasks.loop(time=dt_time(hour=0, minute=0, tzinfo=TZ))
async def midnight_update():
    """每天 00:00 (台灣時間) 更新當天行事曆"""
    try:
        cleanup_expired()
        await refresh_calendar()
    except Exception as e:
        print(f"midnight_update 錯誤: {e}")


@midnight_update.before_loop
async def before_midnight():
    await bot.wait_until_ready()


# =====================
# RUN
# =====================
@bot.event
async def on_ready():
    print(f"Bot online: {bot.user}")
    if not watcher.is_running():
        watcher.start()
    if not midnight_update.is_running():
        midnight_update.start()


bot.run(TOKEN)