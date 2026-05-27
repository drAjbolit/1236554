; AI Chatter AHK Send Fallback — AutoHotkey v2
; Usage:
;   AutoHotkey64.exe ai_chatter_ahk_send.ahk enter
;   AutoHotkey64.exe ai_chatter_ahk_send.ahk clickxy 1234 567
;   AutoHotkey64.exe ai_chatter_ahk_send.ahk clickrel 1200 840
;   AutoHotkey64.exe ai_chatter_ahk_send.ahk clickratio 0.95 0.91
;   AutoHotkey64.exe ai_chatter_ahk_send.ahk click

#Requires AutoHotkey v2.0
#SingleInstance Force

action := "enter"
if A_Args.Length >= 1 {
    action := A_Args[1]
}

CoordMode "Mouse", "Screen"

if WinExist("ahk_exe chrome.exe") {
    WinActivate("ahk_exe chrome.exe")
    WinWaitActive("ahk_exe chrome.exe", , 2)
}

Sleep 250

if (action = "clickxy") {
    if A_Args.Length < 3 {
        ExitApp 2
    }
    x := Integer(A_Args[2])
    y := Integer(A_Args[3])
    MouseMove x, y, 0
    Sleep 100
    Click x, y
} else if (action = "clickrel") {
    if A_Args.Length < 3 {
        ExitApp 2
    }
    relX := Integer(A_Args[2])
    relY := Integer(A_Args[3])
    WinGetPos(&x, &y, &w, &h, "A")
    sx := x + relX
    sy := y + relY
    MouseMove sx, sy, 0
    Sleep 100
    Click sx, sy
} else if (action = "clickratio") {
    if A_Args.Length < 3 {
        ExitApp 2
    }
    rx := Number(A_Args[2])
    ry := Number(A_Args[3])
    WinGetPos(&x, &y, &w, &h, "A")
    sx := x + Round(w * rx)
    sy := y + Round(h * ry)
    MouseMove sx, sy, 0
    Sleep 100
    Click sx, sy
} else if (action = "click") {
    WinGetPos(&x, &y, &w, &h, "A")
    Click x + Round(w * 0.95), y + Round(h * 0.90)
} else {
    Send "{Enter}"
}

Sleep 300
ExitApp 0
