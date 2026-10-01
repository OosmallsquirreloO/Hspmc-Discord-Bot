import asyncio
import os
import re
import sqlite3
import unicodedata
from datetime import date, datetime, time as dt_time, timedelta, timezone

import discord
from discord.ext import commands, tasks
from discord.ui import Button, Modal, Select, TextInput, View
from dotenv import load_dotenv

# =====================
# 基本設定
# =====================
load_dotenv()  # 一定要在 os.getenv 之前
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("找不到 DISCORD_TOKEN，請檢查 .env")

CHECK_INTERVAL_SEC = 30            # 每幾秒檢查一次「活動開始 / 結束」
TZ = timezone(timedelta(hours=8))  # 台灣時間 (UTC+8)，不受主機時區影響
WEEK = "一二三四五六日"

# 可以在 !setadmin 面板裡調整的設定 (預設值)
DEFAULTS = {
    "cal_title": "🎸 熱音社行事曆",
    "color_ongoing": "E74C3C",   # 進行中：紅
    "color_event": "F1C40F",     # 重要活動：金
    "color_practice": "2ECC71",  # 社辦預約：綠
    "future_shown": "15",        # 「未來」最多顯示幾筆
    "max_days_ahead": "180",     # 社辦預約最多預約幾天後
    "max_hours": "0",            # 社辦預約單次最長幾小時 (0 = 不限制)
}


# =====================
# 時間 / 文字工具
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


def infer_date(mo: int, d: int, today: date, limit=None):
    """
    沒有年份時推算年份：今年這天還沒過 -> 今年；已經過了 -> 明年。
    limit 不是 None 時，推算結果離今天超過 limit 天就視為「輸入了過去的日期」回傳 None。
    """
    for y in (today.year, today.year + 1):
        try:
            cand = date(y, mo, d)
        except ValueError:
            continue
        if cand >= today:
            if limit is not None and (cand - today).days > limit:
                return None
            return cand
    return None


def fmt_date(d: date, with_year: bool = False) -> str:
    base = f"{d.month}/{d.day} ({WEEK[d.weekday()]})"
    return f"{d.year}/{base}" if with_year else base


def countdown(d: date, today: date) -> str:
    diff = (d - today).days
    if diff <= 0:
        return "🔥 就是今天！"
    if diff == 1:
        return "⏳ 明天"
    return f"⏳ 還有 {diff} 天"


def trunc(text: str, limit: int = 20) -> str:
    """依「顯示寬度」截斷 (中文算 2 格)，避免手機上的程式碼框被撐寬"""
    out, w = [], 0
    for ch in text:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w + cw > limit:
            return "".join(out).rstrip() + "…"
        out.append(ch)
        w += cw
    return "".join(out)


def clean(text: str) -> str:
    """放進程式碼區塊前，避免使用者輸入的 ` 把框弄壞"""
    return text.replace("`", "'")


# =====================
# DB
# =====================
conn = sqlite3.connect("booking.db")
conn.row_factory = sqlite3.Row

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
for table in ("calendar_refs", "menu_refs", "admin_refs"):
    conn.execute(f"""
    CREATE TABLE IF NOT EXISTS {table} (
        guild_id INTEGER PRIMARY KEY,
        channel_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL
    )
    """)
conn.execute("""
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
""")
conn.commit()


# ---- 設定讀寫 ----
def get_setting(key: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else DEFAULTS[key]


def set_setting(key: str, value: str):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def get_int(key: str) -> int:
    try:
        return int(get_setting(key))
    except ValueError:
        return int(DEFAULTS[key])


def get_color(key: str) -> int:
    try:
        return int(get_setting(key), 16)
    except ValueError:
        return int(DEFAULTS[key], 16)


def migrate_old_table():
    """把最舊版的 bookings (日期是 '9/15' 沒年份) 轉成新版；已過期的舊資料會被丟掉"""
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='bookings'"
    ).fetchone()
    if not exists:
        return
    today_d = now().date()
    for r in conn.execute("SELECT * FROM bookings").fetchall():
        try:
            mo, d = map(int, r["date"].split("/"))
            full = infer_date(mo, d, today_d, get_int("max_days_ahead"))
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


def find_conflict(d: date, s_min: int, e_min: int, exclude_id=None):
    for r in conn.execute(
        "SELECT id, event, start_time, end_time FROM bookings_v2 WHERE date = ?",
        (d.isoformat(),),
    ):
        if exclude_id is not None and r["id"] == exclude_id:
            continue
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
# 日期 / 時段檢查 (預約、編輯共用)
# =====================
def practice_date(text: str):
    """社辦預約的日期 (不用輸入年份)。回傳 (date, None) 或 (None, 錯誤訊息)"""
    md = parse_md(text)
    if not md:
        return None, "日期格式錯誤，請用 6/20 這種格式"
    limit = get_int("max_days_ahead")
    d = infer_date(md[0], md[1], now().date(), limit)
    if d is None:
        return None, f"日期已經過了，或超過 {limit} 天後，無法預約"
    return d, None


