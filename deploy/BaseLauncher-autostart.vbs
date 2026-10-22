' BaseLauncher — автозапуск при входе в Windows: сервер + окно стартера 1С.
' Запускает pythonw.exe (без окна консоли), ждёт подъёма сервера и открывает окно Edge --app.
' Останавливать: POST /api/shutdown или закpыть окно сервера в диспетчере задач.
Option Explicit
Dim sh, appDir, py, port
Set sh = CreateObject("WScript.Shell")
appDir = "D:\git_repos\baselauncher"
py = "D:\python\WPy64-31241\python-3.12.4.amd64\pythonw.exe"
port = 8797

sh.CurrentDirectory = appDir

' сервер (окно не показываем). Если уже запущен — второй процесс просто не сможет занять порт.
Dim ready, i, http
ready = False
On Error Resume Next
sh.Run """" & py & """ run_server.py", 0, False
Err.Clear

' ждём готовности: до 40 попыток по 0.5 с, каждый раз новым объектом
For i = 1 To 40
  Set http = Nothing
  On Error Resume Next

  http.setProxy 1, "", ""              ' 1 = SXH_PROXY_SET_DIRECT: локальный адрес без прокси
  http.open "GET", "http://127.0.0.1:" & port & "/api/state", False
  http.send
  If Err.Number = 0 Then
    If http.status = 200 Then ready = True
  End If
  Err.Clear
  On Error GoTo 0
  If ready Then Exit For
  WScript.Sleep 500
Next

' пауза и окно стартера (открываем всегда: если сервер не поднялся, будет
' ERR_CONNECTION_REFUSED — тогда запустить start_bl.bat вручную)
WScript.Sleep 1200
sh.Run "msedge --app=http://127.0.0.1:" & port, 1, False
