param(
  [string]$ExpectedTitle,
  [string]$ExpectedWslDistro,
  [string]$MessageFile,
  [string]$ExpectedMessageSha256,
  [switch]$ProbeOnly,
  [long]$UiDeadlineUnixMs,
  [long]$UiBudgetMilliseconds
)

# Dot-sourcing defines the same pure guard functions used by the live route.
# It does not enumerate windows, load UIAutomation, or acquire the desktop lock.
function Stop-CodexUi([string]$Code) {
  throw ("CODEX_UI:" + $Code)
}

function Test-CodexUiDeadline([long]$NowUnixMs, [long]$DeadlineUnixMs,
    [double]$ElapsedMs, [double]$BudgetMs) {
  return ($NowUnixMs -lt $DeadlineUnixMs -and $ElapsedMs -ge 0 -and
    $BudgetMs -gt 0 -and $ElapsedMs -lt $BudgetMs -and
    -not [double]::IsInfinity($BudgetMs))
}

function Assert-CodexUiDeadline {
  if ($null -eq $script:CodexUiTimer -or -not (Test-CodexUiDeadline `
      ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) $script:CodexUiDeadlineUnixMs `
      $script:CodexUiTimer.Elapsed.TotalMilliseconds $script:CodexUiBudgetMilliseconds)) {
    Stop-CodexUi 'UI_DEADLINE_EXCEEDED'
  }
}

function Invoke-CodexUiAction([scriptblock]$Action) {
  # Fetch UIA patterns before entering this guard: a late RPC must not be
  # followed by a fresh action after the deadline. Never retry an action.
  Assert-CodexUiDeadline
  & $Action
  Assert-CodexUiDeadline
}

function Select-CodexWindow($Inventory, [string]$Distro) {
  $unknown = @($Inventory | Where-Object { -not $_.ContextKnown })
  if ($unknown.Count -gt 0) { Stop-CodexUi 'WINDOW_CONTEXT_UNKNOWN' }
  $eligible = @($Inventory | Where-Object {
    $_.ContextKind -ceq 'wsl' -and $_.Distro -ceq $Distro
  })
  if ($eligible.Count -eq 0) { Stop-CodexUi 'WSL_WINDOW_NOT_FOUND' }
  $visible = @($eligible | Where-Object { $_.TargetVisible })
  if ($visible.Count -gt 1) { Stop-CodexUi 'TARGET_WINDOW_AMBIGUOUS' }
  if ($visible.Count -eq 1) { return $visible[0] }
  if ($eligible.Count -ne 1) { Stop-CodexUi 'WSL_WINDOW_AMBIGUOUS' }
  return $eligible[0]
}

function Get-CodexWindowSummary($Inventory, [string]$Distro) {
  # Bounded diagnostics never include window/chat titles or filesystem paths.
  return @($Inventory | Where-Object { $null -ne $_ } | Select-Object -First 8 |
    ForEach-Object { [pscustomobject]@{
      window_handle = $_.Handle
      process_id = $_.ProcessId
      context_known = $_.ContextKnown
      context_kind = $_.ContextKind
      expected_context = ($_.ContextKind -ceq 'wsl' -and $_.Distro -ceq $Distro)
      target_visible = $_.TargetVisible
    } })
}

function Test-EmptyCodexComposer([AllowNull()][string]$Value, [bool]$SendEnabled) {
  # A real draft equal to the placeholder enables Send and must be preserved.
  # Enabled Send with empty text may represent attachments; preserve it too.
  if ($SendEnabled) { return $false }
  if ($null -eq $Value -or $Value.Length -eq 0) { return $true }
  $label = 'Ask for follow-up changes'
  return ($Value -ceq $label -or $Value -ceq ($label + [char]10) -or
    $Value -ceq ($label + [char]13 + [char]10))
}

