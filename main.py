import os
import re
import sqlite3
import asyncio
from typing import Optional, Dict, Tuple

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

# ================= DATABASE =================

def init_db():
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS member_channels (
            guild_id INTEGER,
            user_id INTEGER,
            channel_id INTEGER,
            PRIMARY KEY (guild_id, user_id)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            user_id INTEGER,
            farm_channel_id INTEGER,
            item TEXT,
            quantity INTEGER,
            image_url TEXT,
            status TEXT DEFAULT 'PENDING'
        )
        """)

        con.commit()

# ================= HELPERS =================

def normalize_name(name: str):
    name = name.lower()
    name = re.sub(r"[^a-z0-9 ]", "", name)
    name = name.replace(" ", "-")
    return f"farm-{name}"

# ================= BOT =================

intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)

bot.pending_image: Dict[int, Tuple[str, int, int]] = {}

# ================= VIEWS =================

class FarmModal(discord.ui.Modal, title="Enviar Farm"):

    item = discord.ui.TextInput(label="Item")
    quantity = discord.ui.TextInput(label="Quantidade")

    def __init__(self, user_id, channel_id):
        super().__init__()
        self.user_id = user_id
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            qty = int(self.quantity.value)
        except:
            await interaction.response.send_message("Quantidade inválida.", ephemeral=True)
            return

        bot.pending_image[self.user_id] = (self.item.value, qty, self.channel_id)

        await interaction.response.send_message(
            "Agora envie a FOTO do farm aqui no canal.",
            ephemeral=True
        )

class FarmPanel(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Enviar farm", style=discord.ButtonStyle.primary)
    async def enviar(self, interaction: discord.Interaction, button: discord.ui.Button):

        await interaction.response.send_modal(
            FarmModal(interaction.user.id, interaction.channel.id)
        )

class ApprovalView(discord.ui.View):

    def __init__(self, submission_id):
        super().__init__(timeout=None)
        self.submission_id = submission_id

    async def interaction_check(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(STAFF_ROLE_ID)
        if role in interaction.user.roles:
            return True

        await interaction.response.send_message("Sem permissão.", ephemeral=True)
        return False

    @discord.ui.button(label="Aprovar", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):

        await interaction.response.defer(ephemeral=True)

        with sqlite3.connect(DB_PATH) as con:
            cur = con.cursor()
            cur.execute("SELECT user_id,item,quantity,image_url,status FROM submissions WHERE id=?",
                        (self.submission_id,))
            row = cur.fetchone()

        if not row or row[4] != "PENDING":
            await interaction.followup.send("Já processado.", ephemeral=True)
            return

        user_id, item, qty, image_url, status = row

        with sqlite3.connect(DB_PATH) as con:
            con.execute("UPDATE submissions SET status='APPROVED' WHERE id=?",
                        (self.submission_id,))
            con.commit()

        user = await bot.fetch_user(user_id)

        embed = discord.Embed(title="Farm Pago!")
        embed.add_field(name="Item", value=item)
        embed.add_field(name="Quantidade", value=str(qty))
        embed.set_thumbnail(url=image_url)

        try:
            await user.send(embed=embed)
        except:
            pass

        try:
            await interaction.message.delete()
        except:
            await interaction.message.edit(view=None)

        await interaction.followup.send("Aprovado.", ephemeral=True)

    @discord.ui.button(label="Rejeitar", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):

        await interaction.response.defer(ephemeral=True)

        with sqlite3.connect(DB_PATH) as con:
            cur = con.cursor()
            cur.execute("SELECT user_id,item,quantity,status FROM submissions WHERE id=?",
                        (self.submission_id,))
            row = cur.fetchone()

        if not row or row[3] != "PENDING":
            await interaction.followup.send("Já processado.", ephemeral=True)
            return

        user_id, item, qty, status = row

        with sqlite3.connect(DB_PATH) as con:
            con.execute("UPDATE submissions SET status='REJECTED' WHERE id=?",
                        (self.submission_id,))
            con.commit()

        user = await bot.fetch_user(user_id)

        embed = discord.Embed(title="Farm Rejeitado")
        embed.add_field(name="Item", value=item)
        embed.add_field(name="Quantidade", value=str(qty))

        try:
            await user.send(embed=embed)
        except:
            pass

        try:
            await interaction.message.delete()
        except:
            await interaction.message.edit(view=None)

        await interaction.followup.send("Rejeitado.", ephemeral=True)

# ================= EVENTS =================

@bot.event
async def on_ready():
    print(f"Bot online: {bot.user}")

    # REGISTRA VIEWS PERSISTENTES
    bot.add_view(FarmPanel())
    bot.add_view(ApprovalView(0))

@bot.event
async def on_member_join(member: discord.Member):

    if member.guild.id != GUILD_ID:
        return

    category = member.guild.get_channel(FARM_CATEGORY_ID)
    if not category:
        return

    channel = await member.guild.create_text_channel(
        normalize_name(member.display_name),
        category=category
    )

    await channel.set_permissions(member, view_channel=True, send_messages=True)
    await channel.set_permissions(member.guild.default_role, view_channel=False)

    embed = discord.Embed(
        title="Seu canal foi criado!",
        description="Clique no botão abaixo para enviar um farm."
    )

    await channel.send(member.mention, embed=embed, view=FarmPanel())

@bot.event
async def on_message(message: discord.Message):

    if message.author.bot:
        return

    if message.author.id not in bot.pending_image:
        return

    if not message.attachments:
        return

    item, qty, channel_id = bot.pending_image.pop(message.author.id)

    if message.channel.id != channel_id:
        return

    image = message.attachments[0].url

    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("""
        INSERT INTO submissions (guild_id,user_id,farm_channel_id,item,quantity,image_url)
        VALUES (?,?,?,?,?,?)
        """, (message.guild.id, message.author.id, channel_id, item, qty, image))
        con.commit()
        submission_id = cur.lastrowid

    approval_channel = message.guild.get_channel(APPROVAL_CHANNEL_ID)

    embed = discord.Embed(title="Nova solicitação")
    embed.add_field(name="Membro", value=message.author.mention)
    embed.add_field(name="Item", value=item)
    embed.add_field(name="Quantidade", value=str(qty))
    embed.set_image(url=image)

    await approval_channel.send(embed=embed, view=ApprovalView(submission_id))

    await message.channel.send("Enviado para aprovação.")

# ================= START =================

if __name__ == "__main__":
    init_db()

    if not TOKEN:
        raise Exception("DISCORD_TOKEN não configurado.")

    bot.run(TOKEN)
