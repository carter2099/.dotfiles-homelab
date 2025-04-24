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

# prompt
# using starship instead
# not on homelab (yet)
PS1='%B%n%b @ %F{green}%B%/%b%f $ '

# add ~/.zfunc to fpath, then lazy autoload
# every file in there as a function
# ** no functions yet on homelab so this errors
#fpath=(~/.zfunc $fpath)
#autoload -U $fpath[1]/*(.:t)

# alias
alias cdconfig="cd ~/.config"
alias ls="ls --color=auto"
alias zs="source ~/.zshrc"
alias ez="nvim ~/.zshrc"
alias cmatrix="cmatrix -b -C blue -u 5"
alias cdnvim="cd ~/.config/nvim"
alias k="kubectl"
# dotfiles sync
alias dotfiles='/usr/bin/git --git-dir="$HOME/.dotfiles-homelab/" --work-tree="$HOME"'
alias carterhelp='nvim ~/README.md'


export KUBECONFIG=~/.kube/config


# fnm
FNM_PATH="/home/carter/.local/share/fnm"
if [ -d "$FNM_PATH" ]; then
  export PATH="$FNM_PATH:$PATH"
  eval "`fnm env`"
fi

eval "$(fnm env --use-on-cd --shell zsh)"

