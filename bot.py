import discord
from discord.ext import commands
import sqlite3
from datetime import datetime
from discord.ui import View, Button, Modal, TextInput
import asyncio
import os
from dotenv import load_dotenv

TOKEN = os.getenv("DISCORD_TOKEN")

load_dotenv()

# =====================
# DB
# =====================
conn = sqlite3.connect("booking.db")
c = conn.cursor()

c.execute("""
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT,
    start_time TEXT,
    end_time TEXT,
    event TEXT,
    user TEXT
)
""")
conn.commit()

# =====================
# BOT
# =====================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
calendar_message = None

# =====================
# TIME
# =====================

def now_min():
    t = datetime.now()
    return t.hour * 60 + t.minute


def to_min(t):
    h, m = map(int, t.split(":"))
    return h * 60 + m

def today():
    return f"{datetime.now().month}/{datetime.now().day}"


def cleanup_expired():
    now = now_min()
    today_str = today()

    c.execute("SELECT id, date, end_time FROM bookings")
    rows = c.fetchall()

    for booking_id, date, end_time in rows:

        try:
            # ❗只有今天才檢查時間
            if date == today_str:
                if to_min(end_time) < now:
                    c.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
        except:
            continue

    conn.commit()


async def update_calendar():

    global calendar_message

    c.execute("""
        SELECT date, start_time, end_time, event, user
        FROM bookings
        ORDER BY date, start_time
    """)
    rows = c.fetchall()

    embed = discord.Embed(
        title="📅 熱音社行事曆",
        color=0x2ecc71
    )

    if not rows:
        embed.description = "📭 目前沒有活動"
    else:
        text = ""

        for d, s, e, event, user in rows:
            text += f"🎵 {event} | {d} {s}-{e}\n"

        embed.description = text

    if calendar_message:
        await calendar_message.edit(embed=embed)

# =====================
# BOOKING MODAL
# =====================
class BookModal(Modal, title="預約練團"):

    date = TextInput(label="日期 (例: 6/20)")
    start = TextInput(label="開始時間 (例: 18:00)")
    end = TextInput(label="結束時間 (例: 21:00)")
    event = TextInput(label="活動名稱")

    async def on_submit(self, interaction: discord.Interaction):

        date = self.date.value.strip()
        start = self.start.value.strip()
        end = self.end.value.strip()
        event = self.event.value.strip()

        # 時間轉換
        try:
            new_start = to_min(start)
            new_end = to_min(end)
        except:
            await interaction.response.send_message("時間格式錯誤", ephemeral=True)
            return

        if new_end <= new_start:
            await interaction.response.send_message("時間不合理", ephemeral=True)
            return

        # overlap check（最穩版本）
        c.execute("SELECT start_time, end_time FROM bookings WHERE date = ?", (date,))
        rows = c.fetchall()

        for s, e in rows:
            if new_start < to_min(e) and new_end > to_min(s):
                await interaction.response.send_message("時間衝突", ephemeral=True)
                return

        # insert
        c.execute("""
            INSERT INTO bookings (date, start_time, end_time, event, user)
            VALUES (?, ?, ?, ?, ?)
        """, (
            date,
            start,
            end,
            event,
            str(interaction.user.id)  # ✅ 一定要 ID
        ))

        conn.commit()

        await interaction.response.send_message(
            "✅ 預約成功",
            ephemeral=True,
            delete_after=3
        )
        await send_calendar(interaction.channel)

# =====================
# CANCEL
# =====================


class CancelView(View):
    def __init__(self, user_id):
        super().__init__(timeout=60)

        c.execute("""
            SELECT id, date, start_time, end_time, event
            FROM bookings
            WHERE user = ?
        """, (str(user_id),))

        rows = c.fetchall()

        options = []

        for i, d, s, e, event in rows:
            options.append(
                discord.SelectOption(
                    label=f"{event} | {d} {s}-{e}",
                    value=str(i)
                )
            )

        if not options:
            options = [
                discord.SelectOption(label="沒有活動", value="none")
            ]

        self.add_item(CancelSelect(options))


class CancelSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(
            placeholder="選擇要取消的活動",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):

        if self.values[0] == "none":
            await interaction.response.send_message(
                "❌ 沒有可取消的活動",
                ephemeral=True
            )
            return

        c.execute("DELETE FROM bookings WHERE id = ?", (self.values[0],))
        conn.commit()

        # 1️⃣ 回覆
        await interaction.response.send_message("🗑️ 已取消活動", ephemeral=True)

        # 2️⃣ 刪選單訊息
        await interaction.message.delete()

        # ⏱ 等 3 秒刪掉「這則回覆」
        await asyncio.sleep(3)
        await interaction.delete_original_response()

        # 3️⃣ 重新發行事曆（重點）
        await send_calendar(interaction.channel)

