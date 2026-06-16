# Edit on Mac, Run on Windows — SMB + SSH Setup

A one-time setup so you edit code **natively on the MacBook Air** (Claude Code / Codex
desktop apps) while it **runs, builds, and deploys on the Windows 11 Pro PC**, over the
same WiFi. Stack-agnostic — the run/build/deploy commands are slots you fill with whatever
the project uses.

**Two pieces working together:**
- **SMB** = the Windows project folder is mounted on the Mac, so there's ONE live copy of
  the files. Edits from the Mac write straight to the PC. No git, no sync software.
- **SSH** = how a command you type on the Mac executes on the Windows PC. Builds, deploys,
  and the dev server all run on Windows.

Replace `<WIN_USER>` (your Windows username) and `<WIN_IP>` (the PC's IPv4) throughout.

---

## Part 1 — Windows PC (one time)

### 1a. Find the PC's local IP
Open PowerShell and run:
```powershell
ipconfig
```
Note the **IPv4 Address** under your active adapter (e.g. `192.168.1.42`). That's `<WIN_IP>`.

> Recommended: in your router, reserve this IP for the PC (DHCP reservation) so it never
> changes. Otherwise it may shift and break your mount/SSH config.

### 1b. Enable OpenSSH Server
Settings → System → Optional features → **Add a feature** → install **OpenSSH Server**.
Then, in an **admin** PowerShell:
```powershell
Start-Service sshd
Set-Service -Name sshd -StartupType 'Automatic'
# Allow it through the firewall (usually auto-added, run to be sure):
New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH Server (sshd)' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22
```
Test locally: `ssh <WIN_USER>@localhost` — should prompt for your Windows password.

### 1c. Share the projects folder over SMB
Right-click your projects folder (the one containing `odysseus`) → **Properties** →
**Sharing** tab → **Advanced Sharing** → check **Share this folder** → **Permissions** →
give your user **Full Control** → OK.

Note the share name shown (default is the folder name). Path from the Mac will be:
`smb://<WIN_IP>/<ShareName>`.

> Make sure the network is set to **Private** (Settings → Network → your WiFi → Private),
> or Windows blocks SMB/discovery.

### 1d. (For always-on dev server) install a process manager
So a dev/watch server survives you disconnecting. Node-friendly and stack-agnostic via
`pm2` (it can supervise *any* command, not just Node):
```powershell
# If you have Node:
npm install -g pm2
# pm2 can run any process: pm2 start "<your command>" --name odysseus
```
If you don't use Node at all, you can instead leave an SSH session open, or use
**NSSM** (Non-Sucking Service Manager) to run any command as a Windows service. pm2 is the
simplest for dev.

---

## Part 2 — MacBook Air (one time)

### 2a. Mount the Windows folder (SMB)
Finder → **Go → Connect to Server** (`Cmd+K`) → enter:
```
smb://<WIN_IP>/<ShareName>
```
Authenticate with your **Windows** username/password. Check "Remember this password" so it
auto-reconnects. The folder now appears under `/Volumes/<ShareName>` and in Finder's sidebar.

Point **Claude Code / Codex desktop apps** at `/Volumes/<ShareName>/odysseus`. They edit
natively on the Mac; the bytes live on the PC — one copy, no sync.

> To auto-remount on login: System Settings → General → Login Items → add the mounted
> volume. Or drag the mounted drive into Login Items.

### 2b. Passwordless SSH (do this — it's what makes it frictionless)
On the Mac:
```bash
ssh-keygen -t ed25519 -f ~/.ssh/odysseus_win -C "macbook-to-windows"
# press Enter through prompts (or set a passphrase)
```
Copy the **public** key to Windows. Easiest from the Mac:
```bash
ssh <WIN_USER>@<WIN_IP> "powershell -Command \"Add-Content -Path \$env:USERPROFILE\\.ssh\\authorized_keys -Value '$(cat ~/.ssh/odysseus_win.pub)'\""
```
> If that one-liner is fiddly, just `cat ~/.ssh/odysseus_win.pub`, copy it, and paste it as
> a new line into `C:\Users\<WIN_USER>\.ssh\authorized_keys` on the PC (create the file if
> needed).

**Windows admin-account gotcha:** if `<WIN_USER>` is an administrator, Windows OpenSSH
ignores the per-user `authorized_keys` and instead reads
`C:\ProgramData\ssh\administrators_authorized_keys`. Put the key there for admin accounts,
and fix its permissions in admin PowerShell:
```powershell
icacls C:\ProgramData\ssh\administrators_authorized_keys /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F"
```

### 2c. Add an SSH host alias
Edit `~/.ssh/config` on the Mac:
```
Host win
    HostName <WIN_IP>
    User <WIN_USER>
    IdentityFile ~/.ssh/odysseus_win
    ServerAliveInterval 30
```
Now `ssh win` logs you straight into a Windows PowerShell session, no password.

---

## Part 3 — The daily loop

### Run any command on Windows from the Mac
Interactive session:
```bash
ssh win
# you're now in PowerShell ON the PC:
cd C:\path\to\odysseus
<install command>
<build command>
```

One-shot (runs on Windows, output streams back to your Mac):
```bash
ssh win "cd C:/path/to/odysseus; <your command>"
```
> Use `;` between commands (PowerShell). Forward slashes work fine in `cd`.

### Handy Mac-side aliases (stack-agnostic slots)
Add to `~/.zshrc`, then fill in the command for whatever odysseus uses:
```bash
ODY='cd C:/path/to/odysseus'

alias ody-build='ssh win "'"$ODY"'; <BUILD_CMD>"'      # e.g. npm run build / docker compose build / make build
alias ody-deploy='ssh win "'"$ODY"'; <DEPLOY_CMD>"'    # e.g. npm run deploy / docker compose up -d
alias ody-shell='ssh win'                              # jump onto the PC
```
After editing: `source ~/.zshrc`. Then `ody-build`, `ody-deploy` run on Windows.

### Always-on dev server (survives disconnect) — via pm2
Start it once (from the Mac, runs on Windows):
```bash
ssh win "cd C:/path/to/odysseus; pm2 start \"<DEV_CMD>\" --name odysseus"
#   <DEV_CMD> = whatever starts your dev/watch server, e.g.:
#     npm run dev   |   docker compose up   |   uvicorn app:app --reload --host 0.0.0.0
```
Manage it anytime:
```bash
ssh win "pm2 logs odysseus"        # tail logs
ssh win "pm2 restart odysseus"     # restart after dependency changes
ssh win "pm2 stop odysseus"        # stop
ssh win "pm2 list"                 # what's running
ssh win "pm2 save; pm2 startup"    # (run once) keep it running across PC reboots
```
The dev server runs entirely on the PC. Because the files are SMB-mounted, edits you make on
the Mac are *the same files* the server is watching — hot reload triggers on Windows when you
save on the Mac. **Access the running app from the Mac's browser** at
`http://<WIN_IP>:<PORT>` (make sure the dev server binds to `0.0.0.0`, not just `localhost`,
or it won't be reachable across the network — and allow the port through the Windows
firewall).

---

## The one sharp edge: where agent-run commands execute

Claude Code / Codex **desktop apps run on the Mac**. If you let the *agent* run a terminal
command (build, test, deploy), it runs on **macOS**, not Windows — wrong machine.

To keep execution on Windows, do one of:
- **Preferred:** let the agent only *edit files*; you run builds/deploys via the `ody-*`
  aliases or `ssh win`.
- **Or:** tell the agent its build/run command IS an SSH call, e.g. configure the build
  command as:
  ```
  ssh win "cd C:/path/to/odysseus; <BUILD_CMD>"
  ```
  Then even agent-triggered builds land on the PC.

---

## Performance notes (SMB over WiFi)
- File reads/writes cross the network. Editing is fine; very large repos or heavy
  file-watchers (huge `node_modules`, big asset trees) can feel slower than local.
- Use **5GHz WiFi** or, ideally, **wired Ethernet** on at least the PC — biggest single
  speed win.
- If one specific project's watcher/build feels laggy over SMB, that's the case to move
  *that project* to a local clone + Mutagen sync. Everything else can stay on SMB.

---

## Quick reference

| Goal | Command (from Mac) |
|------|--------------------|
| Mount files | Finder `Cmd+K` → `smb://<WIN_IP>/<ShareName>` |
| Shell on PC | `ssh win` |
| One-shot build | `ssh win "cd C:/path/to/odysseus; <BUILD_CMD>"` |
| Start dev server | `ssh win "cd C:/.../odysseus; pm2 start \"<DEV_CMD>\" --name odysseus"` |
| Tail logs | `ssh win "pm2 logs odysseus"` |
| Restart server | `ssh win "pm2 restart odysseus"` |
| Open running app | browser → `http://<WIN_IP>:<PORT>` |
