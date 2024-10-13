# Lines configured by zsh-newuser-install
# EDIT: added appendhistory
HISTFILE=~/.histfile
HISTSIZE=1000
SAVEHIST=10000
setopt INC_APPEND_HISTORY_TIME
unsetopt beep
bindkey -v
# End of lines configured by zsh-newuser-install
# The following lines were added by compinstall
zstyle :compinstall filename '/home/carter/.zshrc'

autoload -Uz compinit
compinit
# End of lines added by compinstall

# User config

# git info in prompt
autoload -Uz vcs_info
zstyle ':vcs_info:*' enable git
#zstyle ':vcs_info:*' check-for-changes true
#zstyle ':vcs_info:git*' actionformats "- %B%F{red}%r%f on %F{green}[%b]%f %m%u%c"
zstyle ':vcs_info:git*' formats "- %B%F{red}%r%f on %F{green}[%b]%f"
precmd() {
    vcs_info
}
setopt prompt_subst
# prompt
PS1='%B%n%b @ %F{cyan}%B%/%b%f ${vcs_info_msg_0_}%b $ '



# add ~/.zfunc to fpath, then lazy autoload
# every file in there as a function
fpath=(~/.zfunc $fpath)
autoload -U $fpath[1]/*(.:t)

# alias
alias cdwork="cd ~/workspace"
#alias cdsand="cd ~/workspace/sandbox"
alias cdconfig="cd ~/.config"
#alias cdnotes="cd ~/Documents/notes"
alias ls="ls --color=auto"
alias checkmirrors="nvim /etc/pacman.d/mirrorlist"
alias refreshmirrors="sudo systemctl start reflector.service"
alias zs="source ~/.zshrc"
alias ez="nvim ~/.zshrc"
alias checkbat="cat /sys/class/power_supply/BAT0/capacity"
#alias colorpick="grim -g \"$(slurp -p)\" -t ppm - | convert - -format '%[pixel:p{0,0}]' txt:-"
alias cmatrix="cmatrix -b -C blue -u 5"
alias mountusb1="sudo mount -v /dev/usb1 /mnt/usb1"
alias mountusb2="sudo mount -v /dev/usb2 /mnt/usb2"
alias cdnvim="cd ~/.config/nvim"
# dotfiles sync
alias dotfiles='/usr/bin/git --git-dir="$HOME/.dotfiles/" --work-tree="$HOME"'


export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"  # This loads nvm
[ -s "$NVM_DIR/bash_completion" ] && \. "$NVM_DIR/bash_completion"  # This loads nvm bash_completion