def parse_any_date(text: str, kind: str):
    """管理員編輯 / 清空用：可輸入 2026/6/20 或 6/20"""
    text = text.strip()
    m = re.fullmatch(r"(\d{4})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{1,2})", text)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3])), None
        except ValueError:
            return None, "這一天不存在"
    md = parse_md(text)
    if not md:
        return None, "日期格式錯誤，請用 6/20 或 2026/6/20"
    limit = get_int("max_days_ahead") if kind == "practice" else None
    d = infer_date(md[0], md[1], now().date(), limit)
    if d is None:
        return None, "日期已經過了" + (f"，或超過 {limit} 天後" if limit else "")
    return d, None


def validate_slot(kind, d: date, start_s, end_s, exclude_id=None):
    """檢查 時間格式 -> 合理性 -> 單次時數上限 -> 衝突。回傳 (錯誤訊息或 None, 開始, 結束)"""
    s, e = parse_time(start_s), parse_time(end_s)
    if not s or not e:
        return "時間格式錯誤，請用 18:00 這種格式", None, None
    if e[0] <= s[0]:
        return "結束時間必須晚於開始時間", None, None
    if datetime.combine(d, dt_time.fromisoformat(e[1])) <= now():
        return "這個時段已經過了", None, None
    max_h = get_int("max_hours")
    if kind == "practice" and max_h and (e[0] - s[0]) > max_h * 60:
        return f"單次社辦預約最長 {max_h} 小時", None, None
    clash = find_conflict(d, s[0], e[0], exclude_id)
    if clash:
        return (
            f"時間衝突：與「{clash['event']}」({clash['start_time']}-{clash['end_time']}) 重疊",
            None, None,
        )
    return None, s, e


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
# 行事曆外觀 (多個色條區塊，手機 / 電腦都適用)
# =====================
def disp_name(guild, b) -> str:
    member = guild.get_member(int(b["user"])) if guild else None
    if member:
        return member.display_name
    return b.get("user_name") or "未知用戶"


def block(lang: str, lines: list) -> str:
    return f"```{lang}\n" + "\n".join(lines) + "\n```"


def ongoing_block(b, guild) -> str:
    """進行中：diff 的 + 會顯示成綠色"""
    lines = [f"+ 🔴 {clean(trunc(b['event'], 20))}"]
    if b["kind"] == "event":
        lines.append("+ ⭐ 重要活動")
        lines.append(f"+ 📅 {fmt_date(b['d'], True)}")
    lines.append(f"+ ⏰ {b['start_time']}–{b['end_time']}")
    lines.append(f"+ 👤 {clean(trunc(disp_name(guild, b), 16))}")
    return block("diff", lines)


def event_block(b, guild, today: date) -> str:
    """重要活動：fix 會顯示成黃色，加上倒數天數"""
    lines = [
        f"✨ {clean(trunc(b['event'], 18))} ✨",
        f"📅 {fmt_date(b['d'], True)}",
        f"⏰ {b['start_time']}–{b['end_time']}",
        f"👤 {clean(trunc(disp_name(guild, b), 16))}",
        countdown(b["d"], today),
    ]
    return block("fix", lines)


def practice_block(b, guild, show_date: bool = True) -> str:
    """社辦預約：ini 的 [標題] 會顯示成藍色"""
    lines = [f"[🏠 {clean(trunc(b['event'], 20))}]"]
    if show_date:
        lines.append(f"📅 {fmt_date(b['d'])}")
    lines.append(f"⏰ {b['start_time']}–{b['end_time']}")
    lines.append(f"👤 {clean(trunc(disp_name(guild, b), 16))}")
    return block("ini", lines)


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


def capped(items: list, cap: int):
    """超過上限就截斷，回傳 (顯示的項目, 被省略的筆數)"""
    return items[:cap], max(0, len(items) - cap)


