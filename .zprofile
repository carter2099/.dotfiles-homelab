# Login shell init for systemd user services (non-interactive).
# .zshrc is only sourced by interactive shells, so fnm/PATH must
# be set up here for OMP Web and other systemd-managed services.
export FNM_PATH="/home/carter/.local/share/fnm"
if [ -d "$FNM_PATH" ]; then
  export PATH="$FNM_PATH:$PATH"
  eval "$(fnm env)"
fi
export PATH="$HOME/.bun/bin:$HOME/.local/bin:$PATH"

# Keep CLI and service control commands pointed at the relocated OMP Web state.
export PI_WEB_DATA_DIR="/srv/omp-web/carter/pi-web"
