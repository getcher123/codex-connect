# Synthetic guard fixtures only. No process/window enumeration or UI calls.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '../skills/codex-connect/scripts/send_ui.ps1')
$script:FixtureChecks = 0

function Assert-True($Value, [string]$Label) {
  if (-not $Value) { throw ("Fixture failed: " + $Label) }
  $script:FixtureChecks++
}
function Assert-Code([scriptblock]$Action, [string]$Expected) {
  $caught = $false
  try { & $Action | Out-Null }
  catch {
    $caught = $true
    if ($_.Exception.Message -cne ('CODEX_UI:' + $Expected)) { throw }
  }
  if (-not $caught) { throw ("Expected guard: " + $Expected) }
  $script:FixtureChecks++
}
function Window([long]$Handle, [string]$Kind, [string]$Distro, [bool]$Visible) {
  return [pscustomobject]@{
    Handle = $Handle; ProcessId = 123; ContextKnown = $true;
    ContextKind = $Kind; Distro = $Distro; TargetVisible = $Visible
  }
}
$local = Window 101 'local' '' $true
$wsl = Window 102 'wsl' 'SyntheticDistro' $false
Assert-True ((Select-CodexWindow @($local, $wsl) 'SyntheticDistro').Handle -eq 102) 'same PID, different context'
$visible = Window 103 'wsl' 'SyntheticDistro' $true
Assert-True ((Select-CodexWindow @($wsl, $visible) 'SyntheticDistro').Handle -eq 103) 'unique visible exact target'
Assert-Code { Select-CodexWindow @($wsl, (Window 104 'wsl' 'SyntheticDistro' $false)) 'SyntheticDistro' } 'WSL_WINDOW_AMBIGUOUS'
Assert-Code { Select-CodexWindow @($visible, (Window 104 'wsl' 'SyntheticDistro' $true)) 'SyntheticDistro' } 'TARGET_WINDOW_AMBIGUOUS'
Assert-Code { Select-CodexWindow @($local) 'SyntheticDistro' } 'WSL_WINDOW_NOT_FOUND'
Assert-Code { Select-CodexWindow @($wsl) 'syntheticdistro' } 'WSL_WINDOW_NOT_FOUND'
$unknown = Window 105 'wsl' 'SyntheticDistro' $false
$unknown.ContextKnown = $false
Assert-Code { Select-CodexWindow @($wsl, $unknown) 'SyntheticDistro' } 'WINDOW_CONTEXT_UNKNOWN'
$manyWindows = @(1..20 | ForEach-Object { Window $_ 'wsl' 'SyntheticDistro' $false })
$summary = @(Get-CodexWindowSummary $manyWindows 'SyntheticDistro')
Assert-True ($summary.Count -eq 8 -and $summary[0].expected_context -eq $true) 'bounded window diagnostics'
Assert-True (Test-EmptyCodexComposer '' $false) 'empty composer'
Assert-True (Test-EmptyCodexComposer ('Ask for follow-up changes' + [char]10) $false) 'placeholder newline'
Assert-True (-not (Test-EmptyCodexComposer 'Ask for follow-up changes' $true)) 'actual placeholder text draft'
Assert-True (-not (Test-EmptyCodexComposer 'unfinished user text' $false)) 'real draft'
Assert-True (-not (Test-EmptyCodexComposer '' $true)) 'attachments or enabled send'
Assert-True (-not (Test-EmptyCodexComposer '  ' $false)) 'whitespace draft retained'
Assert-True (Test-CodexDraft ('Exact text' + [char]10) 'Exact text') 'UIA paragraph terminator'
Assert-True (-not (Test-CodexDraft 'Exact text' ' Exact text ')) 'no trimming'
Assert-True (-not (Test-CodexDraft 'exact text' 'Exact text')) 'case preserved'
Assert-True (-not (Test-CodexDraft ('a' + [char]10 + 'b') ('a\' + [char]10 + 'b'))) 'code backslash preserved'
$lf = [string][char]10
$crlf = [string][char]13 + [char]10
$multiline = 'first line' + $lf + 'second line' + $lf + 'last line'
Assert-True (Test-CodexDraft $multiline ($multiline + $lf)) 'one omitted terminal LF'
Assert-True (Test-CodexDraft ($multiline.Replace($lf, $crlf)) ($multiline + $lf)) 'CRLF normalization with one omitted terminal LF'
Assert-True (Test-CodexDraft ($multiline + $lf) ($multiline + $lf + $lf)) 'one omitted LF preserves the other terminal LF'
Assert-True (-not (Test-CodexDraft $multiline ($multiline + $lf + $lf))) 'two omitted terminal LFs rejected'
Assert-True (-not (Test-CodexDraft ($multiline + $lf + $lf) $multiline)) 'two extra terminal LFs rejected'
Assert-True (Test-CodexDraft '  exact trailing spaces  ' ('  exact trailing spaces  ' + $lf)) 'terminal LF allowance preserves spaces'
Assert-True (-not (Test-CodexDraft 'exact trailing spaces' ('  exact trailing spaces  ' + $lf))) 'terminal LF allowance does not trim spaces'
Assert-True (-not (Test-CodexDraft 'line' ('line' + $lf + ' '))) 'non-LF final character must remain'
Assert-True (-not (Test-CodexDraft ('first linesecond line' + $lf + 'last line') ($multiline + $lf))) 'missing internal LF rejected'
Assert-True (Test-CodexDraft ('a\' + $lf + 'b') ('a\' + $lf + 'b' + $lf)) 'code backslash preserved with omitted terminal LF'
Assert-True (-not (Test-CodexDraft ('a' + $lf + 'b') ('a\' + $lf + 'b' + $lf))) 'terminal LF allowance does not erase code backslash'
Assert-True (Test-CodexUiDeadline 1000 2000 999 1000) 'deadline permits time before both bounds'
Assert-True (-not (Test-CodexUiDeadline 2000 2000 0 1000)) 'absolute deadline includes equality'
Assert-True (-not (Test-CodexUiDeadline 500 2000 1000 1000)) 'monotonic deadline survives backwards UTC jump'
Assert-True (-not (Test-CodexUiDeadline 1000 2000 0 ([double]::PositiveInfinity))) 'unbounded deadline rejected'
$script:CodexUiDeadlineUnixMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() + 60000
$script:CodexUiBudgetMilliseconds = 1000
$script:CodexUiTimer = [pscustomobject]@{ Elapsed = [pscustomobject]@{ TotalMilliseconds = 0 } }
$script:ActionCalls = 0
function Invoke-SyntheticScopedAction {
  $localValue = 'synthetic scoped value'
  Invoke-CodexUiAction { $script:ScopedValue = $localValue; $script:ActionCalls++ }
}
Invoke-SyntheticScopedAction
Assert-True ($script:ScopedValue -ceq 'synthetic scoped value' -and $script:ActionCalls -eq 1) 'guarded action retains caller scope'
# Simulate a pattern lookup returning after the deadline, without any UIA call.
$script:CodexUiTimer.Elapsed.TotalMilliseconds = 1000
Assert-Code { Invoke-CodexUiAction { $script:ActionCalls++ } } 'UI_DEADLINE_EXCEEDED'
Assert-True ($script:ActionCalls -eq 1) 'late RPC cannot be followed by another action'
$script:CodexUiTimer.Elapsed.TotalMilliseconds = 0
Assert-Code { Invoke-CodexUiAction { $script:ActionCalls++; $script:CodexUiTimer.Elapsed.TotalMilliseconds = 1000 } } 'UI_DEADLINE_EXCEEDED'
Assert-True ($script:ActionCalls -eq 2) 'late action result is not retried'
# Main exits at its deadline check, before WindowsIdentity, mutex or UIAutomation.
$ExpectedTitle = 'Synthetic deadline chat'
$ExpectedWslDistro = 'SyntheticDistro'
$UiDeadlineUnixMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() - 1
$UiBudgetMilliseconds = 0
$expired = Invoke-CodexUiMain
Assert-True ($expired.Result.phase -ceq 'outcome_unknown' -and
  $expired.Result.outcome -ceq 'unknown' -and $null -eq $expired.Result.submit_attempted -and
  $expired.Result.error_code -ceq 'UI_DEADLINE_EXCEEDED' -and $expired.Result.inner_ui_timed_out) 'soft deadline stays unknown without claiming Submit'
Write-Output ('Synthetic Windows UI guards: ' + $script:FixtureChecks + ' checks passed; no desktop interaction.')