function Test-CodexDraft([AllowNull()][string]$Value, [string]$Expected) {
  if ($null -eq $Value) { return $false }
  $lf = [string][char]10
  $crlf = [string][char]13 + [char]10
  $actual = $Value.Replace($crlf, $lf)
  $wanted = $Expected.Replace($crlf, $lf)
  # UIA may add a contenteditable paragraph terminator or omit one supplied
  # terminal LF. Accept only that single boundary difference; never Trim().
  # Post-submit IPC correlation still requires the full original user text.
  return ($actual -ceq $wanted -or $actual -ceq ($wanted + $lf) -or
    ($wanted.EndsWith($lf, [System.StringComparison]::Ordinal) -and
      $actual -ceq $wanted.Substring(0, $wanted.Length - 1)))
}

function Find-CodexElements($Root, [string]$Name, $Type) {
  $named = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::NameProperty, $Name
  )
  $typed = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty, $Type
  )
  $condition = New-Object System.Windows.Automation.AndCondition($named, $typed)
  return @($Root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition) |
    Where-Object { $_.Current.Name -ceq $Name })
}

function Get-CodexTargetDocument($Root, [string]$Title) {
  $docs = @(Find-CodexElements $Root $Title ([System.Windows.Automation.ControlType]::Document) |
    Where-Object { -not $_.Current.IsOffscreen })
  if ($docs.Count -eq 1) { return $docs[0] }
  return $null
}

function Get-CodexWindowInventory([string]$Title) {
  Assert-CodexUiDeadline
  $ids = @(Get-Process -Name Code -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
  $roots = [System.Windows.Automation.AutomationElement]::RootElement.FindAll(
    [System.Windows.Automation.TreeScope]::Children,
    [System.Windows.Automation.Condition]::TrueCondition
  )
  $result = @()
  foreach ($item in $roots) {
    Assert-CodexUiDeadline
    try { $current = $item.Current } catch { continue }
    if ($ids -notcontains $current.ProcessId -or
        $current.ControlType -ne [System.Windows.Automation.ControlType]::Window -or
        $current.NativeWindowHandle -eq 0) { continue }
    $known = $false
    $kind = $null
    $distro = $null
    try {
      $condition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'status.host'
      )
      $hosts = @($item.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition))
      if ($hosts.Count -eq 1) {
        $name = $hosts[0].Current.Name
        if ($name -cmatch '^remote WSL: (.+)$') {
          $kind = 'wsl'
          $distro = $Matches[1]
          $known = $true
        } elseif ($name -ceq 'remote') {
          $kind = 'local'
          $known = $true
        } elseif ($name -cmatch '^remote ') {
          $kind = 'other_remote'
          $known = $true
        }
      }
      $target = Get-CodexTargetDocument $item $Title
    } catch {
      $known = $false
      $target = $null
    }
    $result += [pscustomobject]@{
      Handle = [long]$current.NativeWindowHandle
      ProcessId = $current.ProcessId
      ContextKnown = $known
      ContextKind = $kind
      Distro = $distro
      TargetVisible = ($null -ne $target)
    }
  }
  $script:CodexUiLastInventory = $result
  Assert-CodexUiDeadline
  return $result
}

function Get-CodexRoot([long]$Handle) {
  $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$Handle)
  if ($null -eq $root) { Stop-CodexUi 'WINDOW_UNAVAILABLE' }
  return $root
}

function Assert-CodexWindow([long]$Handle, [string]$Distro, [string]$Title) {
  $inventory = @(Get-CodexWindowInventory $Title)
  $selected = Select-CodexWindow $inventory $Distro
  if ($selected.Handle -ne $Handle) { Stop-CodexUi 'TARGET_WINDOW_CHANGED' }
  return (Get-CodexRoot $Handle)
}

function Get-CodexComposer($Root) {
  $edit = @(Find-CodexElements $Root 'Ask for follow-up changes' ([System.Windows.Automation.ControlType]::Edit) |
    Where-Object { -not $_.Current.IsOffscreen })
  $send = @(Find-CodexElements $Root 'Send' ([System.Windows.Automation.ControlType]::Button) |
    Where-Object { -not $_.Current.IsOffscreen })
  if ($edit.Count -ne 1 -or $send.Count -ne 1 -or -not $edit[0].Current.IsEnabled) {
    Stop-CodexUi 'COMPOSER_NOT_UNIQUE_OR_AVAILABLE'
  }
  $stop = @(Find-CodexElements $Root 'Stop' ([System.Windows.Automation.ControlType]::Button) |
    Where-Object { -not $_.Current.IsOffscreen -and $_.Current.IsEnabled })
  if ($stop.Count -gt 0) { Stop-CodexUi 'THREAD_UI_BUSY' }
  $pattern = $edit[0].GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
  return [pscustomobject]@{ Edit = $edit[0]; Send = $send[0]; Pattern = $pattern; Value = $pattern.Current.Value }
}