def build_embeds(guild) -> list:
    """
    同一則訊息裡放多個 Embed，每個有自己的顏色條：
    標題(灰) / 進行中(紅) / 重要活動(金) / 社辦預約(綠)
    """
    n = now()
    today_d = n.date()
    rows = [b for b in load_bookings() if b["end_dt"] > n]  # 已結束的不顯示

    ongoing = [b for b in rows if b["start_dt"] <= n]
    upcoming = [b for b in rows if b["start_dt"] > n]
    events = [b for b in upcoming if b["kind"] == "event"]
    today_p = [b for b in upcoming if b["kind"] == "practice" and b["d"] == today_d]
    future_p = [b for b in upcoming if b["kind"] == "practice" and b["d"] > today_d]

    header = f"📍 今天是 **{fmt_date(today_d)}**"
    head = discord.Embed(title=get_setting("cal_title"), color=0x95A5A6)
    embeds = [head]

    if not rows:
        head.description = f"{header}\n\n📭 目前沒有任何活動"
    else:
        head.description = f"{header}　·　共 **{len(rows)}** 筆"

        if ongoing:
            shown, more = capped(ongoing, 8)
            desc = "\n".join(ongoing_block(b, guild) for b in shown)
            if more:
                desc += f"\n*…還有 {more} 筆*"
            embeds.append(
                discord.Embed(
                    title="🔴 進行中", description=desc, color=get_color("color_ongoing")
                )
            )

        if events:
            shown, more = capped(events, 8)
            desc = "\n".join(event_block(b, guild, today_d) for b in shown)
            if more:
                desc += f"\n*…還有 {more} 筆*"
            embeds.append(
                discord.Embed(
                    title="🌟 重要活動", description=desc, color=get_color("color_event")
                )
            )

        practice = discord.Embed(title="🏠 社辦預約", color=get_color("color_practice"))
        t_shown, t_more = capped(today_p, 8)
        t_lines = [practice_block(b, guild, False) for b in t_shown] or ["*今天沒有其他預約*"]
        if t_more:
            t_lines.append(f"*…還有 {t_more} 筆*")
        add_field_chunked(practice, "📌 今天", t_lines)

        f_shown, f_more = capped(future_p, min(max(get_int("future_shown"), 1), 20))
        f_lines = [practice_block(b, guild, True) for b in f_shown] or ["*沒有未來預約*"]
        if f_more:
            f_lines.append(f"*…還有 {f_more} 筆*")
        add_field_chunked(practice, "📅 未來", f_lines)
        embeds.append(practice)

    embeds[-1].set_footer(text="用選單按鈕預約　·　最後更新")
    embeds[-1].timestamp = discord.utils.utcnow()
    return embeds


# =====================
# 行事曆訊息：更新 / 建立
# =====================
calendar_lock = asyncio.Lock()


def get_ref(guild_id: int, table: str = "calendar_refs"):
    return conn.execute(
        f"SELECT * FROM {table} WHERE guild_id = ?", (guild_id,)
    ).fetchone()


def save_ref(guild_id: int, channel_id: int, message_id: int, table: str = "calendar_refs"):
    conn.execute(
        f"INSERT INTO {table} (guild_id, channel_id, message_id) VALUES (?,?,?) "
        "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id, "
        "message_id=excluded.message_id",
        (guild_id, channel_id, message_id),
    )
    conn.commit()


async def _upsert_in_channel(guild, channel):
    """在指定頻道更新行事曆；有舊的就更新，沒有就新建；舊的在別的頻道就移過來"""
    embeds = build_embeds(guild)
    ref = get_ref(guild.id)

    if ref and ref["channel_id"] == channel.id:
        try:
            msg = await channel.fetch_message(ref["message_id"])
            await msg.edit(embeds=embeds)
            return
        except discord.NotFound:
            pass  # 原訊息被刪了 -> 往下重新建立
    elif ref:
        try:
            old_ch = bot.get_channel(ref["channel_id"]) or await bot.fetch_channel(
                ref["channel_id"]
            )
            old_msg = await old_ch.fetch_message(ref["message_id"])
            await old_msg.delete()
        except discord.HTTPException:
            pass

    msg = await channel.send(embeds=embeds)
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

    embeds = build_embeds(guild)
    try:
        msg = await channel.fetch_message(ref["message_id"])
        await msg.edit(embeds=embeds)
    except discord.NotFound:
        msg = await channel.send(embeds=embeds)  # 被刪掉了 -> 補一個新的
        save_ref(guild.id, channel.id, msg.id)
    except discord.HTTPException as e:
        print(f"更新行事曆失敗: {e}")


async def refresh_calendar(guild=None, fallback_channel=None):
    """
    給 guild：更新該伺服器「已設定」的行事曆 (位置由 !setschedule 決定，不會亂搬)。
              如果還沒有行事曆，且有給 fallback_channel，就在那個頻道建立一個。
    不給參數：更新所有已存在的行事曆 (自動排程用)
    """
    async with calendar_lock:
        if guild is None:
            for ref in conn.execute("SELECT * FROM calendar_refs").fetchall():
                await _refresh_saved(ref)
            return
        ref = get_ref(guild.id)
        if ref:
            await _refresh_saved(ref)
        elif fallback_channel is not None:
            await _upsert_in_channel(guild, fallback_channel)


async def set_calendar_channel(guild, channel):
    """!setschedule：把行事曆固定在這個頻道 (舊的在別的頻道會被刪掉並搬過來)"""
    async with calendar_lock:
        await _upsert_in_channel(guild, channel)


