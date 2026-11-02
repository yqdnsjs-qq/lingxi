' 隐藏窗口启动灵犀服务（2026-11-02 项目主要求）
' 目的：不再出现那个一误关就完蛋的黑框；进程照常跑，只是窗口不可见
' 由 start.bat 和 run\restart_server.py 调用，一般不用手动双击
Option Explicit
Dim fso, sh, here, bat, q, cmdline
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)
bat = fso.GetParentFolderName(here) & "\app\run_server.bat"
If Not fso.FileExists(bat) Then
  MsgBox "找不到服务脚本：" & vbCrLf & bat, 16, "宇之语·灵犀"
  WScript.Quit 1
End If
' 告诉 run_server.bat：这是隐藏模式（出错也别 pause 等人）
sh.Environment("PROCESS")("EG2_HIDDEN") = "1"
' 必须走 cmd /c 加引号的路径；直接把 .bat 交给 Run 不会执行（2026-11-02 实测）
q = Chr(34)
cmdline = "cmd /c " & q & q & bat & q & q
sh.Run cmdline, 0, False