function Find-CodexChatEntries($Root, [string]$Title) {
  $condition = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::NameProperty, $Title
  )
  return @($Root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition) |
    Where-Object {
      $_.Current.Name -ceq $Title -and $_.Current.ControlType -in @(
        [System.Windows.Automation.ControlType]::Button,
        [System.Windows.Automation.ControlType]::MenuItem,
        [System.Windows.Automation.ControlType]::ListItem,
        [System.Windows.Automation.ControlType]::Hyperlink
      )
    })
}

function Search-ExistingCodexChatEntry([long]$Handle, [string]$Title, [string]$Distro) {
  $root = Assert-CodexWindow $Handle $Distro $Title
  $buttonType = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Button
  )
  $recent = @($root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $buttonType) |
    Where-Object { -not $_.Current.IsOffscreen -and $_.Current.IsEnabled -and $_.Current.Name -cmatch '^Recent chats[.]' })
  if ($recent.Count -ne 1) { Stop-CodexUi 'RECENT_CHATS_NOT_UNIQUE' }
  $recentPattern = $recent[0].GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
  Invoke-CodexUiAction { $recentPattern.Expand() }
  $searchCondition = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'recent-tasks-search'
  )
  $search = @()
  for ($i = 0; $i -lt 10; $i++) {
    Start-Sleep -Milliseconds 100
    $root = Assert-CodexWindow $Handle $Distro $Title
    $search = @($root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $searchCondition) |
      Where-Object { -not $_.Current.IsOffscreen -and $_.Current.ControlType -eq [System.Windows.Automation.ControlType]::Edit })
    if ($search.Count -eq 1) { break }
    if ($search.Count -gt 1) { Stop-CodexUi 'RECENT_SEARCH_AMBIGUOUS' }
  }
  if ($search.Count -ne 1) { Stop-CodexUi 'RECENT_SEARCH_UNAVAILABLE' }
  $searchPattern = $search[0].GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
  Invoke-CodexUiAction { $searchPattern.SetValue($Title) }
  $menuType = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Menu
  )
  for ($i = 0; $i -lt 12; $i++) {
    Start-Sleep -Milliseconds 100
    $root = Assert-CodexWindow $Handle $Distro $Title
    $search = @($root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $searchCondition) |
      Where-Object { -not $_.Current.IsOffscreen })
    if ($search.Count -ne 1 -or
        $search[0].GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value -cne $Title) {
      Stop-CodexUi 'RECENT_SEARCH_CHANGED'
    }
    $menus = @($root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $menuType) |
      Where-Object { -not $_.Current.IsOffscreen -and $_.Current.Name -cmatch '^Recent chats[.]' })
    if ($menus.Count -gt 1) { Stop-CodexUi 'RECENT_MENU_AMBIGUOUS' }
    if ($menus.Count -eq 1) {
      $entries = @(Find-CodexChatEntries $menus[0] $Title)
      if ($entries.Count -gt 1) { Stop-CodexUi 'CHAT_TITLE_AMBIGUOUS' }
      if ($entries.Count -eq 1 -and -not $entries[0].Current.IsOffscreen -and $entries[0].Current.IsEnabled) {
        return $entries[0]
      }
    }
  }
  Stop-CodexUi 'TARGET_CHAT_NOT_VISIBLE'
}