# =====================
# COMMANDS
# =====================
@bot.command()
async def schedule(ctx):

    try:
        await ctx.message.delete()
    except:
        pass

    cleanup_expired()

    c.execute("""
        SELECT date, start_time, end_time, event, user
        FROM bookings
        ORDER BY date, start_time
    """)
    rows = c.fetchall()

    embed = discord.Embed(
        title="📅 熱音社行事曆",
        color=0x2ecc71
    )

    if not rows:
        embed.description = "📭 目前沒有活動"
        await ctx.send(embed=embed)
        return

    now = now_min()
    today_str = today()

    today_text = ""
    future_text = ""

    for date, start, end, event, user in rows:

        try:
            member = await bot.fetch_user(int(user))
            user_name = member.name
        except:
            user_name = str(user)

        is_today = (date == today_str)

        status = ""
        if is_today and to_min(start) <= now <= to_min(end):
            status = " 🔴進行中"

        block = (
            "```yaml\n"
            f"🎵 {event}{status}\n"
            f"📅 {date}\n"
            f"⏰ {start}-{end}\n"
            f"👤 {user_name}\n"
            "```\n"
        )

        if is_today:
            today_text += block
        else:
            future_text += block

    embed.add_field(name="📌 今天", value=today_text or "沒有活動", inline=False)
    embed.add_field(name="📌 未來", value=future_text or "沒有未來活動", inline=False)

    embed.set_footer(text="📌 用 !book 預約 / !schedule 查看")

    # 🔥 重點：一定是新訊息
    await ctx.send(embed=embed)


@bot.command()
async def book(ctx):
    await ctx.message.delete()
    await ctx.send("📅 預約系統", view=BookView(), ephemeral=True)

@bot.command()
async def cancel(ctx):

    try:
        await ctx.message.delete()
    except:
        pass

    c.execute("""
        SELECT id
        FROM bookings
        WHERE user = ?
    """, (str(ctx.author.id),))

    rows = c.fetchall()

    if not rows:
        msg = await ctx.send("❌ 你目前沒有活動可以取消")
        await msg.delete(delay=3)
        return

    await ctx.send(
        "🗑️ 請選擇要取消的活動",
        view=CancelView(ctx.author.id)
    )

    await send_calendar(ctx.channel)



@bot.command()
async def clearall(ctx, confirm=None):

    # 只有管理員能用
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ 你沒有權限使用此指令")
        return

    if confirm != "YES":
        await ctx.send(
            "⚠️ 此操作會刪除所有預約\n"
            "請輸入：`!clearall YES` 確認"
        )
        return

    c.execute("DELETE FROM bookings")
    conn.commit()

    await ctx.send("🗑️ 已清空所有預約")

    await send_calendar(ctx.channel)

# =====================
# BOOK BUTTON
# =====================
class BookView(View):

    @discord.ui.button(label="📅 預約練團", style=discord.ButtonStyle.green)
    async def book_button(self, interaction: discord.Interaction, button: Button):

        # 刪掉預約面板
        try:
            await interaction.message.delete()
        except:
            pass

        await interaction.response.send_modal(BookModal())

# =====================
# CALENDAR
# =====================
calendar_message = None  # 放在全域

async def send_calendar(channel):
    global calendar_message

    c.execute("""
        SELECT date, start_time, end_time, event, user
        FROM bookings
        ORDER BY date, start_time
    """)
    rows = c.fetchall()

    embed = discord.Embed(
        title="📅 熱音社行事曆",
        color=0x2ecc71
    )

    if not rows:
        embed.description = "📭 目前沒有活動"

    else:
        now = now_min()
        today_str = today()

        today_block = ""
        future_block = ""

        for date, start, end, event, user in rows:

            try:
                member = await channel.guild.fetch_member(int(user))
                user_name = member.display_name
            except:
                user_name = "未知用戶"

            is_today = (date == today_str)

            status = ""
            if is_today:
                try:
                    if to_min(start) <= now <= to_min(end):
                        status = " 🔴進行中"
                except:
                    pass

            block = (
                "```yaml\n"
                f"🎵 {event}{status}\n"
                f"📅 {date}\n"
                f"⏰ {start}-{end}\n"
                f"👤 {user_name}\n"
                "```"
            )

            if is_today:
                today_block += block + "\n"
            else:
                future_block += block + "\n"

        embed.add_field(name="📌 今天", value=today_block or "沒有活動", inline=False)
        embed.add_field(name="📌 未來", value=future_block or "沒有未來活動", inline=False)

    embed.set_footer(text="📌 用 !book 預約 / !schedule 查看")

    # =========================
    # 🔥 核心：edit or create
    # =========================
    if calendar_message is None:
        calendar_message = await channel.send(embed=embed)
    else:
        await calendar_message.edit(embed=embed)

# =====================
# RUN
# =====================
@bot.event
async def on_ready():
    print("Bot online")

    print(TOKEN)

bot.run(TOKEN)