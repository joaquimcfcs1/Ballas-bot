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
APPROVAL_CHANNEL_ID = int(os.getenv("APPROVAL_CHANNEL_ID", "0"))
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "0"))
PANEL_CHANNEL_ID = int(os.getenv("PANEL_CHANNEL_ID", "0"))

# opcional: categoria para criar canais temporários
TEMP_FARM_CATEGORY_ID = int(os.getenv("TEMP_FARM_CATEGORY_ID", "0"))

DB_PATH = "farmbot.sqlite3"


# ===================== DB =====================
def init_db():
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS temp_channels (
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
            temp_channel_id INTEGER NOT NULL,
            item TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            image_url TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)
        con.commit()


def db_get_temp_channel(guild_id: int, user_id: int) -> Optional[int]:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("SELECT channel_id FROM temp_channels WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        row = cur.fetchone()
        return int(row[0]) if row else None


def db_set_temp_channel(guild_id: int, user_id: int, channel_id: int):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("INSERT OR REPLACE INTO temp_channels (guild_id, user_id, channel_id) VALUES (?, ?, ?)",
                    (guild_id, user_id, channel_id))
        con.commit()


def db_clear_temp_channel(guild_id: int, user_id: int):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("DELETE FROM temp_channels WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        con.commit()


def db_create_submission(guild_id: int, user_id: int, temp_channel_id: int, item: str, quantity: int, image_url: str) -> int:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("""
            INSERT INTO submissions (guild_id, user_id, temp_channel_id, item, quantity, image_url)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (guild_id, user_id, temp_channel_id, item, quantity, image_url))
        con.commit()
        return cur.lastrowid


def db_get_submission(submission_id: int):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("""
            SELECT id, guild_id, user_id, temp_channel_id, item, quantity, image_url, status
            FROM submissions
            WHERE id=?
        """, (submission_id,))
        return cur.fetchone()


def db_update_status(submission_id: int, status: str):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("UPDATE submissions SET status=? WHERE id=?", (status, submission_id))
        con.commit()


# ===================== Helpers =====================
def safe_slug(name: str) -> str:
    base = name.lower()
    base = re.sub(r"[^a-z0-9\- ]", "", base).strip().replace(" ", "-")
    base = re.sub(r"-{2,}", "-", base) or "membro"
    return base[:40]


def make_overwrites(guild: discord.Guild, member: discord.Member) -> dict:
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


# ===================== Bot =====================
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True  # recomendado (e ative no Developer Portal também)

bot = commands.Bot(command_prefix="!", intents=intents)

# user_id -> (item, qty, temp_channel_id)
bot.pending_image: Dict[int, Tuple[str, int, int]] = {}


# ===================== UI =====================
class FarmModal(discord.ui.Modal, title="📤 Enviar Farm"):
    item = discord.ui.TextInput(label="Item", placeholder="Ex: Pólvora", max_length=80)
    quantity = discord.ui.TextInput(label="Quantidade", placeholder="Ex: 1000", max_length=10)

    def __init__(self, user_id: int, temp_channel_id: int):
        super().__init__()
        self.user_id = user_id
        self.temp_channel_id = temp_channel_id

    async def on_submit(self, interaction: discord.Interaction):
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

        bot.pending_image[self.user_id] = (item, qty, self.temp_channel_id)

        await interaction.response.send_message(
            "✅ Formulário recebido! Agora **envie a FOTO** do farm aqui neste canal (até **2 minutos**).",
            ephemeral=True
        )

        async def expire():
            await asyncio.sleep(120)
            cur = bot.pending_image.get(self.user_id)
            if cur and cur[2] == self.temp_channel_id:
                bot.pending_image.pop(self.user_id, None)
                ch = bot.get_channel(self.temp_channel_id)
                if isinstance(ch, discord.TextChannel):
                    await ch.send(f"<@{self.user_id}> ⏳ Tempo esgotado. Clique em **📤 Enviar farm** novamente.")

        asyncio.create_task(expire())


class TempChannelPanel(discord.ui.View):
    # painel dentro do canal privado temporário
    def __init__(self, owner_id: int, temp_channel_id: int):
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.temp_channel_id = temp_channel_id

    @discord.ui.button(label="Enviar farm", style=discord.ButtonStyle.primary, emoji="📤", custom_id="temp_send_farm_btn")
    async def send_farm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Esse canal é privado do dono.", ephemeral=True)
            return
        await interaction.response.send_modal(FarmModal(self.owner_id, self.temp_channel_id))


class PublicPanel(discord.ui.View):
    # painel do canal público: cria canal privado temporário
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Criar canal para enviar farm", style=discord.ButtonStyle.success, emoji="✅", custom_id="public_create_temp_btn")
    async def create_temp(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use isso dentro do servidor.", ephemeral=True)
            return
        if interaction.guild.id != GUILD_ID:
            await interaction.response.send_message("Servidor não configurado.", ephemeral=True)
            return

        guild = interaction.guild
        member = interaction.user

        # já existe canal temp?
        existing = db_get_temp_channel(guild.id, member.id)
        if existing:
            ch = guild.get_channel(existing)
            if isinstance(ch, discord.TextChannel):
                await interaction.response.send_message(f"Você já tem um canal aberto: {ch.mention}", ephemeral=True)
                return
            else:
                db_clear_temp_channel(guild.id, member.id)

        category = guild.get_channel(TEMP_FARM_CATEGORY_ID) if TEMP_FARM_CATEGORY_ID else None
        overwrites = make_overwrites(guild, member)
        channel_name = f"farm-{safe_slug(member.display_name)}"

        ch = await guild.create_text_channel(
            name=channel_name,
            category=category if isinstance(category, discord.CategoryChannel) else None,
            overwrites=overwrites,
            reason="Canal temporário de farm",
        )

        db_set_temp_channel(guild.id, member.id, ch.id)

        embed = discord.Embed(
            title="📦 Canal de Farm (Temporário)",
            description="Clique no botão abaixo para abrir o formulário.\nDepois, envie a foto aqui.\n\n✅ Quando for aprovado/rejeitado, **este canal será apagado**."
        )
        await ch.send(member.mention, embed=embed, view=TempChannelPanel(owner_id=member.id, temp_channel_id=ch.id))
        await interaction.response.send_message(f"Canal criado: {ch.mention}", ephemeral=True)


class ApprovalView(discord.ui.View):
    def __init__(self, submission_id: int):
        super().__init__(timeout=None)
        self.submission_id = submission_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        staff_role = interaction.guild.get_role(STAFF_ROLE_ID)
        if staff_role and staff_role in getattr(interaction.user, "roles", []):
            return True
        await interaction.response.send_message("Você não tem permissão para aprovar/rejeitar.", ephemeral=True)
        return False

    async def _finalize(self, interaction: discord.Interaction, status: str):
        await interaction.response.defer(ephemeral=True)

        row = db_get_submission(self.submission_id)
        if not row:
            await interaction.followup.send("Solicitação não encontrada.", ephemeral=True)
            return

        _id, guild_id, user_id, temp_channel_id, item, qty, image_url, cur_status = row
        if cur_status != "PENDING":
            await interaction.followup.send(f"Já está **{cur_status}**.", ephemeral=True)
            return

        db_update_status(self.submission_id, status)

        # DM
        user = await bot.fetch_user(user_id)
        if status == "APPROVED":
            dm = discord.Embed(title="✅ Farm Pago!", description="Seu farm foi **aprovado** e pago!")
        else:
            dm = discord.Embed(title="❌ Farm Rejeitado", description="Seu farm foi **rejeitado**. Verifique e envie novamente.")
        dm.add_field(name="Item", value=item, inline=True)
        dm.add_field(name="Quantidade", value=str(qty), inline=True)
        dm.set_thumbnail(url=image_url)
        try:
            await user.send(embed=dm)
        except discord.Forbidden:
            pass

        # apagar registro no canal de aprovação
        try:
            await interaction.message.delete()
        except discord.Forbidden:
            await interaction.message.edit(view=None)

        # apagar canal temporário
        guild = interaction.guild
        temp_ch = guild.get_channel(temp_channel_id)
        if isinstance(temp_ch, discord.TextChannel):
            try:
                await temp_ch.delete(reason=f"Farm {status} - canal temporário")
            except discord.Forbidden:
                pass

        db_clear_temp_channel(guild_id, user_id)
        await interaction.followup.send("✅ Processado e canal apagado.", ephemeral=True)

    @discord.ui.button(label="Aprovar", style=discord.ButtonStyle.success, emoji="✅", custom_id="approve_btn")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finalize(interaction, "APPROVED")

    @discord.ui.button(label="Rejeitar", style=discord.ButtonStyle.danger, emoji="❌", custom_id="reject_btn")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finalize(interaction, "REJECTED")


# ===================== Events =====================
@bot.event
async def on_ready():
    print(f"✅ Logado como {bot.user} (ID: {bot.user.id})")

    # Views persistentes (botões não quebram após restart)
    bot.add_view(PublicPanel())

    # Observação: ApprovalView e TempChannelPanel são geradas por mensagem com custom_id,
    # mas a view em si precisa existir no runtime quando o botão for clicado.
    # Como elas são criadas no envio, funciona bem.

    # Opcional: se quiser garantir painel no canal público
    guild = bot.get_guild(GUILD_ID)
    if guild and PANEL_CHANNEL_ID:
        ch = guild.get_channel(PANEL_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel):
            # manda painel se não existir nenhum recente (simples)
            try:
                embed = discord.Embed(
                    title="📤 Envio de Farm",
                    description="Clique no botão abaixo para criar seu canal privado temporário e enviar o farm."
                )
                await ch.send(embed=embed, view=PublicPanel())
            except discord.Forbidden:
                pass


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    if message.guild.id != GUILD_ID:
        return

    pending = bot.pending_image.get(message.author.id)
    if not pending:
        return

    item, qty, temp_channel_id = pending
    if message.channel.id != temp_channel_id:
        return

    if not message.attachments:
        return

    att = message.attachments[0]
    # aceita qualquer anexo, mas recomenda imagem
    if att.content_type and not att.content_type.startswith("image/"):
        return

    bot.pending_image.pop(message.author.id, None)

    submission_id = db_create_submission(
        guild_id=message.guild.id,
        user_id=message.author.id,
        temp_channel_id=temp_channel_id,
        item=item,
        quantity=qty,
        image_url=att.url
    )

    approval_channel = message.guild.get_channel(APPROVAL_CHANNEL_ID)
    if not isinstance(approval_channel, discord.TextChannel):
        await message.channel.send("⚠️ Canal de aprovação não configurado corretamente.")
        return

    embed = discord.Embed(title="🧾 Solicitação de Farm", description=f"ID: **{submission_id}**")
    embed.add_field(name="Membro", value=message.author.mention, inline=True)
    embed.add_field(name="Item", value=item, inline=True)
    embed.add_field(name="Quantidade", value=str(qty), inline=True)
    embed.set_image(url=att.url)

    await approval_channel.send(embed=embed, view=ApprovalView(submission_id))
    await message.channel.send("✅ Enviado para aprovação.")

    await bot.process_commands(message)


# ===================== Start =====================
if __name__ == "__main__":
    init_db()
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN não encontrado nas variáveis do Railway.")
    bot.run(TOKEN)
