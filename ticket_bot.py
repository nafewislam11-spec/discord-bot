"""
Kinetic Ticket Bot
------------------
A Ticket-Tool style bot that works in any server it is added to. Everything is
done from chat with one command:

  /setup
    -> first time:  "Welcome" screen with a [Create A Panel] button
    -> afterwards:  "Panel Editor & Manager" — pick a panel from a dropdown,
                    or create a new one
    -> panel editor:
         dropdowns : panel channel, ticket category, staff roles
         buttons   : Edit Panel (title/description/button/emoji/color)
                     Ticket Settings (panel name, welcome msg, channel name)
                     Back, Save, Send Panel, Delete

When a member clicks the panel button, a private channel (e.g. ticket-0001) is
created in the chosen category. Only that member, the chosen roles and the bot
can see it.

Setup:
    venv/bin/pip install -U discord.py certifi
    export DISCORD_BOT_TOKEN=your_token      (Windows: set DISCORD_BOT_TOKEN=your_token)
    venv/bin/python ticket_bot.py

Needs no privileged intents. Invite the bot with scopes: bot + applications.commands
and permissions: Manage Channels, Manage Roles, Send Messages, Embed Links
(or simply Administrator).
"""
import asyncio
import json
import os
import re
import secrets
from pathlib import Path

# macOS Python often has no root certificates -> CERTIFICATE_VERIFY_FAILED.
# Point SSL at certifi's bundle before discord/aiohttp is imported.
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except ImportError:
    pass

import discord
from discord import app_commands
from discord.ext import commands

def get_token() -> str:
    for k in ("DISCORD_BOT_TOKEN", "BOT_TOKEN", "TOKEN"):
        v = os.environ.get(k)
        if v and v.strip():
            return v.strip()
    env_file = Path(__file__).with_name(".env")
    if env_file.exists():
        for line in env_file.read_text("utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, val = line.split("=", 1)
            if k.strip() in ("DISCORD_BOT_TOKEN", "BOT_TOKEN", "TOKEN"):
                return val.strip().strip("'\"")
    tok_file = Path(__file__).with_name("token.txt")
    if tok_file.exists():
        val = tok_file.read_text("utf-8").strip()
        if val:
            return val
    import sys
    if sys.stdin and sys.stdin.isatty():
        return input("Bot token: ").strip()
    raise RuntimeError("DISCORD_BOT_TOKEN not set! Set DISCORD_BOT_TOKEN env variable or create token.txt.")

TOKEN = get_token()
DATA_FILE = Path(__file__).with_name("panels.json")

TOPIC_RE = re.compile(r"^ticket\|([0-9a-f]+)\|(\d+)$")
CUSTOM_EMOJI_RE = re.compile(r"^<a?:\w+:\d+>$")
HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")

# editable settings of a panel (everything the editor can change)
DEFAULTS = {
    "name": "Support",
    "channel_id": None,
    "category_id": None,
    "role_ids": [],
    "title": "🎫 Support Tickets",
    "description": "Click the button below to open a ticket.\nOnly you and our team will see it.",
    "button_label": "Create Ticket",
    "button_emoji": "🎫",
    "color": "#5865F2",
    "welcome": "",
    "name_format": "ticket-{user}",
}
# bookkeeping kept next to the settings
META = {"guild_id": None, "message_id": None, "posted_channel_id": None, "counter": 0}


# ---------------------------------------------------------------- storage
class Store:
    """Tiny JSON store: {panel_id: {...settings, ...meta}}"""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data: dict[str, dict] = json.loads(path.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), "utf-8")
        tmp.replace(self.path)

    def get(self, pid: str) -> dict | None:
        p = self.data.get(pid)
        return {**DEFAULTS, **META, **p} if p else None

    def update(self, pid: str, **fields):
        self.data.setdefault(pid, {}).update(fields)
        self._save()

    def delete(self, pid: str):
        self.data.pop(pid, None)
        self._save()

    def next_number(self, pid: str) -> int:
        n = self.data[pid].get("counter", 0) + 1
        self.update(pid, counter=n)
        return n

    def for_guild(self, guild_id: int) -> list[tuple[str, dict]]:
        return [(pid, self.get(pid)) for pid, p in self.data.items()
                if p.get("guild_id") == guild_id]


store = Store(DATA_FILE)