# =====================
# 預約表單
# =====================
async def finish_booking(interaction, kind, d, start_s, end_s, event, is_event=False):
    """共用流程：檢查時間 -> 合理性 -> 衝突 -> 存入 -> 提示 3 秒 -> 更新行事曆"""
    err, s, e = validate_slot(kind, d, start_s, end_s)
    if err:
        return await reject(interaction, err)

    insert_booking(kind, d, s[1], e[1], event, interaction.user)  # 以使用者 ID 儲存

    label = "重要活動已新增" if is_event else "預約成功"
    note = ""
    if interaction.guild and not get_ref(interaction.guild.id):
        note = "\n⚠️ 尚未設定行事曆頻道，請管理員在行事曆頻道輸入 !setschedule"
    await interaction.response.send_message(
        f"✅ {label}{note}", ephemeral=True, delete_after=8 if note else 3
    )
    await refresh_calendar(interaction.guild)


class PracticeModal(Modal, title="社辦預約"):
    event = TextInput(label="活動名稱", placeholder="例: 團練 / 個人練習", max_length=50)
    date_in = TextInput(label="日期", placeholder="例: 6/20", max_length=10)
    start = TextInput(label="開始時間", placeholder="例: 18:00", max_length=5)
    end = TextInput(label="結束時間", placeholder="例: 21:00", max_length=5)

    async def on_submit(self, interaction: discord.Interaction):
        d, err = practice_date(self.date_in.value)
        if err:
            return await reject(interaction, err)
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
# !book 面板 (舊指令，保留)
# =====================
class BookView(View):
    def __init__(self, owner_id: int, admin: bool):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.message = None

        practice_btn = Button(label="🏠 社辦預約", style=discord.ButtonStyle.green)
        practice_btn.callback = self.open_practice
        self.add_item(practice_btn)

        if admin:
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
# 取消選單
# =====================
def cancellable(member):
    """一般人：只有自己建立的社辦預約；管理員：所有預約 (含重要活動)"""
    n = now()
    admin = is_admin(member)
    uid = str(member.id)
    rows = [
        b for b in load_bookings()
        if b["end_dt"] > n
        and (admin or (b["kind"] == "practice" and b["user"] == uid))
    ]
    return rows[:25]  # Discord 下拉選單最多 25 項


class CancelView(View):
    def __init__(self, owner, rows, guild):
        super().__init__(timeout=60)
        self.owner_id = owner.id
        self.selected_id = None
        self.message = None   # 一般訊息 (用指令叫出來時)
        self.origin = None    # 私人訊息 (用選單按鈕叫出來時)

        admin = is_admin(owner)
        options = []
        for b in rows:
            is_event = b["kind"] == "event"
            label = (
                f"{'⭐ ' if is_event else '🏠 '}{b['event']} | "
                f"{fmt_date(b['d'])} {b['start_time']}-{b['end_time']}"
            )
            parts = []
            if is_event:
                parts.append(f"重要活動 · {fmt_date(b['d'], True)}")
            if admin:
                parts.append(f"👤 {disp_name(guild, b)}")
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(b["id"]),
                    description=(" · ".join(parts)[:100] or None),
                )
            )

        self.select = Select(placeholder="選擇要取消的項目", options=options, row=0)
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ 這不是你的選單", ephemeral=True)
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
            allowed = is_admin(interaction.user) or (
                row["kind"] == "practice" and row["user"] == uid
            )
            if not allowed:
                text = "❌ 你沒有權限取消這個項目"
            else:
                conn.execute("DELETE FROM bookings_v2 WHERE id = ?", (row["id"],))
                conn.commit()
                text = f"🗑️ 已取消：{row['event']}"

        self.stop()
        await interaction.response.edit_message(content=text, view=None)
        await refresh_calendar(interaction.guild)

        await asyncio.sleep(3)
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            await safe_delete(interaction.message)

    async def on_timeout(self):
        if self.origin is not None:
            try:
                await self.origin.delete_original_response()
            except discord.HTTPException:
                pass
        else:
            await safe_delete(self.message)


# =====================
# 一般使用者的總選單 (永久按鈕，重啟後仍有效)
# =====================
def menu_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🎸 熱音社預約選單",
        description="點下方按鈕就能操作即可 \n結果只有你看得到，用完會自動消失 i人友善",
        color=0x5865F2,
    )
    embed.add_field(name="🏠 社辦預約", value="預約社辦使用時段", inline=False)
    embed.add_field(name="🌟 新增重要活動", value="僅限管理員", inline=False)
    embed.add_field(
        name="🗑️ 取消我的行程", value="取消自己的預約", inline=False
    )
    embed.add_field(name="📋 我的預約", value="查看你自己的預約", inline=False)
    return embed