function Open-ExistingCodexChat([long]$Handle, [string]$Title, [string]$Distro) {
  $root = Assert-CodexWindow $Handle $Distro $Title
  if ($null -ne (Get-CodexTargetDocument $root $Title)) { return }
  # Do not navigate away from an existing real draft.
  $edits = @(Find-CodexElements $root 'Ask for follow-up changes' ([System.Windows.Automation.ControlType]::Edit) |
    Where-Object { -not $_.Current.IsOffscreen })
  if ($edits.Count -gt 1) { Stop-CodexUi 'COMPOSER_NOT_UNIQUE_OR_AVAILABLE' }
  if ($edits.Count -eq 1) {
    $value = $edits[0].GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value
    $buttons = @(Find-CodexElements $root 'Send' ([System.Windows.Automation.ControlType]::Button) |
      Where-Object { -not $_.Current.IsOffscreen })
    $enabled = ($buttons.Count -eq 1 -and $buttons[0].Current.IsEnabled)
    if (-not (Test-EmptyCodexComposer $value $enabled)) { Stop-CodexUi 'OTHER_CHAT_DRAFT_PRESENT' }
  }
  $back = @(Find-CodexElements $root 'Back' ([System.Windows.Automation.ControlType]::Button) |
    Where-Object { -not $_.Current.IsOffscreen -and $_.Current.IsEnabled })
  if ($back.Count -eq 1) {
    $backPattern = $back[0].GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
    Invoke-CodexUiAction { $backPattern.Invoke() }
  } elseif ($back.Count -gt 1) {
    Stop-CodexUi 'CHAT_NAVIGATION_AMBIGUOUS'
  }
  $buttons = @()
  for ($i = 0; $i -lt 12; $i++) {
    Start-Sleep -Milliseconds 150
    $root = Assert-CodexWindow $Handle $Distro $Title
    $chatList = @(Find-CodexElements $root 'Chats' ([System.Windows.Automation.ControlType]::Text) |
      Where-Object { -not $_.Current.IsOffscreen })
    if ($chatList.Count -eq 0) { continue }
    $buttons = @(Find-CodexChatEntries $root $Title)
    if ($buttons.Count -eq 1) {
      if ($buttons[0].Current.IsOffscreen) {
        $scroll = $null
        if ($buttons[0].TryGetCurrentPattern([System.Windows.Automation.ScrollItemPattern]::Pattern, [ref]$scroll)) {
          Invoke-CodexUiAction { $scroll.ScrollIntoView() }
        }
      }
      if (-not $buttons[0].Current.IsOffscreen -and $buttons[0].Current.IsEnabled) { break }
    }
    if ($buttons.Count -gt 1) { Stop-CodexUi 'CHAT_TITLE_AMBIGUOUS' }
  }
  if ($buttons.Count -ne 1 -or $buttons[0].Current.IsOffscreen -or -not $buttons[0].Current.IsEnabled) {
    $buttons = @((Search-ExistingCodexChatEntry $Handle $Title $Distro))
  }
  $invoke = $null
  $selection = $null
  if ($buttons[0].TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$invoke)) {
    Invoke-CodexUiAction { $invoke.Invoke() }
  } elseif ($buttons[0].TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$selection)) {
    Invoke-CodexUiAction { $selection.Select() }
  } else {
    Stop-CodexUi 'CHAT_ENTRY_ACTION_UNAVAILABLE'
  }
  for ($i = 0; $i -lt 12; $i++) {
    Start-Sleep -Milliseconds 150
    $root = Assert-CodexWindow $Handle $Distro $Title
    if ($null -ne (Get-CodexTargetDocument $root $Title)) { return }
  }
  Stop-CodexUi 'TARGET_CHAT_NOT_OPENED'
}