# ---------------------------------------------------------------- helpers
def _limited(me: discord.Member, **perms) -> discord.PermissionOverwrite:
    """Only grant permissions the bot itself has, otherwise Discord returns 403
    when creating the channel."""
    mine = me.guild_permissions
    if mine.administrator:
        return discord.PermissionOverwrite(**perms)
    return discord.PermissionOverwrite(
        **{k: v for k, v in perms.items() if getattr(mine, k, False)})


def staff_perms(me: discord.Member) -> discord.PermissionOverwrite:
    return _limited(
        me, view_channel=True, send_messages=True, read_message_history=True,
        attach_files=True, embed_links=True, manage_messages=True,
    )


def member_perms(me: discord.Member) -> discord.PermissionOverwrite:
    return _limited(
        me, view_channel=True, send_messages=True, read_message_history=True,
        attach_files=True, embed_links=True,
    )


def bot_perms(me: discord.Member) -> discord.PermissionOverwrite:
    return _limited(
        me, view_channel=True, send_messages=True, read_message_history=True,
        embed_links=True, manage_channels=True,
    )


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "", text.lower().replace(" ", "-"))


def channel_name(fmt: str, user: discord.abc.User, number: int) -> str:
    fmt_str = fmt or DEFAULTS["name_format"]
    display = getattr(user, "display_name", user.name)
    raw = (
        fmt_str.replace("{number}", f"{number:04d}")
        .replace("{user}", user.name)
        .replace("{username}", user.name)
        .replace("{display_name}", display)
        .replace("{id}", str(user.id))
    )
    return (slug(raw) or f"ticket-{slug(user.name) or str(user.id)}")[:90]


def color_of(panel: dict) -> discord.Color:
    m = HEX_RE.match(panel.get("color") or "")
    return discord.Color(int(m.group(1), 16)) if m else discord.Color.blurple()


def valid_emoji(text: str) -> bool:
    if not text:
        return True
    if CUSTOM_EMOJI_RE.match(text):
        return True
    # unicode emoji: short and no plain ascii letters/digits
    return len(text) <= 10 and all(ord(c) > 127 for c in text)


def panel_embed(panel: dict) -> discord.Embed:
    return discord.Embed(title=panel["title"], description=panel["description"],
                         color=color_of(panel))


def panel_view(pid: str, panel: dict) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(OpenTicketButton(pid, panel))
    return view


def dashboard_color() -> discord.Color:
    return discord.Color.from_str("#2B2D31")


# ---------------------------------------------------------------- ticket channel
class CloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger,
                       emoji="🔒", custom_id="tk:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
        except (discord.NotFound, discord.HTTPException):
            return
        channel = interaction.channel
        m = TOPIC_RE.match(getattr(channel, "topic", None) or "")
        if not m:
            return await interaction.followup.send(
                "This is not a ticket channel.", ephemeral=True)

        panel = store.get(m.group(1)) or {}
        opener_id = int(m.group(2))
        role_ids = set(panel.get("role_ids", []))
        user = interaction.user
        allowed = (
            user.id == opener_id
            or any(r.id in role_ids for r in user.roles)
            or user.guild_permissions.manage_channels
        )
        if not allowed:
            return await interaction.followup.send(
                "Only the ticket owner or staff can close this ticket.", ephemeral=True)

        await interaction.followup.send(
            "Are you sure you want to close this ticket?", view=ConfirmCloseView(), ephemeral=True)


class ConfirmCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        try:
            await interaction.response.edit_message(content="Closing...", view=None)
        except Exception:
            pass
        await channel.send(f"🔒 Ticket closed by {interaction.user.mention}. "
                           "This channel will be deleted in 5 seconds.")
        await asyncio.sleep(5)
        try:
            await channel.delete(reason=f"Ticket closed by {interaction.user}")
        except discord.HTTPException:
            await channel.send("I couldn't delete this channel. I need **Manage Channels**.")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.edit_message(content="Cancelled.", view=None)
        except Exception:
            pass


class OpenTicketButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"tk:open:(?P<pid>[0-9a-f]+)",
):
    """Persistent panel button. Only the panel id lives in custom_id; all
    settings are read from panels.json, so edits apply instantly and survive
    restarts."""

    def __init__(self, pid: str, panel: dict):
        super().__init__(
            discord.ui.Button(
                label=panel.get("button_label") or "Create Ticket",
                emoji=panel.get("button_emoji") or None,
                style=discord.ButtonStyle.primary,
                custom_id=f"tk:open:{pid}",
            )
        )
        self.pid = pid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        pid = match["pid"]
        return cls(pid, store.get(pid) or DEFAULTS)

    async def callback(self, interaction: discord.Interaction):
        # 1. DEFER ON LINE 1 IMMEDIATELY! This ACKs Discord in 20ms and avoids timeout.
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except (discord.NotFound, discord.HTTPException):
            return

        guild = interaction.guild
        user = interaction.user
        panel = store.get(self.pid)
        if not panel or panel["guild_id"] != guild.id:
            return await interaction.followup.send(
                "This ticket panel was removed. Ask an admin to set it up again.", ephemeral=True)

        category = guild.get_channel(panel["category_id"] or 0)
        if not isinstance(category, discord.CategoryChannel):
            return await interaction.followup.send(
                "The ticket category no longer exists. Ask an admin to fix it with /setup.",
                ephemeral=True)

        # one open ticket per member per panel
        for ch in category.text_channels:
            m = TOPIC_RE.match(ch.topic or "")
            if m and m.group(1) == self.pid and int(m.group(2)) == user.id:
                return await interaction.followup.send(
                    f"You already have an open ticket: {ch.mention}", ephemeral=True)

        me = guild.me
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            me: bot_perms(me),
            user: member_perms(me),
        }
        staff_roles = [r for r in (guild.get_role(rid) for rid in panel["role_ids"]) if r]
        for role in staff_roles:
            overwrites[role] = staff_perms(me)

        number = store.next_number(self.pid)
        try:
            channel = await guild.create_text_channel(
                name=channel_name(panel["name_format"], user, number),
                category=category,
                overwrites=overwrites,
                topic=f"ticket|{self.pid}|{user.id}",
                reason=f"Ticket opened by {user}",
            )
        except discord.Forbidden:
            return await interaction.followup.send(
                "I don't have permission to create channels here. "
                "Ask an admin to give me **Manage Channels** and **Manage Roles** "
                "(or Administrator).", ephemeral=True)
        except discord.HTTPException as e:
            return await interaction.followup.send(
                f"Couldn't create the ticket: {e.text or e}", ephemeral=True)

        welcome_text = (panel.get("welcome") or "").strip()
        if welcome_text and welcome_text.lower() != "none":
            embed = discord.Embed(
                description=welcome_text.replace("{user}", user.mention),
                color=color_of(panel),
            )
            embed.set_author(name=f"{panel['name']} • Ticket #{number:04d}",
                             icon_url=user.display_avatar.url)
            ping = " ".join(r.mention for r in staff_roles)
            await channel.send(
                content=f"{user.mention} {ping}".strip(),
                embed=embed,
                view=CloseView(),
                allowed_mentions=discord.AllowedMentions(users=True, roles=True),
            )

        try:
            await interaction.followup.send(f"✅ Ticket created: {channel.mention}", ephemeral=True)
            await asyncio.sleep(3)
            await interaction.delete_original_response()
        except Exception:
            pass


# ---------------------------------------------------------------- dashboard base
class Dashboard(discord.ui.View):
    """Base for the /setup screens: only the admin who ran /setup can click."""

    def __init__(self, owner: discord.abc.User, guild: discord.Guild):
        super().__init__(timeout=900)
        self.owner = owner
        self.guild = guild

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message(
                "Only the person who ran `/setup` can use this menu.", ephemeral=True)
            return False
        return True


def manager_screen(owner, guild) -> tuple[discord.Embed, discord.ui.View]:
    panels = store.for_guild(guild.id)
    if not panels:
        embed = discord.Embed(
            title="Welcome to Ticket Tool!",
            description=(
                "It looks like this is your first time using the bot. To get started we "
                "first need to create a panel.\n\n"
                "*Q: What is a panel?*\n"
                "A: The panel is what we will use to create tickets. You can create "
                "multiple panels to handle issues of different types like `Ban Appeals`, "
                "`Contact an admin`, `General Help` and anything else you may need."
            ),
            color=dashboard_color(),
        )
    else:
        embed = discord.Embed(
            title="Panel Editor & Manager",
            description=(
                "Select a panel to get started making changes.\n\n"
                f"This server has **{len(panels)}** panel(s).\n\n"
                "*Any unsaved changes will be discarded when switching panels*"
            ),
            color=dashboard_color(),
        )
    return embed, ManagerView(owner, guild, panels)


