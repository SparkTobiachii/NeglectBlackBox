# Neglect Black Box reader (Windows)

Logger for staff PCs. Reads `%LOCALAPPDATA%Low\VRChat\VRChat\output_log_*.txt`, sends **events only** (never the log, never a VRChat login) to the bot, and only while the person is in an instance of the group. No actions, no moderation.

## For staff (install and use)
1. Get `NeglectBlackBox-Setup.exe` from the DEV owner and run it (no admin rights needed). It installs per user, starts the reader and adds it to Windows autostart.
2. Your browser opens once: log in with Discord. That proves who you are (only staff can link a PC).
3. The DEV owner approves your PC in the dashboard (Black Box > Devices). From then on it records by itself whenever you are in a group instance; leaving the instance, closing or crashing VRChat ends it.
4. **Ctrl+Shift+N** shows or hides the program window (dark, Neglect colours). It has no button in the taskbar and the X only hides it, so the logger keeps running. It shows what the logger does and has **Stop logging** (nothing is read or sent until you press **Start logging**; this survives a restart), **Check for updates** and a small **Exit completely**.
   - Ctrl+Shift+N is also the "new private window" shortcut of Chrome and Edge. While the reader runs, the reader gets it. Change it in `%APPDATA%\NeglectBlackBox\settings.json` (`"hotkey": "ctrl+alt+b"`), then restart the reader. When the keys are taken by another program the window opens once and the Start menu entry "Neglect Black Box" opens it.
5. The program **updates itself**: every 5 minutes it asks the bot, and installs a newer version silently (only when the SHA-256 of the installer matches). The button checks at once.
6. Uninstall: Windows Settings > Apps > Neglect Black Box. Data folder: `%APPDATA%\NeglectBlackBox` (`reader.log`, `settings.json`).

## For the DEV owner (release and updates)
The reader lives in the **public** repository `SparkTobiachii/NeglectBotDashboard` (this folder and `neglect/blackbox_parse.py` are the same files as in the bot repository; the bot repository is the master, copy changes over). Nothing needs a secret:
1. Raise `VERSION` in `reader.py` and push to `main` of NeglectBotDashboard. The workflow `Black Box reader (Windows)` starts by itself (or by hand: Actions > Run workflow). It builds `NeglectBlackBox.exe` (PyInstaller, Python + Tk bundled, no console window), runs `selftest` on it, builds `NeglectBlackBox-Setup.exe` (Inno Setup, `installer.iss`), prints the SHA-256 and makes the GitHub release `blackbox-v<version>` with the installer and `SHA256SUMS.txt`.
2. The bot looks at the latest release every few minutes (when a reader reports in), downloads the installer itself, checks the SHA-256 against `SHA256SUMS.txt` and offers it to every PC at once. The PCs ask the bot for the installer (signed request), so older readers update too.
3. **By hand** (if GitHub is not reachable): dashboard > Black Box > Devices > Reader update, enter the version and pick `NeglectBlackBox-Setup.exe`. With "Offer a new upload to every PC at once" switched off, a new version waits for the button "Offer it now" (this also stops the automatic pick-up from GitHub).
4. Readers install only a version that is newer than theirs, only from the bot (signed request) or from `github.com/SparkTobiachii/NeglectBotDashboard/releases/download/`, and only when the SHA-256 matches.

## Developing
- `reader.py run | show | selftest`; needs `PyNaCl` (and `tkinter` for the window, Windows only); the parser is `neglect/blackbox_parse.py` (shared with the bot, no other bot code). The window code is `bb_window.py`, the assets (logo, icon, installer images) are in `assets/`.
- Tests: `tests/test_blackbox_reader.py` (state machine, update rules, window logic with a stand-in for tkinter; run on Linux). The Windows parts (DPAPI, hotkey, tray-less window, registry autostart, installer) can only be tried on Windows.
- `cache_probe.py`: read-only probe of the VRChat avatar cache. Prints a report; nothing is sent.