class MenuView(View):
    def __init__(self):
        super().__init__(timeout=None)  # 永久有效

    @discord.ui.button(
        label="社辦預約", emoji="🏠", style=discord.ButtonStyle.green,
        custom_id="menu:book", row=0,
    )
    async def book_btn(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(PracticeModal())

    @discord.ui.button(
        label="新增重要活動", emoji="🌟", style=discord.ButtonStyle.blurple,
        custom_id="menu:event", row=0,
    )
    async def event_btn(self, interaction: discord.Interaction, button: Button):
        if not is_admin(interaction.user):
            return await reject(interaction, "只有管理員可以新增重要活動")
        await interaction.response.send_modal(EventModal())

    @discord.ui.button(
        label="取消我的行程", emoji="🗑️", style=discord.ButtonStyle.red,
        custom_id="menu:cancel", row=1,
    )
    async def cancel_btn(self, interaction: discord.Interaction, button: Button):
        rows = cancellable(interaction.user)
        if not rows:
            return await interaction.response.send_message(
                "❌ 你目前沒有可以取消的行程", ephemeral=True, delete_after=3
            )
        view = CancelView(interaction.user, rows, interaction.guild)
        view.origin = interaction
        await interaction.response.send_message(
            "🗑️ 請選擇要取消的行程，再按「確認刪除」", view=view, ephemeral=True
        )

    @discord.ui.button(
        label="我的預約", emoji="📋", style=discord.ButtonStyle.secondary,
        custom_id="menu:mine", row=1,
    )
    async def mine_btn(self, interaction: discord.Interaction, button: Button):
        n = now()
        uid = str(interaction.user.id)
        mine = [b for b in load_bookings() if b["user"] == uid and b["end_dt"] > n]
        if not mine:
            return await interaction.response.send_message(
                "📭 你目前沒有任何預約", ephemeral=True, delete_after=5
            )

        lines = []
        for b in mine:
            icon = "⭐" if b["kind"] == "event" else "🏠"
            live = " 🔴進行中" if b["start_dt"] <= n else ""
            lines.append(
                f"{icon} {clean(trunc(b['event'], 20))}{live}\n"
                f"   📅 {fmt_date(b['d'], b['kind'] == 'event')}\n"
                f"   ⏰ {b['start_time']}–{b['end_time']}"
            )
        body = "\n\n".join(lines)[:1800]
        await interaction.response.send_message(
            f"📋 **我的預約** (共 {len(mine)} 筆)\n```\n{body}\n```",
            ephemeral=True,
            delete_after=30,
        )


# =====================
# 管理員面板 (!setadmin)
# =====================
class ConfirmView(View):
    """危險操作前的二次確認 (私人訊息)"""

    def __init__(self, owner_id: int, action, done_text: str):
        super().__init__(timeout=30)
        self.owner_id = owner_id
        self.action = action          # 一個會回傳「刪除筆數」的函式
        self.done_text = done_text    # 用 {n} 代表筆數
        self.origin = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ 這不是你的選單", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="確認刪除", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: Button):
        n = self.action()
        self.stop()
        await interaction.response.edit_message(
            content=self.done_text.format(n=n), view=None
        )
        await refresh_calendar(interaction.guild)
        await asyncio.sleep(3)
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: Button):
        self.stop()
        await interaction.response.edit_message(content="已取消操作", view=None)
        await asyncio.sleep(2)
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        if self.origin is not None:
            try:
                await self.origin.delete_original_response()
            except discord.HTTPException:
                pass


class EditModal(Modal):
    """編輯既有預約 / 活動 (欄位會預先填入目前的內容)"""

    def __init__(self, row: dict, parent_view):
        super().__init__(title="編輯預約 / 活動")
        self.row = row
        self.parent_view = parent_view
        d = date.fromisoformat(row["date"])
        self.event = TextInput(label="活動名稱", default=row["event"], max_length=50)
        self.date_in = TextInput(
            label="日期 (可含年份，例: 2026/6/20)",
            default=f"{d.year}/{d.month}/{d.day}",
            max_length=12,
        )
        self.start = TextInput(label="開始時間", default=row["start_time"], max_length=5)
        self.end = TextInput(label="結束時間", default=row["end_time"], max_length=5)
        for item in (self.event, self.date_in, self.start, self.end):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            return await reject(interaction, "只有管理員可以編輯")

        exists = conn.execute(
            "SELECT id FROM bookings_v2 WHERE id = ?", (self.row["id"],)
        ).fetchone()
        if not exists:
            return await reject(interaction, "這個項目已經不存在了")

        d, err = parse_any_date(self.date_in.value, self.row["kind"])
        if err:
            return await reject(interaction, err)
        err, s, e = validate_slot(
            self.row["kind"], d, self.start.value, self.end.value,
            exclude_id=self.row["id"],
        )
        if err:
            return await reject(interaction, err)

        conn.execute(
            "UPDATE bookings_v2 SET event=?, date=?, start_time=?, end_time=? WHERE id=?",
            (self.event.value.strip(), d.isoformat(), s[1], e[1], self.row["id"]),
        )
        conn.commit()

        await interaction.response.send_message(
            "✅ 已更新", ephemeral=True, delete_after=3
        )
        await refresh_calendar(interaction.guild)

        # 把「選擇項目」的那則私人訊息收掉
        self.parent_view.stop()
        if self.parent_view.origin is not None:
            try:
                await self.parent_view.origin.delete_original_response()
            except discord.HTTPException:
                pass


