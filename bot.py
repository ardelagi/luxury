import os
import asyncio
import discord
import aiohttp
import aiofiles
import google.generativeai as genai
import logging
from discord.ext import commands, tasks
from dotenv import load_dotenv
from datetime import datetime
from discord.ui import View, Select
from typing import Optional, List, Dict, Any

# ==============================================================================
# 1. SETUP LOGGING & CONFIGURATION
# ==============================================================================
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATA_URL = os.getenv("DATA_URL")
COMMITS_URL = os.getenv("COMMITS_URL")
UPDATE_CHANNEL_ID = int(os.getenv("UPDATE_CHANNEL_ID", 0))
LOG_FILE = os.getenv("LOG_FILE", "logs.txt")
ERROR_LOG_FILE = os.getenv("ERROR_LOG_FILE", "errors.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('luxury_bot')
logging.getLogger('grpc').setLevel(logging.ERROR)
os.environ['GRPC_ENABLE_FORK_SUPPORT'] = '0'

# ==============================================================================
# 2. UTILITY FUNCTIONS (LOGGING ASYNCHRONOUS)
# ==============================================================================
async def log_interaction(user: discord.User, query: str, answer: str) -> None:
    try:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log_entry = (
            f"[{timestamp}] User: {user.name} (ID: {user.id})\n"
            f"Query: {query}\n"
            f"Answer: {answer[:300]}...\n"
            f"{'-' * 80}\n\n"
        )
        async with aiofiles.open(LOG_FILE, "a", encoding="utf-8") as f:
            await f.write(log_entry)
    except Exception as e:
        logger.error(f"Error logging interaction: {e}")

async def log_error(error_msg: str) -> None:
    try:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        async with aiofiles.open(ERROR_LOG_FILE, "a", encoding="utf-8") as f:
            await f.write(f"[{timestamp}] {error_msg}\n\n")
    except Exception as e:
        logger.error(f"Error logging error: {e}")

# ==============================================================================
# 3. BOT COG (KELAS UTAMA UNTUK SEMUA FUNGSI BOT)
# ==============================================================================
class LuxuryBotCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: aiohttp.ClientSession = aiohttp.ClientSession()
        self.data_cache: Dict[str, List[Dict[str, Any]]] = {"products": [], "faq": []}
        self.product_categories: Dict[str, List[Dict[str, Any]]] = {}
        self.last_commit_hash: Optional[str] = None
        self.model = self._initialize_gemini()
        if GEMINI_API_KEY:
            genai.configure(api_key=GEMINI_API_KEY)

    def _initialize_gemini(self):
        if not GEMINI_API_KEY:
            logger.warning("GEMINI_API_KEY tidak ditemukan. Fitur AI akan dinonaktifkan.")
            return None
        try:
            generation_config = {"temperature": 0.7, "top_p": 0.95, "top_k": 40, "max_output_tokens": 1024}
            return genai.GenerativeModel("gemini-2.5-flash", generation_config=generation_config)
        except Exception as e:
            logger.error(f"Gagal inisialisasi model Gemini: {e}")
            return None

    def cog_unload(self):
        self.auto_update_data.cancel()
        asyncio.create_task(self.session.close())

    def _parse_data_file(self, content: str) -> Dict[str, Any]:
        lines = content.strip().split("\n")
        products, faq_items, categories = [], [], {}
        current_section = None
        for i, line in enumerate(lines):
            line = line.strip()
            if not line or line.startswith("#"): continue
            if line.startswith("[") and line.endswith("]"):
                current_section = line[1:-1].upper()
                continue
            try:
                if current_section == "FAQ" and "|" in line:
                    question, answer = [p.strip() for p in line.split("|", 1)]
                    faq_items.append({"question": question, "answer": answer})
                elif current_section == "PRODUCTS" and "|" in line:
                    cat, name, price, desc, stock = [p.strip() for p in line.split("|", 4)]
                    product = {"category": cat, "name": name, "price": price, "desc": desc, "stock": stock}
                    products.append(product)
                    categories.setdefault(cat, []).append(product)
            except ValueError:
                logger.warning(f"Format data salah pada baris {i+1}: '{line}' -> dilewati.")
        return {"products": products, "faq": faq_items, "categories": categories}

    async def _fetch_url(self, url: str) -> Optional[Dict[str, Any] | str]:
        try:
            async with self.session.get(url, timeout=10) as res:
                if res.status == 200:
                    if res.headers.get('Content-Type', '').startswith('application/json'):
                        return await res.json()
                    return await res.text()
                else:
                    logger.warning(f"Gagal ambil data dari {url}. Status: {res.status}")
        except aiohttp.ClientError as e:
            logger.error(f"Error fetching URL {url}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error fetching URL {url}: {e}")
            return None

    async def fetch_data(self) -> bool:
        content = await self._fetch_url(DATA_URL)
        if isinstance(content, str):
            parsed = self._parse_data_file(content)
            if parsed["products"] or parsed["faq"]:
                self.data_cache["products"] = parsed["products"]
                self.data_cache["faq"] = parsed["faq"
                self.product_categories = parsed["categories"]
                logger.info(f"✅ Data berhasil dimuat/diperbarui: {len(parsed['products'])} produk, {len(parsed['faq'])} FAQ.")
                return True
            else:
                logger.warning("⚠️ Data berhasil diunduh, namun tidak ada produk/FAQ valid yang ditemukan setelah parsing.")
        return False

    async def get_latest_commit(self) -> Optional[str]:
        data = await self._fetch_url(COMMITS_URL)
        if isinstance(data, dict):
            return data.get("sha")
        return None

    async def _ask_gemini(self, prompt: str) -> str:
        if not self.model:
            return "Maaf, fitur AI saat ini tidak tersedia."
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, lambda: self.model.generate_content(prompt))
            return response.text.strip()
        except Exception as e:
            logger.error(f"Error saat memanggil Gemini AI: {e}")
            return "Maaf, terjadi kesalahan saat memproses pertanyaan Anda. Silakan coba lagi nanti."

    @tasks.loop(minutes=5)
    async def auto_update_data(self):
        """Tugas latar belakang untuk memperbarui data secara otomatis."""
        logger.info("⏳ Memeriksa pembaruan data dari GitHub...")
        new_commit = await self.get_latest_commit()
        force_fetch = not self.data_cache["products"] and not self.data_cache["faq"]

        if force_fetch:
            logger.info("Cache data kosong, mencoba mengambil data untuk pertama kali...")

        if force_fetch or (new_commit and new_commit != self.last_commit_hash):
            if not force_fetch:
                logger.info("🔄 Deteksi perubahan commit, memperbarui data...")

            if await self.fetch_data():
                self.last_commit_hash = new_commit
                if not force_fetch and UPDATE_CHANNEL_ID:
                    try:
                        channel = self.bot.get_channel(UPDATE_CHANNEL_ID)
                        if channel:
                            embed = discord.Embed(
                                title="🔄 Data Diperbarui",
                                description=f"📦 Produk: **{len(self.data_cache['products'])}**\n❓ FAQ: **{len(self.data_cache['faq'])}**",
                                color=discord.Color.green(),
                                timestamp=datetime.now()
                            )
                            await channel.send(embed=embed)
                    except Exception as e:
                        await log_error(f"Gagal mengirim notifikasi update: {e}")
        else:
            logger.info("👍 Data sudah versi terbaru. Tidak ada perubahan.")

    @commands.Cog.listener()
    async def on_ready(self):
        """Event handler saat bot siap dan online."""
        logger.info(f"🤖 Bot aktif sebagai {self.bot.user}")
        logger.info(f"📊 Terhubung ke {len(self.bot.guilds)} server")

        await self.bot.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching, name="!help | Memuat data..."
        ))

        await self.auto_update_data()
        if not self.auto_update_data.is_running():
            self.auto_update_data.start()

        await self.bot.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching, name="!help | Luxury VIP"
        ))

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CommandNotFound):
            return
        elif isinstance(error, commands.MissingRequiredArgument):
            embed = discord.Embed(title="⚠️ Argumen Kurang",
                                  description=f"Command ini memerlukan argumen: `{error.param.name}`",
                                  color=discord.Color.orange())
            await ctx.reply(embed=embed, delete_after=10)
        else:
            await log_error(f"Command error di server '{ctx.guild}' oleh '{ctx.author}': {error}")
            logger.error(f"Unhandled command error: {error}")
            embed = discord.Embed(title="❌ Terjadi Kesalahan",
                                  description="Sesuatu yang tidak terduga terjadi. Tim kami telah diberitahu.",
                                  color=discord.Color.red())
            await ctx.reply(embed=embed, ephemeral=True)

    @commands.command(name="tanya")
    async def tanya(self, ctx: commands.Context, *, query: str):
        if not self.data_cache["products"]:
            await ctx.reply(embed=discord.Embed(title="⚠️ Data Belum Siap", description="Data produk sedang dimuat atau gagal dimuat. Coba lagi nanti atau hubungi admin.", color=discord.Color.orange()))
            return

        async with ctx.typing():
            related_products = [p for p in self.data_cache["products"] if any(word in p["name"].lower() for word in query.lower().split())]
            if not related_products:
                prompt = f"""Kamu adalah customer service Luxury VIP yang ramah dan profesional. Seseorang bertanya: "{query}" Namun pertanyaan ini tidak terkait dengan produk yang tersedia di katalog kami. Berikan respons yang sopan dan arahkan mereka untuk menggunakan !faq atau !stock. Jawab dalam bahasa Indonesia yang natural dan ramah."""
            else:
                context = "Berikut detail produk yang relevan:\n\n"
                for p in related_products:
                    context += f"- **{p['name']}** (Kategori: {p['category']}) | Harga: Rp {p['price']} | Stok: {p['stock']}\n"
                prompt = f"""Kamu adalah customer service Luxury VIP yang ramah, profesional, dan membantu. Gunakan data berikut untuk menjawab pertanyaan:\n{context}\nPertanyaan customer: "{query}"\nBerikan jawaban yang informatif, akurat, dan sopan dalam bahasa Indonesia. Jika ditanya cara beli, jelaskan prosesnya dengan friendly."""

            answer = await self._ask_gemini(prompt)

            embed = discord.Embed(title="💬 Jawaban Luxury VIP",
                                  description=answer,
                                  color=discord.Color.gold() if related_products else discord.Color.orange(),
                                  timestamp=datetime.now())
            if related_products and len(related_products) <= 3:
                for p in related_products:
                    stock_emoji = "✅" if p['stock'].lower() not in ["habis", "0"] else "❌"
                    embed.add_field(name=f"{stock_emoji} {p['name']}",
                                    value=f"🏷️ {p['category']}\n💰 Rp {p['price']}\n📊 Stok: {p['stock']}",
                                    inline=True)
            embed.set_footer(text=f"Ditanyakan oleh {ctx.author.name}")
            await ctx.reply(embed=embed)
            await log_interaction(ctx.author, query, answer)

    @commands.command(name="faq")
    async def faq(self, ctx: commands.Context):
        if not self.data_cache["faq"]:
            await ctx.reply(embed=discord.Embed(title="⚠️ FAQ Belum Tersedia", description="Belum ada data FAQ.", color=discord.Color.orange()))
            return

        embed = discord.Embed(title="❓ FAQ - Pertanyaan yang Sering Ditanyakan",
                              description="Pilih pertanyaan dari menu di bawah untuk melihat jawabannya:",
                              color=discord.Color.blue(),
                              timestamp=datetime.now())

        class FAQSelect(Select):
            def __init__(self, faq_data: List[Dict[str, str]]):
                self.faq_data = faq_data
                options = [
                    discord.SelectOption(label=f"Q{idx + 1}: {item['question'][:80]}", value=str(idx), emoji="❓")
                    for idx, item in enumerate(faq_data[:25])
                ]
                super().__init__(placeholder="Pilih pertanyaan...", options=options)

            async def callback(self, interaction: discord.Interaction):
                item = self.faq_data[int(self.values[0])]
                embed_reply = discord.Embed(title=f"❓ {item['question']}", description=item['answer'],
                                              color=discord.Color.green(), timestamp=datetime.now())
                await interaction.response.send_message(embed=embed_reply, ephemeral=True)

        view = View(timeout=180)
        view.add_item(FAQSelect(self.data_cache["faq"))
        await ctx.reply(embed=embed, view=view)

    @commands.command(name="stock")
    async def stock(self, ctx: commands.Context, *, category: Optional[str] = None):
        if not self.data_cache["products"]:
            await ctx.reply(embed=discord.Embed(title="⚠️ Data Belum Siap", description="Data produk sedang dimuat atau gagal dimuat.", color=discord.Color.orange()))
            return

        if not category:
            embed = discord.Embed(title="🏷️ Kategori Produk",
                                  description="Gunakan `!stock <kategori>` untuk melihat produk.\nContoh: `!stock VIP Gold`",
                                  color=discord.Color.blue(),
                                  timestamp=datetime.now())
            for cat, prods in self.product_categories.items():
                available = sum(1 for p in prods if p['stock'].lower() not in ["habis", "0"])
                embed.add_field(name=cat, value=f"✅ Tersedia: {available} / {len(prods)}", inline=True)
            await ctx.reply(embed=embed)
            return

        category_key = next((key for key in self.product_categories if key.lower() == category.lower()), None)
        if not category_key:
            await ctx.reply(embed=discord.Embed(title="❌ Kategori Tidak Ditemukan", description=f"Kategori '{category}' tidak ada.", color=discord.Color.red()))
            return

        products = self.product_categories[category_key]
        embed = discord.Embed(title=f"📦 Produk: {category_key}", color=discord.Color.blue(), timestamp=datetime.now())
        for item in products[:25]:
            stock_emoji = "✅" if item['stock'].lower() not in ["habis", "0"] else "❌"
            embed.add_field(name=f"{stock_emoji} {item['name']}",
                            value=f"💰 **Harga:** Rp {item['price']}\n📊 **Stok:** {item['stock']}",
                            inline=False)
        await ctx.reply(embed=embed)

    @commands.command(name="help")
    async def help_command(self, ctx: commands.Context):
        embed = discord.Embed(title="💎 Luxury VIP Assistant",
                              description="Selamat datang! Saya adalah bot asisten untuk Luxury VIP.\n\n**📋 Command Tersedia:**\n• `!help` - Menampilkan menu bantuan ini\n• `!faq` - Pertanyaan yang sering ditanyakan\n• `!stock [kategori]` - Melihat produk berdasarkan kategori\n• `!tanya <pertanyaan>` - Bertanya tentang produk (AI)\n• `!ping` - Cek latensi bot\n• `!status` - Cek status sistem bot",
                              color=discord.Color.gold(),
                              timestamp=datetime.now())
        embed.set_footer(text="Pilih menu di bawah untuk detail lebih lanjut.")
        if self.bot.user.avatar:
            embed.set_thumbnail(url=self.bot.user.avatar.url)

        class HelpSelect(Select):
            def __init__(self, cog_instance):
                self.cog = cog_instance
                options = [
                    discord.SelectOption(label="❓ FAQ", description="Cara kerja command !faq", emoji="❓"),
                    discord.SelectOption(label="📦 Stock", description="Cara kerja command !stock", emoji="📦"),
                    discord.SelectOption(label="💬 Tanya", description="Cara bertanya dengan AI", emoji="💬"),
                    discord.SelectOption(label="🤖 Tentang Bot", description="Info sistem dan auto update", emoji="🤖")
                ]
                super().__init__(placeholder="Pilih kategori bantuan...", options=options)

            async def callback(self, interaction: discord.Interaction):
                pilihan = self.values[0]
                embed_info = {
                    "❓ FAQ": "**Command: `!faq`**\nMenampilkan daftar pertanyaan umum. Pilih dari menu dropdown untuk melihat jawaban.",
                    "📦 Stock": "**Command: `!stock [kategori]`**\n- `!stock`: Menampilkan semua kategori.\n- `!stock <nama kategori>`: Menampilkan produk dalam kategori tersebut.",
                    "💬 Tanya": "**Command: `!tanya <pertanyaan>`**\nGunakan bahasa natural untuk bertanya. AI akan menjawab berdasarkan data produk.\n**Contoh:** `!tanya berapa harga VIP Gold?`",
                    "🤖 Tentang Bot": (
                        f"**Luxury VIP Bot v3.1**\n- **AI:** Google Gemini 1.5 Flash\n- **Sumber Data:** GitHub\n- **Auto Update:** Setiap 5 menit\n- **Total Produk:** {len(self.cog.data_cache['products'])}\n- **Total FAQ:** {len(self.cog.data_cache['faq'])}"
                    ),
                }
                msg = embed_info.get(pilihan)
                embed_reply = discord.Embed(title=f"📖 Bantuan: {pilihan}", description=msg, color=discord.Color.purple())
                await interaction.response.send_message(embed=embed_reply, ephemeral=True)

        view = View(timeout=180)
        view.add_item(HelpSelect(self))
        await ctx.reply(embed=embed, view=view)

    @commands.command(name="ping")
    async def ping(self, ctx: commands.Context):
        latency = round(self.bot.latency * 1000)
        color = discord.Color.green() if latency < 150 else (
            discord.Color.gold() if latency < 250 else discord.Color.red())
        embed = discord.Embed(title="🏓 Pong!", description=f"**Latency:** {latency}ms", color=color)
        await ctx.reply(embed=embed)

    @commands.command(name="status")
    async def status(self, ctx: commands.Context):
        embed = discord.Embed(title="📊 Status Sistem Bot", color=discord.Color.blue(), timestamp=datetime.now())
        tersedia = sum(1 for p in self.data_cache['products'] if p['stock'].lower() not in ["habis", "0"])
        embed.add_field(name="📡 Latency", value=f"{round(self.bot.latency * 1000)}ms", inline=True)
        embed.add_field(name="🏢 Servers", value=f"{len(self.bot.guilds)}", inline=True)
        embed.add_field(name="🔄 Auto-Update", value="🟢 Aktif" if self.auto_update_data.is_running() else "🔴 Nonaktif",
                        inline=True)
        embed.add_field(name="📦 Total Produk", value=f"{len(self.data_cache['products'])}", inline=True)
        embed.add_field(name="🏷️ Kategori", value=f"{len(self.product_categories)}", inline=True)
        embed.add_field(name="❓ FAQ", value=f"{len(self.data_cache['faq'])}", inline=True)
        embed.add_field(name="✅ Stok Tersedia", value=f"{tersedia}", inline=True)
        embed.add_field(name="💾 Commit",
                        value=f"`{self.last_commit_hash[:7]}`" if self.last_commit_hash else 'N/A', inline=True)
        await ctx.reply(embed=embed)

# ==============================================================================
# 4. MAIN EXECUTION
# ==============================================================================
async def main():
    if not all([DISCORD_TOKEN, GEMINI_API_KEY, DATA_URL, COMMITS_URL]):
        logger.critical("❌ Satu atau lebih environment variables (TOKEN, API_KEY, URL) tidak ditemukan.")
        return

    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

    @bot.event
    async def on_ready():
        logger.info(f"Framework bot siap, memuat Cog...")
        try:
            await bot.add_cog(LuxuryBotCog(bot))
        except Exception as e:
            logger.error(f"Gagal memuat LuxuryBotCog: {e}")

    try:
        logger.info("🚀 Memulai Luxury VIP Bot...")
        await bot.start(DISCORD_TOKEN)
    except discord.LoginFailure:
        logger.critical("❌ Gagal login. Periksa kembali DISCORD_TOKEN.")
    except Exception as e:
        await log_error(f"Fatal error saat menjalankan bot: {e}")
        logger.critical(f"❌ Fatal error: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Bot dimatikan secara manual.")