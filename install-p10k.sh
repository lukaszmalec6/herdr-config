#!/usr/bin/env bash
# Install the Powerlevel10k prompt config (p10k.zsh) from this repo.
#
# Usage:
#   ./install-p10k.sh
#
# Idempotent. An existing ~/.p10k.zsh is backed up with a date suffix.
# After running `p10k configure`, copy the regenerated file back into the
# repo so the change is tracked: cp ~/.p10k.zsh ./p10k.zsh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$REPO_DIR/p10k.zsh"
DST="${P10K_CONFIG:-$HOME/.p10k.zsh}"
STAMP="$(date +%Y%m%d-%H%M%S)"

if [ ! -f "$SRC" ]; then
    echo "missing $SRC" >&2
    exit 1
fi

if [ -e "$DST" ] && ! cmp -s "$SRC" "$DST"; then
    cp "$DST" "$DST.bak-$STAMP"
    echo "backed up $DST -> $DST.bak-$STAMP"
fi
cp "$SRC" "$DST"
echo "installed $DST"

# Powerlevel10k itself is not installed here; point at the missing piece.
theme_installed=0
for dir in "$(brew --prefix 2>/dev/null)/share/powerlevel10k" \
           "$HOME/powerlevel10k" \
           "$HOME/.oh-my-zsh/custom/themes/powerlevel10k" \
           /usr/share/powerlevel10k; do
    if [ -n "$dir" ] && [ -f "$dir/powerlevel10k.zsh-theme" ]; then
        theme_installed=1
        break
    fi
done
if [ "$theme_installed" -eq 0 ]; then
    echo "note: powerlevel10k theme not found; install it with: brew install powerlevel10k" >&2
fi

ZSH_RC="${ZDOTDIR:-$HOME}/.zshrc"
if [ -f "$ZSH_RC" ] && ! grep -q 'p10k\.zsh' "$ZSH_RC"; then
    echo "note: $ZSH_RC does not source ~/.p10k.zsh; add:" >&2
    echo "  [[ ! -f ~/.p10k.zsh ]] || source ~/.p10k.zsh" >&2
fi

echo
echo "Done. Open a new shell, or run: source ~/.p10k.zsh"