class EditSelectView(View):
    def __init__(self, owner, rows, guild):
        super().__init__(timeout=60)
        self.owner_id = owner.id
        self.origin = None

        options = []
        for b in rows:
            is_event = b["kind"] == "event"
            label = (
                f"{'⭐ ' if is_event else '🏠 '}{b['event']} | "
                f"{fmt_date(b['d'])} {b['start_time']}-{b['end_time']}"
            )
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(b["id"]),
                    description=f"👤 {disp_name(guild, b)}"[:100],
                )
            )
        self.select = Select(placeholder="選擇要編輯的項目", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ 這不是你的選單", ephemeral=True)
            return False
        return True

    async def on_select(self, interaction: discord.Interaction):
        row = conn.execute(
            "SELECT * FROM bookings_v2 WHERE id = ?", (int(self.select.values[0]),)
        ).fetchone()
        if row is None:
            return await interaction.response.edit_message(
                content="❌ 這個項目已經不存在了", view=None
            )
        await interaction.response.send_modal(EditModal(dict(row), self))

    async def on_timeout(self):
        if self.origin is not None:
            try:
                await self.origin.delete_original_response()
            except discord.HTTPException:
                pass


class ClearDayModal(Modal, title="清空某一天"):
    date_in = TextInput(label="日期", placeholder="例: 6/20 或 2026/6/20", max_length=12)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            return await reject(interaction, "只有管理員可以使用")
        d, err = parse_any_date(self.date_in.value, "event")
        if err:
            return await reject(interaction, err)

        rows = conn.execute(
            "SELECT event FROM bookings_v2 WHERE date = ? ORDER BY start_time",
            (d.isoformat(),),
        ).fetchall()
        if not rows:
            return await reject(interaction, f"{fmt_date(d, True)} 沒有任何預約")

        def action():
            conn.execute("DELETE FROM bookings_v2 WHERE date = ?", (d.isoformat(),))
            conn.commit()
            return len(rows)

        names = "\n".join(f"・{r['event']}" for r in rows[:10])
        more = f"\n…還有 {len(rows) - 10} 筆" if len(rows) > 10 else ""
        view = ConfirmView(interaction.user.id, action, "🗑️ 已刪除 {n} 筆預約")
        view.origin = interaction
        await interaction.response.send_message(
            f"⚠️ 確定要清空 **{fmt_date(d, True)}** 的 **{len(rows)}** 筆預約嗎？\n{names}{more}",
            view=view,
            ephemeral=True,
        )


class AppearanceModal(Modal):
    def __init__(self):
        super().__init__(title="行事曆外觀設定")
        self.title_in = TextInput(
            label="行事曆標題", default=get_setting("cal_title"), max_length=40
        )
        self.c_ongoing = TextInput(
            label="進行中顏色 (例: E74C3C)", default=get_setting("color_ongoing"), max_length=7
        )
        self.c_event = TextInput(
            label="重要活動顏色 (例: F1C40F)", default=get_setting("color_event"), max_length=7
        )
        self.c_practice = TextInput(
            label="社辦預約顏色 (例: 2ECC71)", default=get_setting("color_practice"), max_length=7
        )
        self.shown = TextInput(
            label="「未來」最多顯示幾筆 (1-20)", default=get_setting("future_shown"), max_length=2
        )
        for item in (self.title_in, self.c_ongoing, self.c_event, self.c_practice, self.shown):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            return await reject(interaction, "只有管理員可以使用")

        colors = {}
        for key, field in (
            ("color_ongoing", self.c_ongoing),
            ("color_event", self.c_event),
            ("color_practice", self.c_practice),
        ):
            m = re.fullmatch(r"#?([0-9a-fA-F]{6})", field.value.strip())
            if not m:
                return await reject(interaction, "顏色格式錯誤，請輸入 6 位數色碼，例如 2ECC71")
            colors[key] = m[1].upper()

        try:
            shown = int(self.shown.value.strip())
            if not 1 <= shown <= 20:
                raise ValueError
        except ValueError:
            return await reject(interaction, "顯示筆數請輸入 1 到 20 的數字")

        set_setting("cal_title", self.title_in.value.strip() or DEFAULTS["cal_title"])
        for k, v in colors.items():
            set_setting(k, v)
        set_setting("future_shown", str(shown))

        await interaction.response.send_message(
            "✅ 外觀已更新", ephemeral=True, delete_after=3
        )
        await refresh_calendar(interaction.guild)


class LimitsModal(Modal):
    def __init__(self):
        super().__init__(title="預約限制設定")
        self.days = TextInput(
            label="社辦預約最多可預約幾天後 (1-365)",
            default=get_setting("max_days_ahead"),
            max_length=3,
        )
        self.hours = TextInput(
            label="單次最長幾小時 (0 = 不限制)",
            default=get_setting("max_hours"),
            max_length=2,
        )
        self.add_item(self.days)
        self.add_item(self.hours)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            return await reject(interaction, "只有管理員可以使用")
        try:
            days = int(self.days.value.strip())
            hours = int(self.hours.value.strip())
            if not (1 <= days <= 365 and 0 <= hours <= 24):
                raise ValueError
        except ValueError:
            return await reject(interaction, "請輸入有效的數字：天數 1-365，小時 0-24")

        set_setting("max_days_ahead", str(days))
        set_setting("max_hours", str(hours))
        await interaction.response.send_message(
            "✅ 預約限制已更新 (只影響之後的新預約)", ephemeral=True, delete_after=4
        )


def admin_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🛠️ 管理員面板",
        description="只有管理員能使用。一般選單的功能你也都可以照常使用。",
        color=0xE67E22,
    )
    embed.add_field(name="✏️ 編輯預約 / 活動", value="修改名稱、日期、時間", inline=False)
    embed.add_field(name="🧹 批次清理", value="清空某一天，或清空全部", inline=False)
    embed.add_field(name="🎨 行事曆外觀", value="標題、顏色、未來顯示筆數", inline=False)
    embed.add_field(name="⚙️ 預約限制", value="最多預約幾天後、單次最長幾小時", inline=False)
    embed.add_field(name="🔄 刷新行事曆", value="立即清除過期並更新", inline=False)
    return embed


