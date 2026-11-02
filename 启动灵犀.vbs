' 双击它启动灵犀 —— 全程不弹黑框（2026-11-02 项目主要求）
'
' 以前双击 start.bat：先弹启动黑框，再弹“服务窗口”黑框，那个一误关服务就没了。
' 现在改成隐藏执行：没有命令行窗口，服务在后台跑，关不掉（要停请双击 stop.bat）。
' 只有失败时才弹提示框。建议发到桌面当快捷方式，改名「灵犀」。
Option Explicit
Dim fso, sh, here, bat, rc, q
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)
bat = here & "\start.bat"
If Not fso.FileExists(bat) Then
  MsgBox "找不到 start.bat：" & vbCrLf & bat, 16, "宇之语·灵犀"
  WScript.Quit 1
End If
sh.Environment("PROCESS")("EG2_HIDDEN") = "1"
q = Chr(34)
' 3 秒后自动消失的提示（模型加载 30-60 秒，免得以为没反应）
sh.Popup "正在启动灵犀…" & vbCrLf & "模型加载约 30-60 秒，完成后会自动打开界面", 3, "宇之语·灵犀", 64
' 0 = 隐藏窗口；True = 等它跑完，好拿退出码
rc = sh.Run("cmd /c " & q & q & bat & q & q, 0, True)
If rc <> 0 Then
  MsgBox "灵犀没启动成功（退出码 " & rc & "）。" & vbCrLf & vbCrLf & _
         "看日志：logs\server.log 最后 30 行" & vbCrLf & _
         "想看弹窗提示：直接双击 start.bat 再试一次", 48, "宇之语·灵犀"
End If
