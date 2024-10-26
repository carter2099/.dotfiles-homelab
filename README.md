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
## Pacman
  - Remove package
      - `sudo pacman -Rnsv [package]`
## AUR
  - To install from AUR
    - Acquire build files by cloning into `~./builds` folder
    - In the resulting directory, inspect the PKGBUILD
    - Build the package with `makepkg -sirc`
      - `-s` automatically resolves and installs any dependencies with pacman 
      before building
      - `-i` installs the package if it is built successfully
      - `-r` removes build-time deps after the build
      - `-c` cleans up temp build files after the build
    - Install the package
      - `pacman -U package_name-version-arch.pkg.tar.zst`
  - https://wiki.archlinux.org/title/Arch_User_Repository
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
## Audio
  - ALSA -> PipeWire -> WirePlumber
      - ALSA: The default Linux kernel component providing device drivers and 
      lowest-level support for audio hardware
      - PipeWire: Sound server
      - WirePlumber: Session manager for PipeWire
  - Bluetooth
    - Use `bluetoothctl`
      - `scan on|off` to turn on/off device scanning
      - `devices` list devices
      - `pair [device]` to pair
      - `connect [device]` to connect
      - `info [device]` to show info on a device
  - Player
    - cmus
      - Run `cmus`
      - Add/remove tracks to library
      - See `man cmus-tutorial` for help
      - `7` shows all keybinds
      - `5` to go to file browser view
        - `a` adds tracks to library
      - `2` returns to simple library view
        - `D` removes tracks
        - `<enter>` to play
        - `c` to pause/play
  - Download audio from youtube
    - `ytmp3 '[url]'`
      - zsh alias. See `ez`
  - `~/music` contains audio library
## Backup
  - ~/documents/backup
  - Run ./full.sh to backup to /mnt/usb1 and then copy it to /mnt/usb2
  - Make sure to run `mountusb1` and `mountusb2` before running
## zsh
  - Run `ez` to view/edit ~/.zshrc
  - Run `zs` to source ~/.zshrc
## tmux
  - See ~/.tmux.conf
  - **prefix** is set to \<C-space>
  - New window: **prefix + c**
  - Next window: **prefix + n**
  - Previous window: **prefix + p**
  - Split current pane into top and bottom: **prefix + "**
  - Split current pane into left and right: **prefix + %**
  - Move between panes:
    - Show pane indexes: **prefix + q**
    - Press number shown on pane to swap to it
## nvim
  - `:Lazy` for package manager
  - `:Mason` for lsp package manager
  - `<leader>` = space
      - `<leader>y` copies into system clipboard
      - `<leader>pv` fuzzy find files
      - `<leader>ps` text search
      - `<leader>f` lsp format current buffer
## Backlight
  - Run `setbright [percentage]` to set the backlight brightness to \[percentage\]
## Clipboard
  - Use command substitution to paste contents in a shell command: `$(wl-paste)`
  - Pipe into `wl-copy` store in clipboard
## Image viewer
  - `imv [filename]` to view image in new window
  - `imv -f [filename]` to view image fullscreen
## Screenshot
  - **mod + shift + p**
    - mod is Sway mod
  - Saved in ~/documents/screenshots
  - Uses `grim`
## Misc alias
  - `refreshmirrors`: refresh pacman mirrors
  - `checkbat`: echo battery percentage
  - `mountusb1`: mount left USBA
  - `mountusb2`: mount right USBA



### Checklist
  - Compositor
    - Sway
  - Window Manager
    - Sway
  - Git
  - Status Bar
    - Swaybar
  - Launcher
    - Fuzzel
  - Wallpaper
    - Swaybg
  - Theming
    - Colors in various configs. Should probably have a better solution here
  - Dotfiles sync
    - Bare git repo 
      - https://github.com/carter2077/.dotfiles
      - https://wiki.archlinux.org/title/Dotfiles
  - Clipboard
    - wl-copy
  - Brightness
    - `setbright [percentage]`
    - ~/.zfunc/setbright
  - Backup
    - See `Backup` above
  - Editor
    - Neovim with Lazy
  - Audio
    - ALSA -> PipeWire -> WirePlumber
    - cmus player
    - yt-dlp



