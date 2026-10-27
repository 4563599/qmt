param([switch]$VerifyOnly)
$ErrorActionPreference = 'Stop'
$taskName = 'QMT_StrategyConsole_0915'
$root = $PSScriptRoot
$python = 'C:\Program Files\Python311\pythonw.exe'
$script = Join-Path $root 'launcher_gui.py'
if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $script)) {
    throw 'Python or launcher_gui.py is missing.'
}
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and $existing.Actions.Arguments -notlike '*launcher_gui.py*') {
    throw 'The task name is already used by another program.'
}
if (-not $VerifyOnly) {
    $action = New-ScheduledTaskAction -Execute $python -Argument ('-X utf8 "' + $script + '" --scheduled') -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '09:15' -RandomDelay ([TimeSpan]::Zero)
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
    # The GUI may remain open overnight. Its own mutex prevents duplicate scanners.
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances Parallel -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
    $settings.UseUnifiedSchedulingEngine = $false
    $description = 'Open QMT strategy console at 09:15 Asia/Shanghai on SSE trading days. Holidays are excluded by a complete trading calendar. Requires an interactive login; waits for the QMT bridge. Does not start QMT or duplicate a running scanner.'
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description $description -Force | Out-Null
}
$task = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName
[pscustomobject]@{
    TaskName = $task.TaskName
    State = $task.State.ToString()
    NextRunTime = $info.NextRunTime
    LastTaskResult = $info.LastTaskResult
    Execute = $task.Actions.Execute
    Arguments = $task.Actions.Arguments
    WorkingDirectory = $task.Actions.WorkingDirectory
    StartBoundary = $task.Triggers.StartBoundary
    DaysOfWeek = $task.Triggers.DaysOfWeek
    LogonType = $task.Principal.LogonType.ToString()
    RunLevel = $task.Principal.RunLevel.ToString()
}
