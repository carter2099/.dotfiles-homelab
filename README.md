# System Notes
## Wifi
  - Get an interactive prompt with `sudo iwctl`
    - Run `help` to see available commands
  - Once in the prompt, run:
    - `device list`
    - `station [name] scan`
    - `station [name] get-networks`
    - `station [name] connect [SSID]`
      - A password prompt will appear if necessary
      - Or include it directly to an iwctl command:
        - `iwctl --passphrase [passphrase] station [name] connect [SSID]`
## VPN
  - Ensure connection
      - Check /etc/wireguard for wg config
      - Check that `curl ifconfig.me` matches ip address from config
## Sway
  - See full config at ~/.config/sway
  - At time of writing, mod key = alt
  - Keybinds
    - New window: **mod + enter**
    - Launcher: **mod + d**
    - Move focus: **mod + \[h|j|k|l\]**
    - Move window: **mod + shift + \[h|j|k|l\]**
    - Exit window: **mod + shift + q**
    - Enter resize mode: **mod + r**
      - Grow/shrink left|down|up|right: **mod + \[h|j|k|l\]**
      - Exit resize mode: **\[return|escape\]**
    - Reload sway config: **mod + shift + c**
    - Switch layouts:
      - Use split layout: **mod + e**
          - Horizontal: **mod + b**
          - Vertical: **mod + v**
      - Use stacking layout: **mod + s**
      - Use tabbed layout: **mod + w**
## Backup
  - ~/documents/backup
  - Run ./full.sh to backup to /mnt/usb1 and then copy it to /mnt/usb2
  - Make sure to run `mountusb1` and `mountusb2` before running
## zsh
  - Run `ez` to view/edit ~/.zshrc
  - Run `zs` to source ~/.zshrc
## nvim
  - `:Lazy` for package manager
  - `:Mason` for lsp package manager
  - `<leader>` = space
      - `<leader>y` copies into system clipboard
      - `<leader>pv` fuzzy find files
      - `<leader>ps` text search
      - `<leader>f` lsp format current buffer
## Backlight
  - Run `setbright [percentage]` to set the backlight brightness to [percentage]
## Clipboard
  - Use command substitution to paste contents in a shell command: `$(wl-paste)`
  - Pipe into `wl-copy` store in clipboard
## Screenshot
  - mod + shift + p
    - mod is Sway mod
  - Saved in ~/documents/screenshots
## Misc alias
  - `refreshmirrors`: refresh pacman mirrors
  - `checkbat`: echo battery percentage
  - `mountusb1`: mount left USBA
  - `mountusb2`: mount right USBA


