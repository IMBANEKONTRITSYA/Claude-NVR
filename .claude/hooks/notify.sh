#!/usr/bin/env bash
# Notification: системное уведомление, когда Claude ждёт ввода.
# Ошибки не критичны — хук никогда не должен мешать работе.

msg="Claude Code ждёт вашего ввода (FaceWatch)"

if command -v notify-send >/dev/null 2>&1; then
  notify-send "Claude Code" "$msg" --icon=dialog-information 2>/dev/null
elif command -v osascript >/dev/null 2>&1; then
  osascript -e "display notification \"$msg\" with title \"Claude Code\"" 2>/dev/null
elif command -v powershell.exe >/dev/null 2>&1; then
  # Windows (Git Bash / WSL): всплывающая подсказка в трее, без сторонних модулей.
  powershell.exe -NoProfile -WindowStyle Hidden -Command "
    Add-Type -AssemblyName System.Windows.Forms
    \$n = New-Object System.Windows.Forms.NotifyIcon
    \$n.Icon = [System.Drawing.SystemIcons]::Information
    \$n.Visible = \$true
    \$n.ShowBalloonTip(5000, 'Claude Code', '$msg', 'Info')
    Start-Sleep -Seconds 6
    \$n.Dispose()" >/dev/null 2>&1 &
fi

exit 0
