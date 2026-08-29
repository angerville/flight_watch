' run_hidden.vbs - 작업 스케줄러가 콘솔 창을 띄우지 않도록 배치 파일을 숨겨서 실행한다.
'
' 작업 스케줄러가 .bat 를 직접 실행하면(LogonType=Interactive) 매 실행마다 빈
' cmd 창이 떴다 사라진다. wscript 로 이 래퍼를 거치면 창 스타일 0(숨김)으로
' 돌아 아무 창도 뜨지 않는다.
'
' 사용법:  wscript.exe run_hidden.vbs "<실행할 .bat 전체 경로>"
'
' Run(cmd, 0, True): 0 = 창 숨김, True = 배치가 끝날 때까지 대기.
' 대기해야 작업 스케줄러가 실행 시간 / MultipleInstances(중복 방지) /
' ExecutionTimeLimit / 종료 코드를 정상적으로 인식한다.
Option Explicit
Dim sh, rc
If WScript.Arguments.Count < 1 Then
    WScript.Quit 2
End If
Set sh = CreateObject("WScript.Shell")
rc = sh.Run("""" & WScript.Arguments(0) & """", 0, True)
WScript.Quit rc
