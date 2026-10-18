#!/bin/sh
# Herdr tab-bar status: CPU and RAM usage.
# Referenced by ui.tab_bar_right in ~/.config/herdr/config.toml.
# Herdr runs this every few seconds and displays the last line of output.
# On failure, empty output, or timeout the tab-bar entry is cleared.

set -u

top_out=$(LC_ALL=C top -l 1 -n 0 2>/dev/null) || exit 0

# CPU: total usage = 100 minus idle, from top's "CPU usage:" summary line.
cpu=$(printf '%s\n' "$top_out" | awk -F',' '/CPU usage/ { split($3, a, "%"); printf "%.0f", 100 - a[1] }')

# RAM: memory_pressure reports system-wide free pages; usage = 100 - free.
free_pct=$(memory_pressure -Q 2>/dev/null | sed -n 's/.*memory free percentage: \([0-9][0-9]*\)%.*/\1/p')

if [ -n "$cpu" ] && [ -n "$free_pct" ]; then
    printf 'CPU %s%% · RAM %s%%\n' "$cpu" "$((100 - free_pct))"
elif [ -n "$cpu" ]; then
    printf 'CPU %s%%\n' "$cpu"
elif [ -n "$free_pct" ]; then
    printf 'RAM %s%%\n' "$((100 - free_pct))"
fi
