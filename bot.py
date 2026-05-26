"""
Cyphra Hub – Discord Bot
Key system + Bloxlink account linking + server protection
"""

import asyncio
import collections
import datetime
import json
import os
import time

import aiohttp
import discord
from discord import app_commands

# ─── Config ──────────────────────────────────────────────────────────────────

JNKIE_API_KEY   = "032ec4a8-3705-495a-bce2-657482ed9bd3"
JNKIE_BASE      = "https://api.jnkie.com/api/v2"
SERVICE         = "service"
PROVIDER        = "provider"
CHECKPOINT_LINK = "https://jnkie.com/get-key/cyphrahub"
ADMIN_USER_ID   = 1271121429295005776
BOT_TOKEN       = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

BLOXLINK_BASE   = "https://api.blox.link/v4/public"
ROBLOX_THUMB    = "https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={uid}&size=150x150&format=Png"

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
os.makedirs(DATA_DIR, exist_ok=True)

# ─── Persistence ─────────────────────────────────────────────────────────────

def _path(f):
    return os.path.join(DATA_DIR, f)

def load_json(filename, default):
    try:
        with open(_path(filename)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def save_json(filename, data):
    with open(_path(filename), "w") as f:
        json.dump(data, f, indent=2)

# ─── State ───────────────────────────────────────────────────────────────────

settings       = load_json("settings.json", {
    "audit_channel":   None,
    "general_channel": None,
})
lockdown_state = load_json("lockdown.json", {"active": False, "overrides": {}})
whitelist      = set(load_json("whitelist.json", [ADMIN_USER_ID]))
linked_accounts = load_json("linked_accounts.json", {})   # {discord_id: {roblox_id, roblox_username, linked_at}}

# ─── Rate-tracking ───────────────────────────────────────────────────────────

channel_deletions: list[float] = []
role_deletions:    list[float] = []
ban_times:         list[float] = []
kick_times:        list[float] = []
join_times:        list[float] = []
raid_mode = {"active": False, "ends_at": 0.0}

msg_times:   dict[int, collections.deque] = collections.defaultdict(lambda: collections.deque(maxlen=20))
msg_content: dict[int, collections.deque] = collections.defaultdict(lambda: collections.deque(maxlen=30))
recent_msgs: dict[int, list]              = collections.defaultdict(list)

# ─── Bot ─────────────────────────────────────────────────────────────────────

intents = discord.Intents.all()
bot     = discord.Client(intents=intents)
tree    = app_commands.CommandTree(bot)

# ─── Utility helpers ─────────────────────────────────────────────────────────

def is_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.id == ADMIN_USER_ID

def fmt_remaining(expires_at_str: str) -> tuple[str, discord.Color]:
    try:
        expires = datetime.datetime.fromisoformat(expires_at_str)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=datetime.timezone.utc)
        now  = datetime.datetime.now(datetime.timezone.utc)
        diff = expires - now
        secs = diff.total_seconds()
        if secs <= 0:
            return "Expired", discord.Color.red()
        hours, rem = divmod(int(secs), 3600)
        minutes    = rem // 60
        color      = discord.Color.orange() if hours < 1 else discord.Color.green()
        label      = f"{hours}h {minutes}m remaining" if hours > 0 else f"{minutes}m remaining"
        return label, color
    except Exception:
        return "Unknown", discord.Color.greyple()