function Invoke-CodexUiMain {
  $ErrorActionPreference = 'Stop'
  [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
  $mutex = $null
  $ownsMutex = $false
  $staged = $false
  $submitAttempted = $false
  $lastPhase = 'preflight'
  $hash = $null
  $script:CodexUiLastInventory = @()
  $script:CodexUiTimer = [System.Diagnostics.Stopwatch]::StartNew()
  $deadlineActive = $false
  try {
    if ([string]::IsNullOrWhiteSpace($ExpectedTitle) -or
        [string]::IsNullOrWhiteSpace($ExpectedWslDistro)) { Stop-CodexUi 'TARGET_CONTEXT_REQUIRED' }
    if ($UiDeadlineUnixMs -le 0 -or $UiBudgetMilliseconds -lt 0 -or
        $UiBudgetMilliseconds -gt 81000) { Stop-CodexUi 'UI_DEADLINE_REQUIRED' }
    $script:CodexUiDeadlineUnixMs = $UiDeadlineUnixMs
    # Absolute UTC includes PowerShell startup. The monotonic limit additionally
    # prevents a backwards clock change from extending this operation.
    $script:CodexUiBudgetMilliseconds = [Math]::Min([double]$UiBudgetMilliseconds,
      [double]($UiDeadlineUnixMs - [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()))
    $deadlineActive = $true
    Assert-CodexUiDeadline
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $mutex = New-Object System.Threading.Mutex($false, ('Local\CodexConnect.UiInput.' + $sid))
    try { $ownsMutex = $mutex.WaitOne(0) }
    catch [System.Threading.AbandonedMutexException] {
      $ownsMutex = $true
      Stop-CodexUi 'PREVIOUS_UI_OWNER_ABANDONED'
    }
    if (-not $ownsMutex) { Stop-CodexUi 'OTHER_UI_SENDER_ACTIVE' }
    Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class CodexConnectUiHost {
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr handle);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr handle, int command);
}
'@
    $inventory = @(Get-CodexWindowInventory $ExpectedTitle)
    $selected = Select-CodexWindow $inventory $ExpectedWslDistro
    $handle = $selected.Handle
    if ([CodexConnectUiHost]::IsIconic([IntPtr]$handle)) {
      Invoke-CodexUiAction { [CodexConnectUiHost]::ShowWindow([IntPtr]$handle, 9) | Out-Null }
      Start-Sleep -Milliseconds 300
    }
    Open-ExistingCodexChat $handle $ExpectedTitle $ExpectedWslDistro
    $root = Assert-CodexWindow $handle $ExpectedWslDistro $ExpectedTitle
    $document = Get-CodexTargetDocument $root $ExpectedTitle
    if ($null -eq $document) { Stop-CodexUi 'TARGET_CHANGED_BEFORE_STAGING' }
    $composer = Get-CodexComposer $document
    if (-not (Test-EmptyCodexComposer $composer.Value $composer.Send.Current.IsEnabled)) {
      Stop-CodexUi 'USER_DRAFT_PRESENT'
    }
    Assert-CodexUiDeadline
    $lastPhase = 'ui_ready'
    if ($ProbeOnly) {
      return @{ ExitCode = 0; Result = @{
        phase = 'ui_ready'; outcome = 'not_sent'; submit_attempted = $false;
        last_confirmed_phase = $lastPhase; target_confirmed = $true;
        draft_preserved = $true; retry_safe = $false; window_handle = $handle
      } }
    }
    if (-not (Test-Path -LiteralPath $MessageFile -PathType Leaf)) { Stop-CodexUi 'MESSAGE_FILE_NOT_FOUND' }
    $raw = [System.IO.File]::ReadAllBytes($MessageFile)
    if ($raw.Length -eq 0 -or $raw.Length -gt 32768) { Stop-CodexUi 'MESSAGE_SIZE_INVALID' }
    $utf8 = New-Object System.Text.UTF8Encoding($false, $true)
    $message = $utf8.GetString($raw)
    if ([string]::IsNullOrWhiteSpace($message) -or $message.IndexOf([char]0) -ge 0) { Stop-CodexUi 'MESSAGE_EMPTY_OR_NUL' }
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { $hash = [BitConverter]::ToString($sha.ComputeHash($raw)).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
    if ($ExpectedMessageSha256 -cnotmatch '^[a-f0-9]{64}$' -or $hash -cne $ExpectedMessageSha256) {
      Stop-CodexUi 'MESSAGE_HASH_MISMATCH'
    }
    $root = Assert-CodexWindow $handle $ExpectedWslDistro $ExpectedTitle
    $document = Get-CodexTargetDocument $root $ExpectedTitle
    if ($null -eq $document) { Stop-CodexUi 'TARGET_CHANGED_BEFORE_STAGING' }
    $composer = Get-CodexComposer $document
    if (-not (Test-EmptyCodexComposer $composer.Value $composer.Send.Current.IsEnabled)) {
      Stop-CodexUi 'USER_DRAFT_PRESENT'
    }
    # Any error from this point may leave a draft. Never erase it automatically.
    $staged = $true
    $lastPhase = 'staging_started'
    Invoke-CodexUiAction { $composer.Pattern.SetValue($message) }
    $ready = $false
    for ($i = 0; $i -lt 12; $i++) {
      Start-Sleep -Milliseconds 100
      $root = Assert-CodexWindow $handle $ExpectedWslDistro $ExpectedTitle
      $document = Get-CodexTargetDocument $root $ExpectedTitle
      if ($null -eq $document) { Stop-CodexUi 'TARGET_CHANGED_AFTER_STAGING' }
      $composer = Get-CodexComposer $document
      if ((Test-CodexDraft $composer.Value $message) -and $composer.Send.Current.IsEnabled) {
        $ready = $true
        $lastPhase = 'staged'
        break
      }
    }
    if (-not $ready) { Stop-CodexUi 'SEND_BUTTON_NOT_READY' }
    # Fresh objects, target, context and exact text immediately before one click.
    $root = Assert-CodexWindow $handle $ExpectedWslDistro $ExpectedTitle
    $document = Get-CodexTargetDocument $root $ExpectedTitle
    if ($null -eq $document) { Stop-CodexUi 'TARGET_CHANGED_BEFORE_SUBMIT' }
    $composer = Get-CodexComposer $document
    if (-not (Test-CodexDraft $composer.Value $message) -or -not $composer.Send.Current.IsEnabled) {
      Stop-CodexUi 'DRAFT_CHANGED_BEFORE_SUBMIT'
    }
    $sendPattern = $composer.Send.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
    Assert-CodexUiDeadline
    $submitAttempted = $true
    $lastPhase = 'submit_started'
    $submitStartedAt = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    Invoke-CodexUiAction { $sendPattern.Invoke() }
    $lastPhase = 'ui_invoked'
    return @{ ExitCode = 0; Result = @{
      phase = 'ui_invoked'; outcome = 'unknown'; submit_attempted = $true;
      last_confirmed_phase = $lastPhase; submit_started_at_ms = $submitStartedAt;
      message_sha256 = $hash; target_confirmed = $true; retry_safe = $false
    } }
  } catch {
    $reason = 'UI_AUTOMATION_ERROR'
    if ($_.Exception.Message -cmatch '^CODEX_UI:([A-Z0-9_]+)$') { $reason = $Matches[1] }
    if ($deadlineActive -and -not (Test-CodexUiDeadline `
        ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) $script:CodexUiDeadlineUnixMs `
        $script:CodexUiTimer.Elapsed.TotalMilliseconds $script:CodexUiBudgetMilliseconds)) {
      $reason = 'UI_DEADLINE_EXCEEDED'
    }
    $phase = 'failed_before_send'
    $outcome = 'not_sent'
    if ($staged) { $phase = 'staged_not_submitted' }
    if ($submitAttempted) { $phase = 'outcome_unknown'; $outcome = 'unknown' }
    $timedOut = $reason -ceq 'UI_DEADLINE_EXCEEDED'
    if ($timedOut) {
      $phase = 'outcome_unknown'; $outcome = 'unknown'; $submitAttempted = $null
    }
    return @{ ExitCode = 3; Result = @{
      phase = $phase; outcome = $outcome; submit_attempted = $submitAttempted;
      last_confirmed_phase = $lastPhase; reason = $reason; error_code = $reason;
      inner_ui_timed_out = $timedOut;
      message_sha256 = $hash; draft_preserved = $true; retry_safe = $false;
      window_candidate_count = @($script:CodexUiLastInventory).Count;
      window_candidates = @(Get-CodexWindowSummary $script:CodexUiLastInventory $ExpectedWslDistro)
    } }
  } finally {
    if ($null -ne $mutex) {
      if ($ownsMutex) { $mutex.ReleaseMutex() }
      $mutex.Dispose()
    }
  }
}

if ($MyInvocation.InvocationName -ne '.') {
  $execution = Invoke-CodexUiMain
  [Console]::WriteLine(($execution.Result | ConvertTo-Json -Depth 5 -Compress))
  exit $execution.ExitCode
}