class ManagerView(Dashboard):
    def __init__(self, owner, guild, panels):
        super().__init__(owner, guild)
        if panels:
            options = []
            for pid, p in panels[:25]:
                ch = guild.get_channel(p["posted_channel_id"] or 0)
                options.append(discord.SelectOption(
                    label=p["name"][:100], value=pid,
                    description=(f"Posted in #{ch.name}" if ch else "Not sent yet")[:100],
                    emoji=(p["button_emoji"] or None) if valid_emoji(p["button_emoji"]) else None))
            select = discord.ui.Select(placeholder="Select a panel", options=options, row=0)
            select.callback = lambda i: self.open_panel(i, select.values[0])
            self.add_item(select)

        create = discord.ui.Button(label="Create A Panel", emoji="➕",
                                   style=discord.ButtonStyle.success, row=1,
                                   disabled=len(panels) >= 25)
        create.callback = self.create_panel
        self.add_item(create)

    async def open_panel(self, interaction: discord.Interaction, pid: str):
        try:
            panel = store.get(pid)
            if not panel:
                embed, view = manager_screen(self.owner, self.guild)
                return await interaction.response.edit_message(
                    content="That panel no longer exists.", embed=embed, view=view)
            view = EditorView(self.owner, self.guild, pid, panel)
            await interaction.response.edit_message(content=None, embeds=view.embeds(), view=view)
        except (discord.NotFound, discord.HTTPException):
            pass

    async def create_panel(self, interaction: discord.Interaction):
        try:
            await interaction.response.send_modal(CreatePanelModal(self.owner, self.guild))
        except (discord.NotFound, discord.HTTPException):
            pass


class CreatePanelModal(discord.ui.Modal, title="Create A Panel"):
    name = discord.ui.TextInput(label="Panel name", placeholder="e.g. General Help, Ban Appeals",
                                max_length=80)

    def __init__(self, owner, guild):
        super().__init__()
        self.owner, self.guild = owner, guild

    async def on_submit(self, interaction: discord.Interaction):
        pid = secrets.token_hex(4)
        name = self.name.value.strip() or DEFAULTS["name"]
        store.update(pid, **{**DEFAULTS, **META, "name": name, "guild_id": self.guild.id,
                             "title": f"🎫 {name}", "channel_id": interaction.channel_id})
        view = EditorView(self.owner, self.guild, pid, store.get(pid))
        await interaction.response.edit_message(
            content=f"✅ Panel **{name}** created. Set it up below, then press **Save** "
                    "and **Send Panel**.",
            embeds=view.embeds(), view=view)


# ---------------------------------------------------------------- panel editor
class PanelTextModal(discord.ui.Modal, title="Edit Panel"):
    def __init__(self, view: "EditorView"):
        super().__init__()
        self.view = view
        d = view.d
        self.f_title = discord.ui.TextInput(label="Title", default=d["title"], max_length=256)
        self.f_desc = discord.ui.TextInput(label="Description", default=d["description"],
                                           style=discord.TextStyle.paragraph, max_length=4000)
        self.f_btn = discord.ui.TextInput(label="Button text", default=d["button_label"],
                                          max_length=80)
        self.f_emoji = discord.ui.TextInput(label="Button emoji (blank = none)", required=False,
                                            default=d["button_emoji"] or "", max_length=64)
        self.f_color = discord.ui.TextInput(label="Color (hex, e.g. #5865F2)", default=d["color"],
                                            max_length=7)
        for f in (self.f_title, self.f_desc, self.f_btn, self.f_emoji, self.f_color):
            self.add_item(f)

    async def on_submit(self, interaction: discord.Interaction):
        d = self.view.d
        errors = []
        d["title"] = self.f_title.value.strip() or DEFAULTS["title"]
        d["description"] = self.f_desc.value.strip() or DEFAULTS["description"]
        d["button_label"] = self.f_btn.value.strip() or DEFAULTS["button_label"]
        emoji = self.f_emoji.value.strip()
        if valid_emoji(emoji):
            d["button_emoji"] = emoji
        else:
            errors.append(f"`{emoji}` is not a valid emoji (kept the old one).")
        m = HEX_RE.match(self.f_color.value.strip())
        if m:
            d["color"] = f"#{m.group(1).upper()}"
        else:
            errors.append(f"`{self.f_color.value}` is not a hex color (kept the old one).")
        await self.view.refresh(interaction, note="\n".join(f"⚠️ {e}" for e in errors))