async def jnkie_request(method: str, path: str, **kwargs):
    url     = f"{JNKIE_BASE}{path}"
    headers = {"Authorization": f"Bearer {JNKIE_API_KEY}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, headers=headers, **kwargs) as resp:
            try:    data = await resp.json()
            except: data = {}
            return resp.status, data

async def bloxlink_request(path: str):
    url = f"{BLOXLINK_BASE}{path}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            try:    data = await resp.json()
            except: data = {}
            return resp.status, data

async def get_roblox_avatar(roblox_id: int) -> str | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(ROBLOX_THUMB.format(uid=roblox_id)) as resp:
                data = await resp.json()
                return data["data"][0]["imageUrl"]
    except Exception:
        return None

async def get_roblox_user_by_name(username: str) -> dict | None:
    """Returns {id, name} or None."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://users.roblox.com/v1/usernames/users",
                json={"usernames": [username], "excludeBannedUsers": False},
            ) as resp:
                data = await resp.json()
                users = data.get("data", [])
                if users:
                    return {"id": users[0]["id"], "name": users[0]["name"]}
    except Exception:
        pass
    return None

async def get_audit_channel(guild: discord.Guild) -> discord.TextChannel | None:
    cid = settings.get("audit_channel")
    if cid:
        ch = guild.get_channel(int(cid))
        if ch: return ch
    return discord.utils.get(guild.text_channels, name="audit-logs")

async def get_general_channel(guild: discord.Guild) -> discord.TextChannel | None:
    cid = settings.get("general_channel")
    if cid:
        ch = guild.get_channel(int(cid))
        if ch: return ch
    for name in ("general", "chat", "lobby"):
        ch = discord.utils.get(guild.text_channels, name=name)
        if ch: return ch
    return guild.text_channels[0] if guild.text_channels else None

async def dm_admin(content: str = None, embed: discord.Embed = None):
    try:
        admin = await bot.fetch_user(ADMIN_USER_ID)
        await admin.send(content=content, embed=embed)
    except Exception:
        pass

async def log_event(guild: discord.Guild, embed: discord.Embed):
    ch = await get_audit_channel(guild)
    if ch:
        try: await ch.send(embed=embed)
        except Exception: pass

async def lock_everyone(guild: discord.Guild) -> dict:
    overrides = {}
    for channel in guild.channels:
        if isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
            ow = channel.overwrites_for(guild.default_role)
            overrides[str(channel.id)] = {"send_messages": ow.send_messages}
            ow.send_messages = False
            try:
                await channel.set_permissions(guild.default_role, overwrite=ow,
                                               reason="Cyphra protection lockdown")
            except Exception: pass
    return overrides

async def unlock_everyone(guild: discord.Guild, overrides: dict):
    for channel in guild.channels:
        if isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
            ow   = channel.overwrites_for(guild.default_role)
            prev = overrides.get(str(channel.id), {})
            ow.send_messages = prev.get("send_messages", None)
            try:
                await channel.set_permissions(guild.default_role, overwrite=ow,
                                               reason="Cyphra protection unlock")
            except Exception: pass

# ─── Key system commands ─────────────────────────────────────────────────────

@tree.command(name="getkey", description="Get your Cyphra Hub key")
async def cmd_getkey(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        embed = discord.Embed(
            title="🔑 Cyphra Hub – Get Your Key",
            description=(
                f"Click the link below to complete the checkpoint and receive your key.\n\n"
                f"**[Get Key → {CHECKPOINT_LINK}]({CHECKPOINT_LINK})**\n\n"
                "After completing, use `/mykeys` to view your active keys."
            ),
            color=discord.Color.blue(),
        )
        embed.set_footer(text="Cyphra Hub Key System")
        try:
            await interaction.user.send(embed=embed)
            await interaction.followup.send("📬 Check your DMs!", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I couldn't DM you. Please enable **DMs from Server Members** "
                "in your Privacy Settings and try again.",
                ephemeral=True,
            )
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


@tree.command(name="mykeys", description="View your active Cyphra Hub keys")
async def cmd_mykeys(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        status, data = await jnkie_request(
            "GET", "/keys",
            params={"service": SERVICE, "provider": PROVIDER},
        )
        if status != 200:
            await interaction.followup.send(f"❌ API error ({status}). Try again later.", ephemeral=True)
            return

        keys      = data if isinstance(data, list) else data.get("keys", [])
        uid       = str(interaction.user.id)
        user_keys = [k for k in keys if str(k.get("discord_id", "")) == uid]

        if not user_keys:
            await interaction.followup.send(
                "🔍 No keys found. Use `/getkey` to get one!", ephemeral=True
            )
            return

        # Check for linked Roblox account
        roblox_info = linked_accounts.get(uid)
        roblox_line = ""
        if roblox_info:
            roblox_line = f"\n🎮 Linked Roblox: **{roblox_info['roblox_username']}** (`{roblox_info['roblox_id']}`)"

        embeds = []
        for k in user_keys[:5]:
            key_val    = k.get("key", "???")
            expires_at = k.get("expires_at", "")
            created_at = k.get("created_at", "Unknown")
            remaining, color = fmt_remaining(expires_at)

            embed = discord.Embed(
                title=f"🔑 Key: `{key_val[:12]}...`",
                color=color,
            )
            embed.add_field(name="Status",  value=remaining, inline=True)
            embed.add_field(name="Premium", value="✅" if k.get("is_premium") else "❌", inline=True)
            embed.add_field(name="Created", value=created_at[:10], inline=True)
            if roblox_line:
                embed.add_field(name="Roblox Account", value=roblox_line.strip(), inline=False)
            embeds.append(embed)

        await interaction.followup.send(embeds=embeds, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


@tree.command(name="generate", description="[Admin] Generate a new key")
@app_commands.describe(hours="Validity in hours (default 24)", premium="Premium key?")
async def cmd_generate(interaction: discord.Interaction, hours: int = 24, premium: bool = False):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        status, data = await jnkie_request(
            "POST", "/keys",
            json={"service": SERVICE, "provider": PROVIDER,
                  "validity_hours": hours, "is_premium": premium},
        )
        if status not in (200, 201):
            await interaction.followup.send(f"❌ API error ({status}): {data}", ephemeral=True)
            return
        key     = data.get("key", "???")
        expires = data.get("expires_at", "Unknown")
        embed   = discord.Embed(title="✅ Key Generated", color=discord.Color.green())
        embed.add_field(name="Key",     value=f"```{key}```", inline=False)
        embed.add_field(name="Expires", value=expires,        inline=True)
        embed.add_field(name="Premium", value="✅" if premium else "❌", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


@tree.command(name="revoke", description="[Admin] Revoke a key")
@app_commands.describe(key="The full key to revoke")
async def cmd_revoke(interaction: discord.Interaction, key: str):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        status, data = await jnkie_request("DELETE", f"/keys/{key}")
        if status in (200, 204):
            await interaction.followup.send(f"✅ Key `{key[:12]}...` revoked.", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ API error ({status}): {data}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


@tree.command(name="stats", description="[Admin] View key statistics")
async def cmd_stats(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        status, data = await jnkie_request("GET", "/keys",
                                            params={"service": SERVICE, "provider": PROVIDER})
        if status != 200:
            await interaction.followup.send(f"❌ API error ({status})", ephemeral=True)
            return
        keys    = data if isinstance(data, list) else data.get("keys", [])
        now     = datetime.datetime.now(datetime.timezone.utc)
        total   = len(keys)
        expired = 0
        premium = 0
        for k in keys:
            try:
                exp = datetime.datetime.fromisoformat(k.get("expires_at", ""))
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=datetime.timezone.utc)
                if exp < now: expired += 1
            except Exception: pass
            if k.get("is_premium"): premium += 1
        embed = discord.Embed(title="📊 Key Statistics", color=discord.Color.blue())
        embed.add_field(name="Total",   value=total,           inline=True)
        embed.add_field(name="Active",  value=total - expired, inline=True)
        embed.add_field(name="Expired", value=expired,         inline=True)
        embed.add_field(name="Premium", value=premium,         inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


@tree.command(name="setup", description="[Admin] Post the Cyphra Hub info embed")
async def cmd_setup(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    embed = discord.Embed(
        title="🔑 Cyphra Hub – Key System",
        description=(
            "Welcome to **Cyphra Hub**!\n\n"
            "**Step 1 →** Use `/getkey` to get the checkpoint link in your DMs.\n"
            "**Step 2 →** Complete the checkpoint to receive your key.\n"
            "**Step 3 →** Use `/mykeys` to view your active keys.\n"
            "**Optional →** Use `/link <username>` to link your Roblox account.\n\n"
            f"**Direct Link:** {CHECKPOINT_LINK}"
        ),
        color=discord.Color.blue(),
    )
    embed.set_footer(text="Cyphra Hub • Key System")
    await interaction.response.send_message(embed=embed)


# ─── Roblox account linking ──────────────────────────────────────────────────

@tree.command(name="link", description="Link your Roblox account")
@app_commands.describe(roblox_username="Your Roblox username")
async def cmd_link(interaction: discord.Interaction, roblox_username: str):
    await interaction.response.defer(ephemeral=True)
    uid = str(interaction.user.id)
    try:
        # 1. Check if Bloxlink already has a link for this Discord user
        status, data = await bloxlink_request(
            f"/guilds/{interaction.guild_id}/discord-to-roblox/{interaction.user.id}"
        )
        if status == 200 and data.get("robloxID"):
            roblox_id   = int(data["robloxID"])
            roblox_name = data.get("resolved", {}).get("roblox", {}).get("name", roblox_username)
        else:
            # 2. Look up the provided username via Roblox API
            roblox_data = await get_roblox_user_by_name(roblox_username)
            if not roblox_data:
                await interaction.followup.send(
                    f"❌ Roblox user **{roblox_username}** not found. Check the spelling and try again.",
                    ephemeral=True,
                )
                return
            roblox_id   = roblox_data["id"]
            roblox_name = roblox_data["name"]

        # 3. Store in local JSON
        linked_accounts[uid] = {
            "roblox_id":       roblox_id,
            "roblox_username": roblox_name,
            "linked_at":       datetime.datetime.utcnow().isoformat(),
        }
        save_json("linked_accounts.json", linked_accounts)

        avatar_url = await get_roblox_avatar(roblox_id)
        embed = discord.Embed(
            title="✅ Roblox Account Linked",
            description=f"Successfully linked to **{roblox_name}** (`{roblox_id}`)",
            color=discord.Color.green(),
        )
        embed.add_field(name="Roblox Username", value=roblox_name,  inline=True)
        embed.add_field(name="Roblox ID",       value=str(roblox_id), inline=True)
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)
        embed.set_footer(text="Use /unlink to remove this link")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


@tree.command(name="verify", description="Auto-link your Roblox account via Bloxlink")
async def cmd_verify(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    uid = str(interaction.user.id)
    try:
        status, data = await bloxlink_request(
            f"/guilds/{interaction.guild_id}/discord-to-roblox/{interaction.user.id}"
        )
        if status != 200 or not data.get("robloxID"):
            await interaction.followup.send(
                "❌ No Bloxlink link found for your account.\n"
                "Use `/link <roblox_username>` to link manually, "
                "or verify with Bloxlink first at <https://blox.link>.",
                ephemeral=True,
            )
            return

        roblox_id   = int(data["robloxID"])
        roblox_name = data.get("resolved", {}).get("roblox", {}).get("name", str(roblox_id))

        linked_accounts[uid] = {
            "roblox_id":       roblox_id,
            "roblox_username": roblox_name,
            "linked_at":       datetime.datetime.utcnow().isoformat(),
        }
        save_json("linked_accounts.json", linked_accounts)

        avatar_url = await get_roblox_avatar(roblox_id)
        embed = discord.Embed(
            title="✅ Verified via Bloxlink",
            description=f"Linked to **{roblox_name}** (`{roblox_id}`)",
            color=discord.Color.green(),
        )
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


@tree.command(name="unlink", description="Remove your linked Roblox account")
async def cmd_unlink(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    uid = str(interaction.user.id)
    if uid not in linked_accounts:
        await interaction.followup.send("❌ You don't have a linked Roblox account.", ephemeral=True)
        return
    removed = linked_accounts.pop(uid)
    save_json("linked_accounts.json", linked_accounts)
    await interaction.followup.send(
        f"✅ Unlinked **{removed['roblox_username']}** from your account.",
        ephemeral=True,
    )


@tree.command(name="whois", description="Look up a user's linked Roblox account")
@app_commands.describe(member="The Discord user to look up")
async def cmd_whois(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    uid  = str(member.id)
    info = linked_accounts.get(uid)
    if not info:
        await interaction.followup.send(
            f"❌ {member.mention} hasn't linked their Roblox account. "
            "They can use `/link <username>` to do so.",
            ephemeral=True,
        )
        return
    avatar_url = await get_roblox_avatar(info["roblox_id"])
    embed = discord.Embed(
        title=f"🔍 {member.display_name}'s Roblox Account",
        color=discord.Color.blue(),
    )
    embed.add_field(name="Roblox Username", value=info["roblox_username"],      inline=True)
    embed.add_field(name="Roblox ID",       value=str(info["roblox_id"]),       inline=True)
    embed.add_field(name="Linked At",       value=info["linked_at"][:10],       inline=True)
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="hwid", description="Check a key's HWID and linked accounts")
@app_commands.describe(key="The key to check")
async def cmd_hwid(interaction: discord.Interaction, key: str):
    await interaction.response.defer(ephemeral=True)
    try:
        status, data = await jnkie_request(
            "POST", "/flow/verify-key",
            json={"key": key, "service": SERVICE, "provider": PROVIDER},
        )

        embed = discord.Embed(title="🔍 Key & HWID Info", color=discord.Color.blue())

        # Key validity
        if status == 200:
            valid      = data.get("valid", False)
            expires_at = data.get("expires_at", "")
            hwid       = data.get("hwid") or "Not set"
            discord_id = str(data.get("discord_id") or "")
            remaining, color = fmt_remaining(expires_at)
            embed.color = color
            embed.add_field(name="Key Valid",   value="✅ Yes" if valid else "❌ No", inline=True)
            embed.add_field(name="Expires",     value=remaining,                     inline=True)
            embed.add_field(name="HWID",        value=f"`{hwid}`",                   inline=False)

            # Linked Discord account from Jnkie metadata
            if discord_id:
                try:
                    disc_user = await bot.fetch_user(int(discord_id))
                    embed.add_field(
                        name="Linked Discord",
                        value=f"{disc_user} (`{discord_id}`)",
                        inline=False,
                    )
                    # Check local JSON for their Roblox link
                    roblox_info = linked_accounts.get(discord_id)
                    if roblox_info:
                        avatar_url = await get_roblox_avatar(roblox_info["roblox_id"])
                        embed.add_field(
                            name="Linked Roblox",
                            value=f"**{roblox_info['roblox_username']}** (`{roblox_info['roblox_id']}`)",
                            inline=False,
                        )
                        if avatar_url:
                            embed.set_thumbnail(url=avatar_url)
                    else:
                        embed.add_field(name="Linked Roblox", value="Not linked", inline=False)
                except Exception:
                    embed.add_field(name="Linked Discord", value=f"`{discord_id}`", inline=False)
            else:
                embed.add_field(name="Linked Discord", value="Not linked", inline=False)
        else:
            embed.color = discord.Color.red()
            embed.add_field(name="Status", value=f"❌ API error ({status})", inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


# ─── Admin / server commands ─────────────────────────────────────────────────

@tree.command(name="lockdown", description="[Admin] Lock all channels immediately")
async def cmd_lockdown(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    guild     = interaction.guild
    overrides = await lock_everyone(guild)
    lockdown_state["active"]    = True
    lockdown_state["overrides"] = overrides
    save_json("lockdown.json", lockdown_state)
    ch = await get_general_channel(guild)
    if ch:
        try:
            await ch.send(embed=discord.Embed(
                title="🔒 Server Lockdown",
                description="The server is under lockdown. All channels have been locked. Please stand by.",
                color=discord.Color.red(),
            ))
        except Exception: pass
    await dm_admin(f"🔒 Lockdown activated on **{guild.name}**.")
    await interaction.followup.send("✅ Server locked down.", ephemeral=True)


@tree.command(name="unlock", description="[Admin] Lift the lockdown")
async def cmd_unlock(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    await unlock_everyone(guild, lockdown_state.get("overrides", {}))
    lockdown_state["active"]    = False
    lockdown_state["overrides"] = {}
    save_json("lockdown.json", lockdown_state)
    ch = await get_general_channel(guild)
    if ch:
        try:
            await ch.send(embed=discord.Embed(
                title="🔓 Lockdown Lifted",
                description="The server lockdown has been lifted. Normal messaging has been restored.",
                color=discord.Color.green(),
            ))
        except Exception: pass
    await dm_admin(f"🔓 Lockdown lifted on **{guild.name}**.")
    await interaction.followup.send("✅ Lockdown lifted.", ephemeral=True)


@tree.command(name="setsettings", description="[Admin] Update channel settings")
@app_commands.describe(
    audit_channel_id="Channel ID for audit logs",
    general_channel_id="Channel ID for announcements",
)
async def cmd_settings(
    interaction: discord.Interaction,
    audit_channel_id:   str = None,
    general_channel_id: str = None,
):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    if audit_channel_id:   settings["audit_channel"]   = audit_channel_id
    if general_channel_id: settings["general_channel"] = general_channel_id
    save_json("settings.json", settings)
    await interaction.response.send_message("✅ Settings saved.", ephemeral=True)


# ─── Server Protection ───────────────────────────────────────────────────────

def _prune(lst: list, window: float):
    cutoff = time.time() - window
    while lst and lst[0] < cutoff:
        lst.pop(0)

async def _nuke_lockdown(guild: discord.Guild, reason: str):
    embed = discord.Embed(
        title="🚨 Anti-Nuke Triggered",
        description=f"**Reason:** {reason}\n\nServer locked for 5 minutes.",
        color=discord.Color.red(),
        timestamp=datetime.datetime.utcnow(),
    )
    await log_event(guild, embed)
    await dm_admin(embed=embed)
    overrides = await lock_everyone(guild)
    await asyncio.sleep(300)
    await unlock_everyone(guild, overrides)

@bot.event
async def on_guild_channel_delete(channel):
    channel_deletions.append(time.time())
    _prune(channel_deletions, 5)
    if len(channel_deletions) >= 3:
        channel_deletions.clear()
        asyncio.create_task(_nuke_lockdown(
            channel.guild,
            f"Mass channel deletion (3+ in 5s). Last: #{channel.name}"
        ))

@bot.event
async def on_guild_role_delete(role):
    role_deletions.append(time.time())
    _prune(role_deletions, 5)
    if len(role_deletions) >= 3:
        role_deletions.clear()
        asyncio.create_task(_nuke_lockdown(
            role.guild,
            f"Mass role deletion (3+ in 5s). Last: @{role.name}"
        ))

@bot.event
async def on_member_ban(guild, user):
    ban_times.append(time.time())
    _prune(ban_times, 10)
    if len(ban_times) >= 5:
        ban_times.clear()
        asyncio.create_task(_nuke_lockdown(
            guild,
            f"Ban wave (5+ in 10s). Last: {user} ({user.id})"
        ))

@bot.event
async def on_member_remove(member):
    async def _check():
        await asyncio.sleep(1)
        try:
            async for entry in member.guild.audit_logs(limit=5, action=discord.AuditLogAction.kick):
                if entry.target.id == member.id:
                    kick_times.append(time.time())
                    _prune(kick_times, 10)
                    if len(kick_times) >= 5:
                        kick_times.clear()
                        asyncio.create_task(_nuke_lockdown(
                            member.guild,
                            f"Mass kick (5+ in 10s). Last: {member} ({member.id})"
                        ))
                    return
        except Exception: pass
    asyncio.create_task(_check())

@bot.event
async def on_webhooks_update(channel):
    async def _check():
        await asyncio.sleep(1)
        try:
            async for entry in channel.guild.audit_logs(limit=3, action=discord.AuditLogAction.webhook_create):
                if entry.user.id not in whitelist:
                    embed = discord.Embed(
                        title="⚠️ Webhook Created by Non-Admin",
                        description=f"**By:** {entry.user} ({entry.user.id})\n**Channel:** #{channel.name}",
                        color=discord.Color.orange(),
                        timestamp=datetime.datetime.utcnow(),
                    )
                    await log_event(channel.guild, embed)
                    await dm_admin(embed=embed)
                return
        except Exception: pass
    asyncio.create_task(_check())

@bot.event
async def on_member_join(member):
    join_times.append(time.time())
    _prune(join_times, 10)
    if len(join_times) >= 5 and not raid_mode["active"]:
        raid_mode["active"]  = True
        raid_mode["ends_at"] = time.time() + 600
        asyncio.create_task(_start_raid_mode(member.guild))
    if raid_mode["active"]:
        embed = discord.Embed(
            title="🚨 Raid Mode: New Join",
            description=f"{member} ({member.id}) joined during raid mode.",
            color=discord.Color.red(),
        )
        await log_event(member.guild, embed)

async def _start_raid_mode(guild: discord.Guild):
    try:
        await guild.edit(verification_level=discord.VerificationLevel.highest,
                         reason="Cyphra anti-raid")
    except Exception: pass
    overrides = await lock_everyone(guild)
    embed = discord.Embed(
        title="🚨 Raid Mode Activated",
        description="5+ members joined within 10s. Server locked for 10 minutes.",
        color=discord.Color.red(),
        timestamp=datetime.datetime.utcnow(),
    )
    await log_event(guild, embed)
    await dm_admin(embed=embed)
    await asyncio.sleep(600)
    raid_mode["active"] = False
    await unlock_everyone(guild, overrides)
    try:
        await guild.edit(verification_level=discord.VerificationLevel.medium,
                         reason="Cyphra anti-raid: expired")
    except Exception: pass
    await log_event(guild, discord.Embed(
        title="✅ Raid Mode Deactivated",
        description="10 minutes elapsed. Server unlocked.",
        color=discord.Color.green(),
    ))

@bot.event
async def on_guild_update(before, after):
    changes = []
    if before.name != after.name:               changes.append(f"Name: `{before.name}` → `{after.name}`")
    if before.icon != after.icon:               changes.append("Icon changed")
    if before.verification_level != after.verification_level:
        changes.append(f"Verification: `{before.verification_level}` → `{after.verification_level}`")
    if not changes: return
    async def _check():
        await asyncio.sleep(1)
        try:
            async for entry in after.audit_logs(limit=3, action=discord.AuditLogAction.guild_update):
                if entry.user.id not in whitelist:
                    embed = discord.Embed(
                        title="⚠️ Server Settings Changed",
                        description="\n".join(changes) + f"\n\n**By:** {entry.user} ({entry.user.id})",
                        color=discord.Color.orange(),
                        timestamp=datetime.datetime.utcnow(),
                    )
                    await log_event(after, embed)
                    await dm_admin(embed=embed)
                return
        except Exception: pass
    asyncio.create_task(_check())

# ─── Anti-Spam ───────────────────────────────────────────────────────────────

async def _timeout(member: discord.Member, minutes: int, reason: str):
    try:
        until = discord.utils.utcnow() + datetime.timedelta(minutes=minutes)
        await member.timeout(until, reason=reason)
    except Exception: pass

async def _bulk_delete(guild: discord.Guild, uid: int, since: float):
    msgs = recent_msgs.get(uid, [])
    to_del = [m for m in msgs if m.created_at.timestamp() >= since]
    by_ch: dict[int, list] = collections.defaultdict(list)
    for m in to_del: by_ch[m.channel.id].append(m)
    for cid, cmsgs in by_ch.items():
        ch = guild.get_channel(cid)
        if not ch: continue
        try:
            if len(cmsgs) == 1: await cmsgs[0].delete()
            else: await ch.delete_messages(cmsgs[:100])
        except Exception: pass

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    uid  = message.author.id
    now  = time.time()
    mb   = message.author

    recent_msgs[uid].append(message)
    if len(recent_msgs[uid]) > 30:
        recent_msgs[uid] = recent_msgs[uid][-30:]

    # Rate spam: 5 msgs in 3s → 5-min timeout
    msg_times[uid].append(now)
    rate_msgs = [t for t in msg_times[uid] if t >= now - 3]
    if len(rate_msgs) >= 5:
        msg_times[uid].clear()
        asyncio.create_task(_bulk_delete(message.guild, uid, now - 3))
        asyncio.create_task(_timeout(mb, 5, "Spam: rate limit"))
        asyncio.create_task(log_event(message.guild, discord.Embed(
            title="🚫 Spam – Rate",
            description=f"{mb} ({uid}) sent 5+ msgs in 3s. Timed out 5m.",
            color=discord.Color.orange(),
        )))
        try: await mb.send("⚠️ Timed out for 5 minutes for spamming.")
        except Exception: pass
        return

    # Duplicate spam: 10 same msgs in 10s → 10-min timeout
    content = message.content.strip().lower()
    msg_content[uid].append((now, content))
    recent_content = [(t, c) for t, c in msg_content[uid] if t >= now - 10]
    if sum(1 for _, c in recent_content if c == content) >= 10:
        msg_content[uid].clear()
        asyncio.create_task(_bulk_delete(message.guild, uid, now - 10))
        asyncio.create_task(_timeout(mb, 10, "Spam: duplicate messages"))
        asyncio.create_task(log_event(message.guild, discord.Embed(
            title="🚫 Spam – Duplicates",
            description=f"{mb} ({uid}) sent same message 10+ times. Timed out 10m.",
            color=discord.Color.orange(),
        )))
        try: await mb.send("⚠️ Timed out for 10 minutes for duplicate spam.")
        except Exception: pass
        return

    # Mass mention: 10+ mentions → 15-min timeout
    if len(message.mentions) + len(message.role_mentions) >= 10:
        try: await message.delete()
        except Exception: pass
        asyncio.create_task(_timeout(mb, 15, "Spam: mass mention"))
        asyncio.create_task(log_event(message.guild, discord.Embed(
            title="🚫 Mass Mention",
            description=f"{mb} ({uid}) mentioned 10+ users/roles. Timed out 15m.",
            color=discord.Color.red(),
        )))
        try: await mb.send("⚠️ Timed out for 15 minutes for mass mentioning.")
        except Exception: pass

# ─── Startup ─────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"Cyphra Bot online as {bot.user}")
    try:
        synced = await tree.sync()
        print(f"Synced {len(synced)} commands")
    except Exception as e:
        print(f"Command sync error: {e}")
    print("Protection systems active")

bot.run(BOT_TOKEN)