class AdminView(View):
    def __init__(self):
        super().__init__(timeout=None)  # 永久有效

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_admin(interaction.user):
            await reject(interaction, "只有管理員可以使用這個面板")
            return False
        return True

    @discord.ui.button(
        label="編輯預約/活動", emoji="✏️", style=discord.ButtonStyle.primary,
        custom_id="admin:edit", row=0,
    )
    async def edit_btn(self, interaction: discord.Interaction, button: Button):
        n = now()
        rows = [b for b in load_bookings() if b["end_dt"] > n][:25]
        if not rows:
            return await reject(interaction, "目前沒有可以編輯的預約")
        view = EditSelectView(interaction.user, rows, interaction.guild)
        view.origin = interaction
        total = sum(1 for b in load_bookings() if b["end_dt"] > n)
        note = f"\n(共 {total} 筆，只顯示最近 25 筆)" if total > 25 else ""
        await interaction.response.send_message(
            f"✏️ 請選擇要編輯的項目{note}", view=view, ephemeral=True
        )

    @discord.ui.button(
        label="刷新行事曆", emoji="🔄", style=discord.ButtonStyle.secondary,
        custom_id="admin:refresh", row=0,
    )
    async def refresh_btn(self, interaction: discord.Interaction, button: Button):
        if not get_ref(interaction.guild.id):
            return await reject(
                interaction, "尚未設定行事曆頻道，請先在行事曆頻道輸入 !setschedule"
            )
        removed = cleanup_expired()
        await interaction.response.send_message(
            f"🔄 已刷新行事曆 (清除 {removed} 筆過期)", ephemeral=True, delete_after=3
        )
        await refresh_calendar(interaction.guild)

    @discord.ui.button(
        label="目前設定", emoji="📋", style=discord.ButtonStyle.secondary,
        custom_id="admin:settings", row=0,
    )
    async def settings_btn(self, interaction: discord.Interaction, button: Button):
        max_h = get_int("max_hours")
        embed = discord.Embed(title="📋 目前設定", color=0xE67E22)
        embed.add_field(name="行事曆標題", value=get_setting("cal_title"), inline=False)
        embed.add_field(
            name="顏色",
            value=(
                f"🔴 進行中 `#{get_setting('color_ongoing')}`\n"
                f"🌟 重要活動 `#{get_setting('color_event')}`\n"
                f"🏠 社辦預約 `#{get_setting('color_practice')}`"
            ),
            inline=False,
        )
        embed.add_field(name="未來最多顯示", value=f"{get_int('future_shown')} 筆", inline=True)
        embed.add_field(
            name="最多預約", value=f"{get_int('max_days_ahead')} 天後", inline=True
        )
        embed.add_field(
            name="單次最長", value=f"{max_h} 小時" if max_h else "不限制", inline=True
        )
        await interaction.response.send_message(embed=embed, ephemeral=True, delete_after=30)

    @discord.ui.button(
        label="清空某一天", emoji="🗓️", style=discord.ButtonStyle.danger,
        custom_id="admin:clearday", row=1,
    )
    async def clearday_btn(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(ClearDayModal())

    @discord.ui.button(
        label="清空全部", emoji="💣", style=discord.ButtonStyle.danger,
        custom_id="admin:clearall", row=1,
    )
    async def clearall_btn(self, interaction: discord.Interaction, button: Button):
        total = conn.execute("SELECT COUNT(*) AS c FROM bookings_v2").fetchone()["c"]
        if total == 0:
            return await reject(interaction, "目前沒有任何預約")

        def action():
            conn.execute("DELETE FROM bookings_v2")
            conn.commit()
            return total

        view = ConfirmView(interaction.user.id, action, "🗑️ 已清空 {n} 筆預約")
        view.origin = interaction
        await interaction.response.send_message(
            f"⚠️ 確定要清空**所有** {total} 筆預約嗎？這個動作無法復原。",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(
        label="行事曆外觀", emoji="🎨", style=discord.ButtonStyle.primary,
        custom_id="admin:appearance", row=2,
    )
    async def appearance_btn(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(AppearanceModal())

    @discord.ui.button(
        label="預約限制", emoji="⚙️", style=discord.ButtonStyle.primary,
        custom_id="admin:limits", row=2,
    )
    async def limits_btn(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(LimitsModal())


# =====================
# 指令
# =====================
async def post_panel(ctx, table: str, embed, view):
    """發出面板訊息；如果之前已經發過，先刪掉舊的，避免重複"""
    old = get_ref(ctx.guild.id, table)
    if old:
        try:
            ch = bot.get_channel(old["channel_id"]) or await bot.fetch_channel(
                old["channel_id"]
            )
            old_msg = await ch.fetch_message(old["message_id"])
            await old_msg.delete()
        except discord.HTTPException:
            pass
    msg = await ctx.send(embed=embed, view=view)
    save_ref(ctx.guild.id, ctx.channel.id, msg.id, table)


@bot.command()
@commands.guild_only()
async def schedule(ctx):
    """更新行事曆 (已設定的行事曆頻道)；還沒設定就建立在這個頻道"""
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

    rows = cancellable(ctx.author)
    if not rows:
        await ctx.send("❌ 你目前沒有可以取消的行程", delete_after=3)
        return

    view = CancelView(ctx.author, rows, ctx.guild)
    view.message = await ctx.send("🗑️ 請選擇要取消的行程，再按「確認刪除」", view=view)


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


@bot.command()
@commands.guild_only()
async def setmenu(ctx):
    """(管理員) 在這個頻道建立一般使用者的總選單"""
    await safe_delete(ctx.message)
    if not is_admin(ctx.author):
        await ctx.send("❌ 只有管理員可以使用此指令", delete_after=3)
        return
    await post_panel(ctx, "menu_refs", menu_embed(), MenuView())
    await ctx.send("✅ 選單已建立", delete_after=3)


@bot.command()
@commands.guild_only()
async def setadmin(ctx):
    """(管理員) 在這個頻道建立管理員面板 (建議放在只有管理員看得到的頻道)"""
    await safe_delete(ctx.message)
    if not is_admin(ctx.author):
        await ctx.send("❌ 只有管理員可以使用此指令", delete_after=3)
        return
    await post_panel(ctx, "admin_refs", admin_embed(), AdminView())
    await ctx.send("✅ 管理員面板已建立", delete_after=3)


@bot.command()
@commands.guild_only()
async def setschedule(ctx):
    """(管理員) 把行事曆固定在這個頻道；舊的行事曆會被刪掉並搬過來"""
    await safe_delete(ctx.message)
    if not is_admin(ctx.author):
        await ctx.send("❌ 只有管理員可以使用此指令", delete_after=3)
        return

    cleanup_expired()
    await set_calendar_channel(ctx.guild, ctx.channel)
    await ctx.send("✅ 已將行事曆設定在此頻道", delete_after=3)


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
async def setup_hook():
    bot.add_view(MenuView())   # 讓重啟前發出的按鈕繼續有效
    bot.add_view(AdminView())


bot.setup_hook = setup_hook


@bot.event
async def on_ready():
    print(f"Bot online: {bot.user}")
    if not watcher.is_running():
        watcher.start()
    if not midnight_update.is_running():
        midnight_update.start()


bot.run(TOKEN)