class TicketSettingsModal(discord.ui.Modal, title="Ticket Settings"):
    def __init__(self, view: "EditorView"):
        super().__init__()
        self.view = view
        d = view.d
        self.f_name = discord.ui.TextInput(label="Panel name (shown in the manager)",
                                           default=d["name"], max_length=80)
        self.f_welcome = discord.ui.TextInput(
            label="Welcome message (blank = no message)", default=d["welcome"] or "",
            style=discord.TextStyle.paragraph, max_length=2000, required=False)
        self.f_format = discord.ui.TextInput(
            label="Channel name ({user}, {number})", default=d["name_format"],
            placeholder="ticket-{user}", max_length=60)
        for f in (self.f_name, self.f_welcome, self.f_format):
            self.add_item(f)

    async def on_submit(self, interaction: discord.Interaction):
        d = self.view.d
        d["name"] = self.f_name.value.strip() or DEFAULTS["name"]
        d["welcome"] = self.f_welcome.value.strip()
        d["name_format"] = self.f_format.value.strip() or DEFAULTS["name_format"]
        await self.view.refresh(interaction)


class EditorView(Dashboard):
    """Edits a draft copy of one panel. Nothing is stored until Save / Send Panel."""

    def __init__(self, owner, guild, pid: str, panel: dict):
        super().__init__(owner, guild)
        self.pid = pid
        self.d = {k: panel[k] for k in DEFAULTS}
        self.d["role_ids"] = list(self.d["role_ids"])
        self.saved = dict(self.d)
        self.build()

    @property
    def dirty(self) -> bool:
        return self.d != self.saved

    # ---- layout
    def build(self):
        self.clear_items()
        g = self.guild
        ch = g.get_channel(self.d["channel_id"] or 0)
        cat = g.get_channel(self.d["category_id"] or 0)
        roles = [r for r in (g.get_role(i) for i in self.d["role_ids"]) if r]

        s_ch = discord.ui.ChannelSelect(
            placeholder="📢 Panel channel — where the panel is sent",
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            default_values=[ch] if ch else [], row=0)
        s_ch.callback = lambda i: self.on_select(i, "channel_id", s_ch)

        s_cat = discord.ui.ChannelSelect(
            placeholder="📁 Ticket category — where tickets are created",
            channel_types=[discord.ChannelType.category],
            default_values=[cat] if cat else [], row=1)
        s_cat.callback = lambda i: self.on_select(i, "category_id", s_cat)

        s_roles = discord.ui.RoleSelect(
            placeholder="🛡️ Staff roles — who can see the tickets",
            min_values=0, max_values=25, default_values=roles, row=2)
        s_roles.callback = lambda i: self.on_select(i, "role_ids", s_roles)

        for item in (s_ch, s_cat, s_roles):
            self.add_item(item)

        buttons = [
            ("Edit Panel", "✏️", discord.ButtonStyle.secondary, self.on_edit_text, 3),
            ("Ticket Settings", "⚙️", discord.ButtonStyle.secondary, self.on_edit_settings, 3),
            ("Back", "⬅️", discord.ButtonStyle.secondary, self.on_back, 4),
            ("Save", "💾", discord.ButtonStyle.primary, self.on_save, 4),
            ("Send Panel", "📨", discord.ButtonStyle.success, self.on_send, 4),
            ("Delete", "🗑️", discord.ButtonStyle.danger, self.on_delete, 4),
        ]
        for label, emoji, style, cb, row in buttons:
            b = discord.ui.Button(label=label, emoji=emoji, style=style, row=row)
            b.callback = cb
            self.add_item(b)

    def embeds(self) -> list[discord.Embed]:
        g, d = self.guild, self.d
        rec = store.get(self.pid) or META
        ch = g.get_channel(d["channel_id"] or 0)
        cat = g.get_channel(d["category_id"] or 0)
        posted = g.get_channel(rec["posted_channel_id"] or 0)
        roles = [r for r in (g.get_role(i) for i in d["role_ids"]) if r]

        info = discord.Embed(
            title=f"Panel Editor — {d['name']}",
            description=("🟡 **Unsaved changes** — press **Save**.\n" if self.dirty else "")
                        + (f"📨 Live in {posted.mention}" if posted else "📭 Not sent yet")
                        + "\n*Any unsaved changes will be discarded when you press Back*",
            color=dashboard_color(),
        )
        info.add_field(name="📢 Panel channel", value=ch.mention if ch else "❌ not selected")
        info.add_field(name="📁 Ticket category", value=f"**{cat.name}**" if cat else "❌ not selected")
        info.add_field(name="🔢 Tickets opened", value=str(rec["counter"]))
        info.add_field(name="🛡️ Staff roles",
                       value=" ".join(r.mention for r in roles) if roles else "❌ none", inline=False)
        info.add_field(name="🔘 Button",
                       value=f"{d['button_emoji'] or ''} {d['button_label']}".strip())
        info.add_field(name="🏷️ Channel name",
                       value=f"`{channel_name(d['name_format'], self.owner, rec['counter'] + 1)}`")
        info.add_field(name="👋 Welcome message", value=d["welcome"][:1024], inline=False)
        preview = panel_embed(d)
        preview.set_author(name="Preview")
        return [info, preview]

    async def refresh(self, interaction: discord.Interaction, note: str = ""):
        self.build()
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(content=note or None, embeds=self.embeds(), view=self)
            else:
                await interaction.response.edit_message(content=note or None, embeds=self.embeds(), view=self)
        except (discord.NotFound, discord.HTTPException):
            pass

    # ---- callbacks
    async def on_select(self, interaction: discord.Interaction, key: str, select):
        try:
            if key == "role_ids":
                self.d[key] = [r.id for r in select.values]
            else:
                self.d[key] = select.values[0].id if select.values else None
            await self.refresh(interaction)
        except (discord.NotFound, discord.HTTPException):
            pass

    async def on_edit_text(self, interaction: discord.Interaction):
        await interaction.response.send_modal(PanelTextModal(self))

    async def on_edit_settings(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TicketSettingsModal(self))

    async def on_back(self, interaction: discord.Interaction):
        self.stop()
        embed, view = manager_screen(self.owner, self.guild)
        await interaction.response.edit_message(content=None, embed=embed, view=view)

    def _problems(self) -> list[str]:
        g, d, me = self.guild, self.d, self.guild.me
        ch = g.get_channel(d["channel_id"] or 0)
        cat = g.get_channel(d["category_id"] or 0)
        out = []
        if not ch:
            out.append("Select a **panel channel**.")
        else:
            p = ch.permissions_for(me)
            if not (p.view_channel and p.send_messages and p.embed_links):
                out.append(f"I need **Send Messages** + **Embed Links** in {ch.mention}.")
        if not cat:
            out.append("Select a **ticket category**.")
        elif not cat.permissions_for(me).manage_channels:
            out.append(f"I need **Manage Channels** in **{cat.name}**.")
        if not d["role_ids"]:
            out.append("Select at least one **staff role**.")
        return out

    async def _commit(self) -> str:
        """Save the draft and update the live panel message in place (if any)."""
        store.update(self.pid, **self.d)
        self.saved = dict(self.d)
        rec = store.get(self.pid)
        ch = self.guild.get_channel(rec["posted_channel_id"] or 0)
        if ch and rec["message_id"]:
            try:
                msg = await ch.fetch_message(rec["message_id"])
                await msg.edit(embed=panel_embed(rec), view=panel_view(self.pid, rec))
                return f"💾 Saved. The live panel in {ch.mention} was updated."
            except discord.NotFound:
                store.update(self.pid, message_id=None, posted_channel_id=None)
            except discord.HTTPException as e:
                return f"💾 Saved, but I couldn't update the live panel: {e.text or e}"
        return "💾 Saved."

    async def on_save(self, interaction: discord.Interaction):
        await interaction.response.defer()
        note = await self._commit()
        self.build()
        await interaction.edit_original_response(content=note, embeds=self.embeds(), view=self)

    async def on_send(self, interaction: discord.Interaction):
        problems = self._problems()
        if problems:
            return await self.refresh(interaction, note="\n".join(f"⚠️ {p}" for p in problems))
        await interaction.response.defer()
        await self._commit()
        rec = store.get(self.pid)
        target = self.guild.get_channel(rec["channel_id"])

        # remove the old copy so there is only one live panel
        old_ch = self.guild.get_channel(rec["posted_channel_id"] or 0)
        if old_ch and rec["message_id"]:
            try:
                await (await old_ch.fetch_message(rec["message_id"])).delete()
            except discord.HTTPException:
                pass
        try:
            msg = await target.send(embed=panel_embed(rec), view=panel_view(self.pid, rec))
        except discord.HTTPException as e:
            note = f"❌ Couldn't send the panel: {e.text or e}"
        else:
            store.update(self.pid, message_id=msg.id, posted_channel_id=target.id)
            note = f"📨 Panel sent: {msg.jump_url}"
        self.build()
        await interaction.edit_original_response(content=note, embeds=self.embeds(), view=self)

    async def on_delete(self, interaction: discord.Interaction):
        self.message = interaction.message
        await interaction.response.send_message(
            f"Delete panel **{self.d['name']}**? The live panel message will be removed. "
            "Open ticket channels are not touched.",
            view=ConfirmDeleteView(self), ephemeral=True)


