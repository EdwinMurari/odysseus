# Mac → Windows Dev Setup (Tailscale + SSH + SMB)

**Goal:** Edit code with native Mac apps (Claude Code / Codex desktop), run & deploy on the Windows 11 Pro PC. Secure, encrypted, private to your tailnet. No port forwarding, no internet exposure.

**Architecture**

- **SMB over Tailscale** → editing. The Windows project folder mounts on the Mac as a drive; native Mac apps edit it directly. One live copy, no sync, no git latency.
- **SSH over Tailscale** → run / build / deploy. Commands fired from the Mac execute on Windows.
- Everything rides the encrypted WireGuard tunnel and is locked to the tailnet (`100.x` addresses). Nothing is reachable from the open LAN or internet.

> Replace placeholders throughout:
> - `WINUSER` = your Windows username
> - `WIN_TS_IP` = Windows PC's Tailscale IP (run `tailscale ip -4` on Windows, e.g. `100.101.102.103`)
> - `winpc` = the Windows machine's Tailscale MagicDNS name (optional, nicer than the IP)

---

## Part 0 — Tailscale sanity check (both machines)

1. On Windows: `tailscale ip -4` → note the `100.x.x.x` address (this is `WIN_TS_IP`).
2. On Mac: `tailscale status` → confirm the Windows PC is listed and reachable.
3. Test reachability from Mac: `ping WIN_TS_IP`.

If MagicDNS is on (Tailscale admin console → DNS → enable MagicDNS), you can use the machine name `winpc` instead of the IP everywhere below.

---

## Part 1 — SSH on Windows (run / build / deploy), hardened

### 1a. Install & start OpenSSH Server

PowerShell **as Administrator** on the Windows PC:

```powershell
# Install (skip if already installed)
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0

# Start now + start automatically on boot
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic
```

### 1b. Add your Mac's public key

On the **Mac**, create a key if you don't have one:

```bash
ssh-keygen -t ed25519 -C "macbook-air"
# press Enter for default path (~/.ssh/id_ed25519), set a passphrase if you like
cat ~/.ssh/id_ed25519.pub        # copy this line
```

