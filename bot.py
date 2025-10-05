import os
import asyncio
import discord
import requests
import google.generativeai as genai
import logging
from discord.ext import commands, tasks
from dotenv import load_dotenv
from datetime import datetime
from discord.ui import View, Select
from typing import Optional, List, Dict

# === Setup Logging ===
# Mengatur logging untuk memantau aktivitas bot dan error
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('luxury_bot')

# Mengabaikan warning yang tidak relevan dari gRPC
logging.getLogger('grpc').setLevel(logging.ERROR)
os.environ['GRPC_ENABLE_FORK_SUPPORT'] = '0'

# === Load Environment Variables ===
# Memuat konfigurasi dari file .env
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATA_URL = os.getenv("DATA_URL")
COMMITS_URL = os.getenv("COMMITS_URL")
UPDATE_CHANNEL_ID = os.getenv("UPDATE_CHANNEL_ID")

# === Setup Gemini AI ===
# Konfigurasi API Google Gemini
genai.configure(api_key=GEMINI_API_KEY)

generation_config = {
    "temperature": 0.7,
    "top_p": 0.95,
    "top_k": 40,
    "max_output_tokens": 1024,
}

# Membuat instance model generatif
model = genai.GenerativeModel(
    "gemini-1.5-flash-latest",
    generation_config=generation_config
)

# === Setup Discord Bot ===
# Inisialisasi bot dengan intent yang diperlukan
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# === Cache & State Management ===
# Variabel untuk menyimpan data dan status
data_cache: Dict[str, List[Dict[str, str]]] = {
    "products": [],
    "faq": []
}
product_categories: Dict[str, List[Dict[str, str]]] = {}
last_commit_hash: Optional[str] = None

# === Logging Functions ===
LOG_FILE = "logs.txt"
ERROR_LOG_FILE = "errors.log"

def log_interaction(user: discord.User, query: str, answer: str) -> None:
    """Mencatat interaksi user dengan bot ke file log."""
    try:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] User: {user.name} (ID: {user.id})\n")
            f.write(f"Query: {query}\n")
            f.write(f"Answer: {answer[:200]}...\n")
            f.write("-" * 80 + "\n\n")
    except Exception as e:
        logger.error(f"Error logging interaction: {e}")