class ConfirmDeleteView(discord.ui.View):
    def __init__(self, editor: EditorView):
        super().__init__(timeout=60)
        self.editor = editor

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        ed = self.editor
        rec = store.get(ed.pid)
        if rec:
            ch = ed.guild.get_channel(rec["posted_channel_id"] or 0)
            if ch and rec["message_id"]:
                try:
                    await (await ch.fetch_message(rec["message_id"])).delete()
                except discord.HTTPException:
                    pass
            store.delete(ed.pid)
        await interaction.response.edit_message(content="🗑️ Panel deleted.", view=None)
        ed.stop()
        embed, view = manager_screen(ed.owner, ed.guild)
        try:
            await ed.message.edit(content=None, embeds=[embed], view=view)
        except (discord.HTTPException, AttributeError):
            pass

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", view=None)


# ---------------------------------------------------------------- bot
class TicketBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())

    async def setup_hook(self):
        self.add_dynamic_items(OpenTicketButton)
        self.add_view(CloseView())
        synced = await self.tree.sync()
        print(f"Synced {len(synced)} slash command(s).")
        port = int(os.environ.get("PORT", 0))
        if port:
            from aiohttp import web
            app = web.Application()
            app.router.add_get("/", lambda r: web.Response(text="Bot is running!"))
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", port)
            await site.start()
            print(f"Health server running on port {port}")

    async def on_ready(self):
        print(f"Logged in as {self.user} in {len(self.guilds)} server(s).")


