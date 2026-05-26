# Cyphra Hub Discord Bot

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure the bot
Open `bot.py` and replace `YOUR_BOT_TOKEN_HERE` with your actual bot token.

### 3. Discord Developer Portal
In your application's Bot settings, enable all three **Privileged Gateway Intents**:
- Presence Intent
- Server Members Intent
- Message Content Intent

### 4. Run
```bash
python bot.py
```

---

## Commands

| Command | Access | Description |
|---------|--------|-------------|
| `/getkey` | Everyone | DMs the checkpoint link |
| `/mykeys` | Everyone | Shows your active keys |
| `/generate` | Admin | Generate a new key |
| `/revoke` | Admin | Revoke a key by value |
| `/stats` | Admin | Key statistics |
| `/setup` | Admin | Post the hub info embed |
| `/lockdown` | Admin | Lock all channels |
| `/unlock` | Admin | Lift the lockdown |
| `/setsettings` | Admin | Update audit/general channel IDs |

---

## Protection Systems (automatic)

| System | Trigger | Action |
|--------|---------|--------|
| Anti-Nuke | 3+ channel deletions in 5s | 5-min lockdown + DM admin |
| Anti-Nuke | 3+ role deletions in 5s | 5-min lockdown + DM admin |
| Anti-Nuke | 5+ bans in 10s | 5-min lockdown + DM admin |
| Anti-Nuke | 5+ kicks in 10s | 5-min lockdown + DM admin |
| Anti-Nuke | Webhook created by non-admin | Log + DM admin |
| Anti-Nuke | Bot added by non-admin | Log + DM admin |
| Anti-Nuke | Server settings changed by non-admin | Log + DM admin |
| Anti-Raid | 5+ joins in 10s | 10-min lockdown + max verification |
| Anti-Spam | 5+ messages in 3s | Delete msgs + 5-min timeout |
| Anti-Spam | 10+ identical messages in 10s | Delete msgs + 10-min timeout |
| Anti-Spam | 10+ mentions in one message | Delete msg + 15-min timeout |

---

## Data files (`data/` folder)

| File | Purpose |
|------|---------|
| `settings.json` | Firebase URL, secret, channel IDs |
| `lockdown.json` | Lockdown state and channel permission snapshots |
| `whitelist.json` | User IDs exempt from protection triggers |

---

## Setting the audit and general channels

After inviting the bot, run:
```
/setsettings audit_channel_id:YOUR_CHANNEL_ID general_channel_id:YOUR_CHANNEL_ID
```

The bot will also auto-detect a channel named `#audit-logs` and `#general` if no IDs are set.

