import os
import discord
import requests
import google.generativeai as genai
from discord.ext import commands, tasks
from dotenv import load_dotenv
from datetime import datetime

# === Load Environment ===
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATA_URL = os.getenv("DATA_URL")
COMMITS_URL = os.getenv("COMMITS_URL")

# === Setup Gemini ===
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

# === Setup Discord ===
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# === Cache produk dan commit terakhir ===
data_cache = []
last_commit_hash = None


# === Fungsi ambil data dari GitHub ===
def fetch_data():
    global data_cache
    try:
        res = requests.get(DATA_URL, timeout=10)
        if res.status_code == 200:
            lines = res.text.strip().split("\n")
            parsed = []
            for line in lines:
                if "|" in line:
                    parts = [p.strip() for p in line.split("|")]
                    if len(parts) >= 4:
                        parsed.append({
                            "name": parts[0],
                            "price": parts[1],
                            "desc": parts[2],
                            "stock": parts[3]
                        })
            data_cache = parsed
            print("✅ Data produk diperbarui dari GitHub.")
        else:
            print(f"⚠️ Gagal ambil data. Status {res.status_code}")
    except Exception as e:
        print(f"❌ Error fetch data: {e}")


# === Cek commit terakhir GitHub ===
def get_latest_commit():
    try:
        res = requests.get(COMMITS_URL, timeout=10)
        if res.status_code == 200:
            data = res.json()
            return data["sha"]
        else:
            print(f"⚠️ Gagal ambil commit. Status {res.status_code}")
            return None
    except Exception as e:
        print(f"❌ Error get commit: {e}")
        return None


# === Auto update tiap 5 menit ===
@tasks.loop(seconds=300)
async def auto_update_data():
    global last_commit_hash

    new_commit = get_latest_commit()
    if new_commit and new_commit != last_commit_hash:
        print("🔄 Deteksi perubahan di GitHub, memperbarui data...")
        fetch_data()
        last_commit_hash = new_commit
    else:
        print("⏳ Tidak ada perubahan di GitHub.")


@bot.event
async def on_ready():
    global last_commit_hash
    print(f"🤖 Bot aktif sebagai {bot.user}")

    # Ambil commit pertama
    last_commit_hash = get_latest_commit()
    fetch_data()

    auto_update_data.start()


# === Logging interaksi ===
def log_interaction(user, query, answer):
    with open("logs.txt", "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {user}: {query}\n")
        f.write(f"→ {answer}\n\n")


# === Command utama ===
@bot.command(name="tanya")
async def tanya(ctx, *, query: str):
    if not data_cache:
        await ctx.reply("⚠️ Data produk belum siap, coba lagi sebentar.")
        return

    related_products = [
        p for p in data_cache if p["name"].lower() in query.lower()
    ]

    if not related_products:
        answer = (
            "Maaf, saya tidak memiliki informasi tentang itu. "
            "Silakan ajukan pertanyaan seputar produk yang tersedia di Luxury VIP."
        )
        embed = discord.Embed(
            title="💬 Jawaban Luxury VIP",
            description=answer,
            color=discord.Color.red()
        )
        await ctx.reply(embed=embed)
        log_interaction(ctx.author, query, answer)
        return

    context = "Berikut detail produk yang tersedia:\n\n"
    for p in related_products:
        context += f"- {p['name']}: Rp {p['price']}, {p['desc']} (stok: {p['stock']})\n"
    context += "\nJawab pertanyaan dengan sopan dan profesional seperti customer service Luxury VIP."

    try:
        response = model.generate_content([
            {"role": "system", "content": "Kamu adalah customer service Luxury VIP yang ramah dan profesional."},
            {"role": "user", "content": f"{context}\n\nPertanyaan: {query}"}
        ])

        answer = response.text.strip()

        embed = discord.Embed(
            title="💬 Jawaban Luxury VIP",
            description=answer,
            color=discord.Color.gold()
        )
        await ctx.reply(embed=embed)

        log_interaction(ctx.author, query, answer)

    except Exception as e:
        print(f"❌ Error AI: {e}")
        await ctx.reply("⚠️ Terjadi kesalahan saat memproses pertanyaan.")


# === Command !faq untuk daftar produk ===
@bot.command(name="faq")
async def faq(ctx):
    if not data_cache:
        await ctx.reply("⚠️ Data produk belum siap, coba lagi sebentar.")
        return

    embed = discord.Embed(
        title="📘 Daftar Produk Luxury VIP",
        description="Berikut produk yang tersedia saat ini:",
        color=discord.Color.blue()
    )

    for item in data_cache[:10]:
        embed.add_field(
            name=f"{item['name']} - Rp {item['price']}",
            value=f"{item['desc']}\n📦 Stok: {item['stock']}",
            inline=False
        )

    embed.set_footer(text="Gunakan !tanya <pertanyaan> untuk info lebih detail.")
    await ctx.reply(embed=embed)


# === Command bantuan ===
@bot.command(name="help")
async def help_command(ctx):
    embed = discord.Embed(
        title="💡 Bantuan Luxury VIP Bot",
        description=(
            "**!tanya <pertanyaan>** → Ajukan pertanyaan seputar produk.\n"
            "**!faq** → Lihat daftar produk dan stok.\n"
            "Bot otomatis update saat ada perubahan di GitHub."
        ),
        color=discord.Color.teal()
    )
    await ctx.reply(embed=embed)


# === Jalankan bot ===
bot.run(DISCORD_TOKEN)
