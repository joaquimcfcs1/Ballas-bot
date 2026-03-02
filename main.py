import os
import re
import sqlite3
import asyncio
from typing import Optional, List, Dict, Tuple

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

FARM_CATEGORY_ID = int(os.getenv("FARM_CATEGORY_ID", "0"))
APPROVAL_CHANNEL_ID = int(os.getenv("APPROVAL_CHANNEL_ID", "0"))
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "0"))

DB_PATH = "farmbot.sqlite3"

# ====== DB ======
def init_db():
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS member_channels (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            farm_channel_id INTEGER NOT NULL,
            item TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            image_url TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)
        con.commit()

def db_get_member_channel(guild_id: int, user_id: int) -> Optional[int]:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("SELECT channel_id FROM member_channels WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id))
        row = cur.fetchone()
        return int(row[0]) if row else None

def db_set_member_channel(guild_id: int, user_id: int, channel_id: int):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("INSERT OR REPLACE INTO member_channels (guild_id, user_id, channel_id) VALUES (?, ?, ?)",
                    (guild_id, user_id, channel_id))
        con.commit()

def db_create_submission(guild_id: int, user_id: int, farm_channel_id: int, item: str, quantity: int, image_url: str) -> int:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("""
            INSERT INTO submissions (guild_id, user_id, farm_channel_id, item, quantity, image_url)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (guild_id, user_id, farm_channel_id, item, quantity, image_url))
        con.commit()
        return cur.lastrowid

def db_get_submission(submission_id: int):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("SELECT id, guild_id, user_id, farm_channel_id, item, quantity, image_url, status FROM submissions WHERE id=?",
                    (submission_id,))
        return cur.fetchone()

def db_update_status(submission_id: int, status: str):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("UPDATE submissions SET status=? WHERE id=?", (status, submission_id))
        con.commit()

# ====== Helpers ======
def normalize_channel_name(display_name: str) -> str:
    base = display_name.lower()
    base = re.sub(r"[^a-z0-9\- ]", "", base).strip().replace(" ", "-")
    base = re.sub(r"-{2,}", "-", base) or "membro"
    return f"farm-{base}"[:90]

def make_private_overwrites(guild: discord.Guild, member: discord.Member) -> dict:
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
        ),
    }
    staff_role = guild.get_role(STAFF_ROLE_ID)
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_messages=True,
        )
    return overwrites

# ====== UI (Modal + Views) ======
class FarmModal(discord.ui.Modal, title="📤 Enviar Farm"):
    item = discord.ui.TextInput(label="Item", placeholder="Ex: Pólvora", max_length=80)
    quantity = discord.ui.TextInput(label="Quantidade", placeholder="Ex: 1000", max_length=10)

    def __init__(self, bot: "FarmBot", member_id: int, channel_id: int):
        super().__init__()
        self.bot = bot
        self.member_id = member_id
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction):
        # valida quantidade
        try:
            qty = int(str(self.quantity.value).strip())
            if qty <= 0:
                raise ValueError()
        except ValueError:
            await interaction.response.send_message("Quantidade inválida. Use um número inteiro > 0.", ephemeral=True)
            return

        item = str(self.item.value).strip()
        if not item:
            await interaction.response.send_message("Item inválido.", ephemeral=True)
            return

        # marca que esse membro está "aguardando imagem"
        self.bot.pending_image[self.member_id] = (item, qty, self.channel_id)

        await interaction.response.send_message(
            "✅ Formulário recebido!\nAgora **envie a FOTO** do farm aqui neste canal (até **2 minutos**).",
            ephemeral=True
        )

        # timeout automático: se não mandar imagem em 2 min, cancela
        async def expire():
            await asyncio.sleep(120)
            cur = self.bot.pending_image.get(self.member_id)
            if cur and cur[2] == self.channel_id:  # ainda pendente no mesmo canal
                self.bot.pending_image.pop(self.member_id, None)
                ch = self.bot.get_channel(self.channel_id)
                if isinstance(ch, discord.TextChannel):
                    await ch.send(f"<@{self.member_id}> ⏳ Tempo esgotado. Clique em **📤 Enviar farm** novamente.")

        asyncio.create_task(expire())

class FarmPanelView(discord.ui.View):
    def __init__(self, bot: "FarmBot", member_id: int, channel_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.member_id = member_id
        self.channel_id = channel_id

    @discord.ui.button(label="Enviar farm", style=discord.ButtonStyle.primary, emoji="📤")
    async def send_farm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.member_id:
            await interaction.response.send_message("Esse painel é do dono do canal.", ephemeral=True)
            return
        await interaction.response.send_modal(FarmModal(self.bot, self.member_id, self.channel_id))

class ApprovalView(discord.ui.View):
    def __init__(self, bot: "FarmBot", submission_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.submission_id = submission_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        staff_role = interaction.guild.get_role(STAFF_ROLE_ID)
        if staff_role and staff_role in getattr(interaction.user, "roles", []):
            return True
        await interaction.response.send_message("Você não tem permissão para aprovar.", ephemeral=True)
        return False

    @discord.ui.button(label="Aprovar", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        row = db_get_submission(self.submission_id)
        if not row:
            await interaction.response.send_message("Solicitação não encontrada.", ephemeral=True)
            return
        _id, guild_id, user_id, farm_channel_id, item, qty, image_url, status = row
        if status != "PENDING":
            await interaction.response.send_message(f"Já está **{status}**.", ephemeral=True)
            return

        db_update_status(self.submission_id, "APPROVED")

        # DM estilo "Farm Pago" (sem registro)
        user = await self.bot.fetch_user(user_id)
        dm = discord.Embed(title="✅ Farm Pago!", description="Olá, seu farm foi **pago!**")
        dm.add_field(name="Item", value=item, inline=True)
        dm.add_field(name="Quantidade", value=str(qty), inline=True)
        dm.set_thumbnail(url=image_url)
        try:
            await user.send(embed=dm)
        except discord.Forbidden:
            pass

        e = interaction.message.embeds[0]
        e.title = "✅ Farm APROVADO"
        e.color = discord.Color.green()
        await interaction.response.edit_message(embed=e, view=None)

    @discord.ui.button(label="Rejeitar", style=discord.ButtonStyle.danger, emoji="❌")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        row = db_get_submission(self.submission_id)
        if not row:
            await interaction.response.send_message("Solicitação não encontrada.", ephemeral=True)
            return
        _id, guild_id, user_id, farm_channel_id, item, qty, image_url, status = row
        if status != "PENDING":
            await interaction.response.send_message(f"Já está **{status}**.", ephemeral=True)
            return

        db_update_status(self.submission_id, "REJECTED")

        user = await self.bot.fetch_user(user_id)
        dm = discord.Embed(title="❌ Farm Rejeitado", description="Seu envio foi rejeitado. Confira a foto/quantidade e envie novamente.")
        dm.add_field(name="Item", value=item, inline=True)
        dm.add_field(name="Quantidade", value=str(qty), inline=True)
        try:
            await user.send(embed=dm)
        except discord.Forbidden:
            pass

        e = interaction.message.embeds[0]
        e.title = "❌ Farm REJEITADO"
        e.color = discord.Color.red()
        await interaction.response.edit_message(embed=e, view=None)

# ====== Bot ======
class FarmBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.messages = True
        intents.message_content = False  # não precisa ler conteúdo, só anexos
        super().__init__(command_prefix="!", intents=intents)

        # member_id -> (item, qty, channel_id)
        self.pending_image: Dict[int, Tuple[str, int, int]] = {}

    async def setup_hook(self):
        # nada de slash commands; só UI/botões
        pass

bot = FarmBot()

@bot.event
async def on_ready():
    print(f"✅ Logado como {bot.user} (ID: {bot.user.id})")

async def ensure_member_channel(guild: discord.Guild, member: discord.Member) -> Optional[discord.TextChannel]:
    existing_id = db_get_member_channel(guild.id, member.id)
    if existing_id:
        ch = guild.get_channel(existing_id)
        if isinstance(ch, discord.TextChannel):
            return ch

    category = guild.get_channel(FARM_CATEGORY_ID)
    if not isinstance(category, discord.CategoryChannel):
        return None

    overwrites = make_private_overwrites(guild, member)
    ch = await guild.create_text_channel(
        name=normalize_channel_name(member.display_name),
        category=category,
        overwrites=overwrites,
        reason="Canal privado de farm (auto)",
    )
    db_set_member_channel(guild.id, member.id, ch.id)
    return ch

async def send_panel(channel: discord.TextChannel, member: discord.Member):
    embed = discord.Embed(
        title="✅ Seu canal de farm foi criado!",
        description=(
            "Clique no botão abaixo para **enviar um farm**.\n"
            "Você vai preencher **Item** e **Quantidade** e depois mandar a **foto**."
        )
    )
    view = FarmPanelView(bot, member_id=member.id, channel_id=channel.id)
    await channel.send(content=f"<@{member.id}>", embed=embed, view=view)

@bot.event
async def on_member_join(member: discord.Member):
    if member.guild.id != GUILD_ID:
        return
    ch = await ensure_member_channel(member.guild, member)
    if ch:
        await send_panel(ch, member)

@bot.event
async def on_message(message: discord.Message):
    # ignora bots
    if message.author.bot:
        return
    if not message.guild:
        return
    if message.guild.id != GUILD_ID:
        return

    pending = bot.pending_image.get(message.author.id)
    if not pending:
        return

    item, qty, channel_id = pending
    if message.channel.id != channel_id:
        return

    # precisa ter anexo (imagem)
    if not message.attachments:
        return

    att = message.attachments[0]
    if not (att.content_type or "").startswith("image/"):
        return

    # remove pendência
    bot.pending_image.pop(message.author.id, None)

    # cria submissão e manda para aprovação
    submission_id = db_create_submission(
        guild_id=message.guild.id,
        user_id=message.author.id,
        farm_channel_id=message.channel.id,
        item=item,
        quantity=qty,
        image_url=att.url
    )

    approval_channel = message.guild.get_channel(APPROVAL_CHANNEL_ID)
    if not isinstance(approval_channel, discord.TextChannel):
        await message.channel.send("⚠️ Canal de aprovação não configurado.")
        return

    embed = discord.Embed(title="🧾 Solicitação de Farm", description=f"ID: **{submission_id}**")
    embed.add_field(name="Membro", value=message.author.mention, inline=True)
    embed.add_field(name="Item", value=item, inline=True)
    embed.add_field(name="Quantidade", value=str(qty), inline=True)
    embed.set_image(url=att.url)

    await approval_channel.send(embed=embed, view=ApprovalView(bot, submission_id))
    await message.channel.send("✅ Foto recebida! Enviado para aprovação.")

# ====== Start ======
if __name__ == "__main__":
    init_db()
    bot.run(TOKEN)