On **Windows**, because your account is an Administrator, the key must go in the **admin** keys file (this is the #1 gotcha — the per-user `authorized_keys` is ignored for admins):

```powershell
# Paste your Mac public key into this file (create it if missing)
notepad C:\ProgramData\ssh\administrators_authorized_keys
```

Then fix its permissions (required, or sshd silently refuses the key):

```powershell
icacls C:\ProgramData\ssh\administrators_authorized_keys /inheritance:r
icacls C:\ProgramData\ssh\administrators_authorized_keys /grant "Administrators:F" /grant "SYSTEM:F"
```

> If your Windows account is a **standard** user instead, put the key in `C:\Users\WINUSER\.ssh\authorized_keys` and skip the icacls step.

### 1c. Disable password auth (key-only)

Edit `C:\ProgramData\ssh\sshd_config` and set:

```
PubkeyAuthentication yes
PasswordAuthentication no
```

Restart sshd:

```powershell
Restart-Service sshd
```

### 1d. Lock SSH to the Tailscale network only

Block SSH from everywhere except the tailnet, so it's never exposed on the LAN/internet:

```powershell
# Remove/disable the broad default rule if present, then add a tailnet-only rule
New-NetFirewallRule -DisplayName "SSH from Tailscale only" -Direction Inbound `
  -Protocol TCP -LocalPort 22 -RemoteAddress 100.64.0.0/10 -Action Allow

# (Optional) block port 22 from all other sources explicitly
New-NetFirewallRule -DisplayName "Block SSH non-Tailscale" -Direction Inbound `
  -Protocol TCP -LocalPort 22 -RemoteAddress LocalSubnet -Action Block
```

### 1e. Test from the Mac

```bash
ssh WINUSER@WIN_TS_IP
# you should land in a PowerShell prompt ON the Windows PC, no password
```

### 1f. Convenience: SSH config alias

On the Mac, add to `~/.ssh/config`:

```
Host winpc
    HostName WIN_TS_IP
    User WINUSER
    IdentityFile ~/.ssh/id_ed25519
```

Now `ssh winpc` is all you need.

---

## Part 2 — SMB on Windows (editing), private to Tailscale

### 2a. Share the projects folder

In Windows Explorer: right-click your projects folder (e.g. `D:\Projects`) → **Properties → Sharing → Advanced Sharing → Share this folder**. Set Permissions so your `WINUSER` has **Read/Write** (Full Control). Note the share name (default = folder name).

### 2b. Restrict SMB to Tailscale (critical for security)

SMB (port 445) must **never** be open to the LAN or internet. Lock it to the tailnet:

```powershell
# Allow SMB only from Tailscale addresses
New-NetFirewallRule -DisplayName "SMB from Tailscale only" -Direction Inbound `
  -Protocol TCP -LocalPort 445 -RemoteAddress 100.64.0.0/10 -Action Allow

# Block SMB from the regular LAN
New-NetFirewallRule -DisplayName "Block SMB non-Tailscale" -Direction Inbound `
  -Protocol TCP -LocalPort 445 -RemoteAddress LocalSubnet -Action Block
```

Also confirm you are **not** forwarding ports 445/139 on your router (you shouldn't be — Tailscale removes any need for it).

### 2c. Performance fix — disable SMB MultiChannel

There's a known issue where SMB MultiChannel tanks performance over Tailscale. Disable it on Windows:

```powershell
Set-SmbServerConfiguration -EnableMultiChannel $false -Force
```

### 2d. Mount on the Mac

Finder → **Go → Connect to Server** (`Cmd+K`):

```
smb://WIN_TS_IP/ShareName
```

Enter your Windows username/password when prompted; check "remember in Keychain." The share appears under `/Volumes/ShareName` and in Finder's sidebar.

To make it auto-mount, add it to **System Settings → General → Login Items**.

---

## Part 3 — Point the native apps at it

Open Claude Code desktop / Codex desktop on the Mac and open the project from the mounted share (e.g. `/Volumes/Projects/odysseus`). Edits save straight to the Windows disk — one live copy, no sync step.

**IMPORTANT — where agent commands run.** Claude Code / Codex desktop run on the Mac, so any terminal command *they* execute runs on **macOS**, not Windows. To keep build/run/deploy on Windows, configure the agent's run/build command to go through SSH. For example, set the project's build command to:

```bash
ssh winpc "cd D:/Projects/odysseus && npm run build"
```

That way even agent-triggered builds execute on the Windows PC.

---

## Part 4 — The daily loop

**Edit:** native Mac apps on `/Volumes/Projects/odysseus`.

**Build / deploy — one line from the Mac:**

```bash
ssh winpc "cd D:/Projects/odysseus && npm install && npm run build && npm run deploy"
```

**Interactive session on Windows:**

```bash
ssh winpc        # then cd and run anything
```

**Handy Mac aliases** (`~/.zshrc`):

```bash
alias ody='ssh winpc "cd D:/Projects/odysseus && npm run dev"'
alias odybuild='ssh winpc "cd D:/Projects/odysseus && npm run build"'
alias odydeploy='ssh winpc "cd D:/Projects/odysseus && npm run deploy"'
```

**Long-running dev server / watcher:** so it survives the SSH disconnect, manage it with a process manager on Windows (e.g. for Node: `npm i -g pm2`, then `pm2 start npm --name odysseus -- run dev`). Restart from the Mac with `ssh winpc "pm2 restart odysseus"`.

---

## Security summary

- **Encryption:** all SSH + SMB traffic rides Tailscale's WireGuard tunnel.
- **No exposure:** ports 22 and 445 are firewalled to `100.64.0.0/10` (tailnet) and blocked from the LAN; nothing is port-forwarded.
- **SSH:** key-only, passwords disabled.
- **Scope:** local/tailnet development only — no public ingress.

## Quick troubleshooting

- **SSH key refused:** admin account → key must be in `administrators_authorized_keys` with the icacls permissions from 1b.
- **Can't mount SMB:** confirm the Tailscale firewall rule (2b) and that you're using `WIN_TS_IP`, not the LAN IP.
- **SMB feels slow:** confirm MultiChannel is disabled (2c); prefer the Tailscale direct connection (`tailscale status` should show a direct, not relayed/DERP, path).
- **Build runs on Mac instead of Windows:** the agent ran it locally — route the command through `ssh winpc "..."` (Part 3).
