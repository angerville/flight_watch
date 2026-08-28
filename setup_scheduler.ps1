#requires -Version 5.1
<#
    flight_watch 작업 스케줄러 등록 스크립트.

    이 프로젝트 폴더는 OneDrive로 두 대의 PC에 동기화되지만, Windows 작업
    스케줄러의 등록 내용은 PC마다 따로 저장된다(동기화 안 됨). 그래서 번갈아
    쓰는 PC "각각에서" 이 스크립트를 한 번씩 실행해야 양쪽 모두 자동 실행된다.

    등록되는 작업 (폴더: \FlightWatch\):
      * track_prices  - run_flight_watch.bat   : 매시간   (Playwright 가격 조회)
      * bot_commands   - run_bot_commands.bat   : 10분마다 (텔레그램 명령어 롱폴링,
                                                  1회 실행이 570초간 상주)

    실행 계정 / 조건:
      * 로그인한 현재 사용자로, "로그인했을 때만" 실행 (암호 저장 불필요).
        -> 그 PC를 실제로 쓰는 동안에만 돈다. PC가 꺼져 있는 구간은
           GitHub Actions 워크플로(6시간 간격)가 메꾼다 - 의도된 분담이다.
      * MultipleInstances=IgnoreNew : 이전 실행이 안 끝났으면 새 실행은 건너뜀
        (lockfile.py 의 스크립트 레벨 락과 이중 방어).
      * StartWhenAvailable : PC가 꺼져 있어 놓친 실행은 켜지면 곧 한 번 따라 실행.
      * 노트북 배터리에서도 실행/유지.

    사용법 (해당 PC에서):
        powershell -ExecutionPolicy Bypass -File .\setup_scheduler.ps1
    또는 setup_scheduler.bat 더블클릭.

    제거:
        powershell -ExecutionPolicy Bypass -File .\setup_scheduler.ps1 -Remove
#>
[CmdletBinding()]
param(
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$folder = '\FlightWatch\'

$tasks = @(
    [pscustomobject]@{
        Name     = 'track_prices'
        Bat      = Join-Path $here 'run_flight_watch.bat'
        # 매시간 09분에 시작해 1시간 간격 무한 반복. 정각(00분)은 전 세계 cron이
        # 몰리는 시간이라 일부러 피함(워크플로 주석과 동일한 이유).
        Interval = (New-TimeSpan -Hours 1)
        StartAt  = (Get-Date).Date.AddMinutes(9)
        # track_prices 1회가 20~30분 걸릴 수 있어 넉넉히 잡음.
        Limit    = (New-TimeSpan -Hours 2)
    }
    [pscustomobject]@{
        Name     = 'bot_commands'
        Bat      = Join-Path $here 'run_bot_commands.bat'
        Interval = (New-TimeSpan -Minutes 10)
        StartAt  = (Get-Date).Date.AddMinutes(3)
        # 1회 실행이 570초 후 스스로 종료. 그보다 짧으면 강제 종료돼 버림.
        Limit    = (New-TimeSpan -Minutes 20)
    }
)

function Remove-FlightWatchTask {
    param([string]$Name)
    $existing = Get-ScheduledTask -TaskPath $folder -TaskName $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskPath $folder -TaskName $Name -Confirm:$false
        Write-Host "  제거됨: $folder$Name"
    }
}

if ($Remove) {
    Write-Host "flight_watch 작업 제거 중..."
    foreach ($t in $tasks) { Remove-FlightWatchTask -Name $t.Name }
    Write-Host "완료."
    return
}

# 이 PC에 배치 파일이 실제로 있는지(=OneDrive 동기화가 끝났는지) 먼저 확인.
foreach ($t in $tasks) {
    if (-not (Test-Path -LiteralPath $t.Bat)) {
        throw "배치 파일을 찾을 수 없습니다: $($t.Bat)`nOneDrive 동기화가 끝났는지 확인하세요."
    }
}

$userId = "$env:USERDOMAIN\$env:USERNAME"
Write-Host "실행 계정: $userId  (로그인 상태에서만 실행)"
Write-Host "프로젝트 경로: $here"
Write-Host ""

foreach ($t in $tasks) {
    Remove-FlightWatchTask -Name $t.Name

    $action = New-ScheduledTaskAction -Execute $t.Bat -WorkingDirectory $here

    # -Once + RepetitionInterval 로 무한 반복 트리거를 만든다. PowerShell 5.1의
    # New-ScheduledTaskTrigger 는 -RepetitionInterval 파라미터를 직접 노출하지
    # 않는 빌드가 있어, 별도 트리거에서 Repetition 객체만 떼어다 붙이는
    # 널리 쓰이는 우회법을 사용한다.
    $trigger = New-ScheduledTaskTrigger -Once -At $t.StartAt
    $rep = (New-ScheduledTaskTrigger -Once -At $t.StartAt `
                -RepetitionInterval $t.Interval `
                -RepetitionDuration ([TimeSpan]::FromDays(3650))).Repetition
    $trigger.Repetition = $rep

    $settings = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -StartWhenAvailable `
        -DontStopIfGoingOnBatteries `
        -AllowStartIfOnBatteries `
        -ExecutionTimeLimit $t.Limit `
        -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 1)

    $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited

    Register-ScheduledTask -TaskPath $folder -TaskName $t.Name `
        -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
        -Description "flight_watch: $($t.Bat) ($([int]$t.Interval.TotalMinutes)분 간격)" | Out-Null

    Write-Host ("  등록됨: {0}{1}  -  {2}, {3}분 간격, 시작 {4:HH:mm}" -f `
        $folder, $t.Name, (Split-Path $t.Bat -Leaf), [int]$t.Interval.TotalMinutes, $t.StartAt)
}

Write-Host ""
Write-Host "등록된 작업 확인:"
Get-ScheduledTask -TaskPath $folder | Select-Object TaskName, State | Format-Table -AutoSize

Write-Host "지금 바로 한 번 돌려보려면:"
Write-Host "  Start-ScheduledTask -TaskPath '$folder' -TaskName 'track_prices'"
Write-Host "  Start-ScheduledTask -TaskPath '$folder' -TaskName 'bot_commands'"
Write-Host ""
Write-Host "로그: task_run.log (가격 조회) / bot_run.log (명령어)"
