#!/usr/bin/env bash
# verilog_script one-click installer.
#
#   ./install.sh            install into ~/.vim (plugin + python package)
#   ./install.sh --check    only check the environment (vim + python>=3.7)
#   ./install.sh --manager  print plugin-manager snippets, install nothing
#
# Plugin-manager users (vim-plug / Vundle / packer / dein) do NOT need this
# script — see README.md.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIN_PY_MAJOR=3
MIN_PY_MINOR=7

err() { echo "[verilog_script] ERROR: $*" >&2; exit 1; }
info() { echo "[verilog_script] $*"; }

check_vim() {
    command -v vim >/dev/null 2>&1 || err "vim not found in PATH"
    info "vim: $(vim --version | head -1)"
}

check_python() {
    local py="${1:-python3}"
    command -v "$py" >/dev/null 2>&1 || err "python3 not found (install Python >= ${MIN_PY_MAJOR}.${MIN_PY_MINOR}, or set g:verilog_tooling_python)"
    "$py" -c "import sys; sys.exit(sys.version_info < (${MIN_PY_MAJOR}, ${MIN_PY_MINOR}))" \
        || err "$py is too old: $("$py" -V 2>&1) — need Python >= ${MIN_PY_MAJOR}.${MIN_PY_MINOR}"
    info "python: $("$py" -V 2>&1) ($py)"
}

case "${1:-}" in
    --check)
        check_vim
        check_python "${2:-python3}"
        info "environment OK"
        exit 0
        ;;
    --manager)
        cat <<'EOF'
Plugin-manager snippets (add to your vimrc):

  " vim-plug
  Plug 'marshaha/verilog_script'

  " Vundle
  Plugin 'marshaha/verilog_script'

  " packer.nvim
  use 'marshaha/verilog_script'

  " dein.vim
  call dein#add('marshaha/verilog_script')

Requirements: Python >= 3.7 in PATH (or set g:verilog_tooling_python).
EOF
        exit 0
        ;;
    "")
        check_vim
        PY="$(command -v python3 || true)"
        [ -n "$PY" ] && check_python "$PY"
        DEST="$HOME/.vim"
        mkdir -p "$DEST/plugin" "$DEST/python"
        cp "$SRC_DIR/plugin/"*.vim "$DEST/plugin/"
        rm -rf "$DEST/python/verilog_tooling"
        cp -R "$SRC_DIR/python/verilog_tooling" "$DEST/python/"
        info "installed:"
        info "  $DEST/plugin/verilog_tooling.vim"
        info "  $DEST/plugin/automatic.vim"
        info "  $DEST/python/verilog_tooling/  (stdlib-only, no pip needed)"
        info "restart vim, then :AALL on a Verilog file to verify"
        ;;
    *)
        err "unknown option: $1 (use --check or --manager)"
        ;;
esac