bot = TicketBot()


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    msg = f"Error: {error}"
    print(msg)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


# ---------------------------------------------------------------- commands
@bot.tree.command(name="setup", description="Open the ticket panel editor & manager")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def setup(interaction: discord.Interaction):
    await interaction.response.defer()
    embed, view = manager_screen(interaction.user, interaction.guild)
    await interaction.followup.send(embed=embed, view=view)


@bot.tree.command(name="close", description="Close this ticket channel")
@app_commands.guild_only()
async def close_cmd(interaction: discord.Interaction):
    channel = interaction.channel
    m = TOPIC_RE.match(getattr(channel, "topic", None) or "")
    if not m:
        return await interaction.response.send_message(
            "This is not a ticket channel.", ephemeral=True)

    panel = store.get(m.group(1)) or {}
    opener_id = int(m.group(2))
    role_ids = set(panel.get("role_ids", []))
    user = interaction.user
    allowed = (
        user.id == opener_id
        or any(r.id in role_ids for r in user.roles)
        or user.guild_permissions.manage_channels
    )
    if not allowed:
        return await interaction.response.send_message(
            "Only the ticket owner or staff can close this ticket.", ephemeral=True)

    await interaction.response.send_message(
        "Are you sure you want to close this ticket?", view=ConfirmCloseView(), ephemeral=True)


@bot.tree.command(name="help", description="How to use the ticket bot")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Ticket Bot — Help",
        description=(
            "**/setup** — open the Panel Editor & Manager (needs Manage Server)\n\n"
            "1. Press **Create A Panel** and give it a name\n"
            "2. Pick the **panel channel**, **ticket category** and **staff roles**\n"
            "3. Use **Edit Panel** / **Ticket Settings** to change any text\n"
            "4. Press **Save**, then **Send Panel**\n\n"
            "Members click the panel button → a private ticket channel is created that "
            "only they and the staff roles can see. Editing and saving a panel later "
            "updates the live panel message automatically."
        ),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------- startup
if __name__ == "__main__":
    bot.run(TOKEN)