def log_error(error_msg: str) -> None:
    """Mencatat error ke file log terpisah."""
    try:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(ERROR_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {error_msg}\n\n")
    except Exception as e:
        logger.error(f"Error logging error: {e}")

# === Data Fetching and Parsing ===
def parse_data_file(content: str) -> Dict[str, List[Dict[str, str]]]:
    """Mengurai konten file data.txt menjadi produk, FAQ, dan kategori."""
    lines = content.strip().split("\n")
    products, faq_items, categories = [], [], {}
    current_section = None
    
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current_section = line[1:-1].upper()
            continue
        
        if current_section == "FAQ" and "|" in line:
            parts = [p.strip() for p in line.split("|", 1)]
            if len(parts) >= 2:
                faq_items.append({"question": parts[0], "answer": parts[1]})
        
        elif current_section == "PRODUCTS" and "|" in line:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 5:
                category = parts[0]
                product = {
                    "category": category, "name": parts[1], "price": parts[2],
                    "desc": parts[3], "stock": parts[4]
                }
                products.append(product)
                if category not in categories:
                    categories[category] = []
                categories[category].append(product)
    
    return {"products": products, "faq": faq_items, "categories": categories}

async def fetch_data(retries: int = 3) -> bool:
    """Mengambil data dari URL GitHub dengan mekanisme coba lagi (retry)."""
    global data_cache, product_categories
    for attempt in range(retries):
        try:
            loop = asyncio.get_event_loop()
            res = await loop.run_in_executor(None, lambda: requests.get(DATA_URL, timeout=10))
            
            if res.status_code == 200:
                parsed = parse_data_file(res.text)
                if parsed["products"] or parsed["faq"]:
                    data_cache["products"] = parsed["products"]
                    data_cache["faq"] = parsed["faq"]
                    product_categories = parsed["categories"]
                    logger.info(f"✅ Data diperbarui: {len(parsed['products'])} produk, {len(parsed['faq'])} FAQ")
                    return True
                else:
                    logger.warning("⚠️ Tidak ada data valid yang ditemukan.")
                    return False
            else:
                logger.warning(f"⚠️ Gagal ambil data. Status {res.status_code}")
        except Exception as e:
            log_error(f"Error fetch data (attempt {attempt + 1}/{retries}): {e}")
            logger.error(f"❌ Error fetch data (attempt {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)
    return False

async def get_latest_commit(retries: int = 3) -> Optional[str]:
    """Mendapatkan hash commit terakhir dari GitHub."""
    for attempt in range(retries):
        try:
            loop = asyncio.get_event_loop()
            res = await loop.run_in_executor(None, lambda: requests.get(COMMITS_URL, timeout=10))
            if res.status_code == 200:
                return res.json().get("sha")
            else:
                logger.warning(f"⚠️ Gagal ambil commit. Status {res.status_code}")
        except Exception as e:
            log_error(f"Error get commit (attempt {attempt + 1}/{retries}): {e}")
            logger.error(f"❌ Error get commit (attempt {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)
    return None

# === Background Task for Auto Update ===
@tasks.loop(minutes=5)
async def auto_update_data():
    """Tugas latar belakang untuk memperbarui data secara otomatis."""
    global last_commit_hash
    try:
        new_commit = await get_latest_commit()
        if new_commit and new_commit != last_commit_hash:
            logger.info("🔄 Deteksi perubahan di GitHub, memperbarui data...")
            if await fetch_data():
                last_commit_hash = new_commit
                if UPDATE_CHANNEL_ID:
                    try:
                        channel = bot.get_channel(int(UPDATE_CHANNEL_ID))
                        if channel:
                            embed = discord.Embed(
                                title="🔄 Data Diperbarui",
                                description=(
                                    f"📦 Produk: **{len(data_cache['products'])}**\n"
                                    f"❓ FAQ: **{len(data_cache['faq'])}**\n"
                                    f"🏷️ Kategori: **{len(product_categories)}**"
                                ),
                                color=discord.Color.green(),
                                timestamp=datetime.now()
                            )
                            await channel.send(embed=embed)
                    except Exception as e:
                        log_error(f"Error sending update notification: {e}")
        else:
            logger.info("⏳ Tidak ada perubahan di GitHub.")
    except Exception as e:
        log_error(f"Error in auto_update_data: {e}")
        logger.error(f"❌ Error in auto_update_data: {e}")

@auto_update_data.before_loop
async def before_auto_update():
    await bot.wait_until_ready()

# === Bot Events ===
@bot.event
async def on_ready():
    """Event handler saat bot siap dan online."""
    global last_commit_hash
    logger.info(f"🤖 Bot aktif sebagai {bot.user}")
    logger.info(f"📊 Terhubung ke {len(bot.guilds)} server")
    
    last_commit_hash = await get_latest_commit()
    await fetch_data()
    
    if not auto_update_data.is_running():
        auto_update_data.start()
    
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="!help | Luxury VIP"))

@bot.event
async def on_command_error(ctx, error):
    """Global error handler untuk command."""
    if isinstance(error, commands.CommandNotFound):
        embed = discord.Embed(title="❌ Command Tidak Ditemukan", description="Gunakan `!help` untuk melihat daftar command.", color=discord.Color.red())
        await ctx.reply(embed=embed, delete_after=10)
    elif isinstance(error, commands.MissingRequiredArgument):
        embed = discord.Embed(title="⚠️ Argumen Kurang", description=f"Command ini memerlukan argumen: `{error.param.name}`", color=discord.Color.orange())
        await ctx.reply(embed=embed, delete_after=10)
    else:
        log_error(f"Command error: {error}")
        logger.error(f"Command error: {error}")

# === Bot Commands ===

@bot.command(name="tanya")
async def tanya(ctx, *, query: str):
    """Bertanya tentang produk menggunakan AI."""
    if not data_cache["products"]:
        await ctx.reply(embed=discord.Embed(title="⚠️ Data Belum Siap", description="Data produk sedang dimuat, coba lagi nanti.", color=discord.Color.orange()))
        return

    async with ctx.typing():
        # ... (logic for !tanya remains unchanged)
        query_lower = query.lower()
        related_products = [
            p for p in data_cache["products"]
            if any(word in p["name"].lower() for word in query_lower.split())
        ]

        if not related_products:
            try:
                prompt = f"""Kamu adalah customer service Luxury VIP yang ramah dan profesional.
Seseorang bertanya: "{query}"

Namun pertanyaan ini tidak terkait dengan produk yang tersedia di katalog kami.
Berikan respons yang sopan dan arahkan mereka untuk:
- Gunakan !faq untuk melihat pertanyaan umum
- Gunakan !stock untuk melihat produk tersedia
Jawab dalam bahasa Indonesia yang natural dan ramah."""

                response = model.generate_content(prompt)
                answer = response.text.strip()
                
            except Exception as e:
                log_error(f"Error AI general response: {e}")
                answer = (
                    "Maaf, saya tidak memiliki informasi tentang itu. "
                    "Silakan gunakan `!faq` untuk melihat pertanyaan umum atau "
                    "`!stock` untuk melihat daftar produk yang tersedia."
                )
            
            embed = discord.Embed(
                title="💬 Jawaban Luxury VIP",
                description=answer,
                color=discord.Color.orange()
            )
            embed.set_footer(text=f"Ditanyakan oleh {ctx.author.name}")
            await ctx.reply(embed=embed)
            log_interaction(ctx.author, query, answer)
            return

        context = "Berikut detail produk yang tersedia:\n\n"
        for p in related_products:
            context += f"- **{p['name']}** (Kategori: {p['category']})\n"
            context += f"  Harga: Rp {p['price']}\n"
            context += f"  {p['desc']}\n"
            context += f"  Stok: {p['stock']}\n\n"
        
        prompt = f"""Kamu adalah customer service Luxury VIP yang ramah, profesional, dan membantu.

{context}

Pertanyaan customer: "{query}"

Berikan jawaban yang:
1. Informatif dan akurat berdasarkan data produk
2. Sopan dan ramah
3. Jelas dan mudah dipahami
4. Jika ditanya harga/stok, sebutkan detailnya
5. Jika ditanya cara beli, jelaskan prosesnya dengan friendly

Jawab dalam bahasa Indonesia yang natural."""

        try:
            response = model.generate_content(prompt)
            answer = response.text.strip()
            
            embed = discord.Embed(
                title="💬 Jawaban Luxury VIP",
                description=answer,
                color=discord.Color.gold(),
                timestamp=datetime.now()
            )
            
            if len(related_products) <= 3:
                for p in related_products:
                    stock_emoji = "✅" if p['stock'].lower() not in ["habis", "0"] else "❌"
                    embed.add_field(
                        name=f"{stock_emoji} {p['name']}",
                        value=f"🏷️ {p['category']}\n💰 Rp {p['price']}\n📊 Stok: {p['stock']}",
                        inline=True
                    )
            
            embed.set_footer(text=f"Ditanyakan oleh {ctx.author.name}")
            await ctx.reply(embed=embed)
            log_interaction(ctx.author, query, answer)
            
        except Exception as e:
            log_error(f"Error AI response: {e}")
            logger.error(f"Error AI response: {e}")
            embed = discord.Embed(
                title="❌ Kesalahan Sistem",
                description="Maaf, terjadi kesalahan saat memproses pertanyaan. Silakan coba lagi.",
                color=discord.Color.red()
            )
            await ctx.reply(embed=embed)


@bot.command(name="faq")
async def faq(ctx):
    """Menampilkan daftar Pertanyaan yang Sering Diajukan (FAQ)."""
    if not data_cache["faq"]:
        await ctx.reply(embed=discord.Embed(title="⚠️ FAQ Belum Tersedia", description="Belum ada data FAQ.", color=discord.Color.orange()))
        return
    
    embed = discord.Embed(
        title="❓ FAQ - Pertanyaan yang Sering Ditanyakan",
        description="Pilih pertanyaan dari menu di bawah untuk melihat jawabannya:",
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )
    
    # Menampilkan beberapa pertanyaan sebagai contoh di embed awal
    for idx, item in enumerate(data_cache["faq"][:5], 1):
        embed.add_field(name=f"{idx}. {item['question']}", value="*Pilih dari dropdown*", inline=False)
    
    class FAQSelect(Select):
        def __init__(self):
            options = [
                discord.SelectOption(label=f"Q{idx+1}", description=item['question'][:100], emoji="❓")
                for idx, item in enumerate(data_cache["faq"][:25])
            ]
            super().__init__(placeholder="Pilih pertanyaan...", options=options)

        async def callback(self, interaction: discord.Interaction):
            selected_idx = int(self.values[0][1:]) - 1
            item = data_cache["faq"][selected_idx]
            embed_reply = discord.Embed(title=f"❓ {item['question']}", description=item['answer'], color=discord.Color.green(), timestamp=datetime.now())
            await interaction.response.send_message(embed=embed_reply, ephemeral=True)

    view = View(timeout=180)
    view.add_item(FAQSelect())
    await ctx.reply(embed=embed, view=view)


@bot.command(name="stock")
async def stock(ctx, category: Optional[str] = None, page: int = 1):
    """Melihat daftar produk dan stok berdasarkan kategori."""
    if not data_cache["products"]:
        await ctx.reply(embed=discord.Embed(title="⚠️ Data Belum Siap", description="Data produk sedang dimuat.", color=discord.Color.orange()))
        return

    # Tampilkan daftar kategori jika tidak ada argumen
    if not category:
        embed = discord.Embed(title="🏷️ Kategori Produk", description="Gunakan `!stock <kategori>` untuk melihat produk.", color=discord.Color.blue(), timestamp=datetime.now())
        for cat, prods in product_categories.items():
            available = sum(1 for p in prods if p['stock'].lower() not in ["habis", "0"])
            embed.add_field(name=cat, value=f"✅ Tersedia: {available} / {len(prods)}", inline=True)
        await ctx.reply(embed=embed)
        return

    # Cari kategori (case-insensitive)
    category_key = next((key for key in product_categories if key.lower() == category.lower()), None)

    if not category_key:
        await ctx.reply(embed=discord.Embed(title="❌ Kategori Tidak Ditemukan", description=f"Kategori '{category}' tidak ada.", color=discord.Color.red()))
        return
    
    # ... (logic for pagination and display remains unchanged)
    products = product_categories[category_key]
    items_per_page = 5
    total_pages = (len(products) + items_per_page - 1) // items_per_page
    page = max(1, min(page, total_pages))
    
    start_idx = (page - 1) * items_per_page
    end_idx = min(start_idx + items_per_page, len(products))
    
    embed = discord.Embed(
        title=f"📦 Produk: {category_key}",
        description=f"Halaman {page}/{total_pages}",
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )

    for item in products[start_idx:end_idx]:
        stock_status = item['stock']
        stock_emoji = "✅" if stock_status.lower() not in ["habis", "0"] else "❌"
            
        embed.add_field(
            name=f"{stock_emoji} {item['name']}",
            value=(
                f"💰 **Harga:** Rp {item['price']}\n"
                f"📝 **Deskripsi:** {item['desc']}\n"
                f"📊 **Stok:** {item['stock']}"
            ),
            inline=False
        )
    
    if total_pages > 1:
        nav_text = ""
        if page > 1: nav_text += f"⬅️ `!stock \"{category_key}\" {page - 1}`"
        if page < total_pages: nav_text += f" | `!stock \"{category_key}\" {page + 1}` ➡️"
        embed.set_footer(text=nav_text)
    
    await ctx.reply(embed=embed)


@bot.command(name="help")
async def help_command(ctx):
    """Menampilkan menu bantuan."""
    embed = discord.Embed(
        title="💎 Luxury VIP Assistant",
        description=(
            "Selamat datang! Saya adalah bot asisten untuk Luxury VIP.\n\n"
            "**📋 Command Tersedia:**\n"
            "• `!help` - Menampilkan menu bantuan ini\n"
            "• `!faq` - Pertanyaan yang sering ditanyakan\n"
            "• `!stock [kategori]` - Melihat produk berdasarkan kategori\n"
            "• `!tanya <pertanyaan>` - Bertanya tentang produk (AI)\n"
            "• `!ping` - Cek latensi bot\n"
            "• `!status` - Cek status sistem bot"
        ),
        color=discord.Color.gold(),
        timestamp=datetime.now()
    )
    embed.set_footer(text="Pilih menu di bawah untuk detail lebih lanjut.")
    if bot.user.avatar:
        embed.set_thumbnail(url=bot.user.avatar.url)

    # ... (HelpSelect and HelpView logic remains unchanged)
    class HelpSelect(discord.ui.Select):
        def __init__(self):
            options = [
                discord.SelectOption(
                    label="❓ FAQ", description="Cara kerja command !faq", emoji="❓"
                ),
                discord.SelectOption(
                    label="📦 Stock & Kategori", description="Cara kerja command !stock", emoji="📦"
                ),
                discord.SelectOption(
                    label="💬 Tanya Produk", description="Cara bertanya dengan AI", emoji="💬"
                ),
                discord.SelectOption(
                    label="🤖 Tentang Bot", description="Info sistem dan auto update", emoji="🤖"
                ),
            ]
            super().__init__(placeholder="Pilih kategori bantuan...", options=options)

        async def callback(self, interaction: discord.Interaction):
            pilihan = self.values[0]
            embed_dict = {
                "❓ FAQ": (
                    "**Command: `!faq`**\nMenampilkan daftar pertanyaan umum. Pilih pertanyaan dari menu dropdown untuk melihat jawabannya secara pribadi."
                ),
                "📦 Stock & Kategori": (
                    "**Command: `!stock [kategori] [halaman]`**\n- `!stock`: Menampilkan semua kategori yang tersedia.\n- `!stock <nama kategori>`: Menampilkan produk dalam kategori tersebut."
                ),
                "💬 Tanya Produk": (
                    "**Command: `!tanya <pertanyaan>`**\nGunakan bahasa natural untuk bertanya apa saja tentang produk. AI akan mencoba menjawabnya berdasarkan data yang ada.\n**Contoh:** `!tanya berapa harga VIP Gold?`"
                ),
                "🤖 Tentang Bot": (
                    f"**Luxury VIP Bot v2.1**\n- **AI:** Google Gemini 1.5 Flash\n- **Sumber Data:** GitHub\n- **Auto Update:** Setiap 5 menit\n- **Total Produk:** {len(data_cache['products'])}\n- **Total FAQ:** {len(data_cache['faq'])}"
                ),
            }
            msg = embed_dict.get(pilihan, "Pilihan tidak valid.")
            embed_reply = discord.Embed(title=f"📖 Bantuan: {pilihan}", description=msg, color=discord.Color.purple(), timestamp=datetime.now())
            await interaction.response.send_message(embed=embed_reply, ephemeral=True)

    view = View(timeout=180)
    view.add_item(HelpSelect())
    await ctx.reply(embed=embed, view=view)


@bot.command(name="ping")
async def ping(ctx):
    """Cek latensi atau 'ping' dari bot."""
    latency = round(bot.latency * 1000)
    color = discord.Color.green() if latency < 100 else (discord.Color.gold() if latency < 200 else discord.Color.red())
    embed = discord.Embed(title="🏓 Pong!", description=f"**Latency:** {latency}ms", color=color)
    await ctx.reply(embed=embed)


@bot.command(name="status")
async def status(ctx):
    """Menampilkan status operasional bot."""
    embed = discord.Embed(title="📊 Status Sistem Bot", color=discord.Color.blue(), timestamp=datetime.now())
    tersedia = sum(1 for p in data_cache['products'] if p['stock'].lower() not in ["habis", "0"])
    
    embed.add_field(name="📡 Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="🏢 Servers", value=f"{len(bot.guilds)}", inline=True)
    embed.add_field(name="🔄 Auto-Update", value="🟢 Aktif" if auto_update_data.is_running() else "🔴 Nonaktif", inline=True)
    embed.add_field(name="📦 Total Produk", value=f"{len(data_cache['products'])}", inline=True)
    embed.add_field(name="🏷️ Kategori", value=f"{len(product_categories)}", inline=True)
    embed.add_field(name="❓ FAQ", value=f"{len(data_cache['faq'])}", inline=True)
    embed.add_field(name="📊 Stok Tersedia", value=f"{tersedia}", inline=True)
    embed.add_field(name="💾 Commit Terakhir", value=f"`{last_commit_hash[:7]}`" if last_commit_hash else 'N/A', inline=True)
    
    await ctx.reply(embed=embed)

# === Main Execution ===
def main():
    """Fungsi utama untuk menjalankan bot."""
    try:
        if not all([DISCORD_TOKEN, GEMINI_API_KEY, DATA_URL, COMMITS_URL]):
            raise ValueError("Satu atau lebih environment variables (TOKEN, API_KEY, URL) tidak ditemukan.")
        logger.info("🚀 Memulai Luxury VIP Bot...")
        bot.run(DISCORD_TOKEN, log_handler=None)
    except (ValueError, Exception) as e:
        log_error(f"Fatal error: {e}")
        logger.critical(f"❌ Fatal error saat memulai bot: {e}")

if __name__ == "__main__":
    main()
