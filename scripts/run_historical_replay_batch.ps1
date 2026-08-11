[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string[]]$Dates,

    [string]$Instrument = "NSE:INFY",
    [string]$SnapshotDirectory = "data/reference/instruments/provider=zerodha/date=2026-07-30",
    [string]$Calendar = "config/calendars/nse_equity_2026.yaml",
    [string]$ReplayDatabase = "state/replay/trading.sqlite",
    [string]$OperationalDatabase = "state/trading.sqlite",
    [string]$Confirm = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$requiredConfirmation = "RUN CONTROLLED HISTORICAL REPLAY BATCH"
if ($Confirm -ne $requiredConfirmation) {
    throw "Exact confirmation required: $requiredConfirmation"
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $projectRoot
try {
    if (-not $env:KITE_API_KEY) {
        throw "KITE_API_KEY is not configured in this terminal."
    }
    if (-not $env:KITE_EXPECTED_USER_ID) {
        throw "KITE_EXPECTED_USER_ID is not configured in this terminal."
    }

    $branch = (& git branch --show-current).Trim()
    if ($LASTEXITCODE -ne 0 -or $branch -ne "dev") {
        throw "Run the batch from a clean dev branch; current branch is '$branch'."
    }
    $changes = @(& git status --porcelain --untracked-files=no)
    if ($LASTEXITCODE -ne 0 -or $changes.Count -ne 0) {
        throw "Run the batch from a clean tracked worktree."
    }

    $parsedDates = @($Dates | ForEach-Object {
        $parsed = [datetime]::MinValue
        if (-not [datetime]::TryParseExact(
            $_,
            "yyyy-MM-dd",
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::None,
            [ref]$parsed
        )) {
            throw "Invalid date '$_'; dates must use YYYY-MM-DD."
        }
        $parsed.Date
    })
    $canonicalDates = @($parsedDates | Sort-Object -Unique)
    if ($canonicalDates.Count -ne $parsedDates.Count) {
        throw "Dates must be unique."
    }
    for ($index = 0; $index -lt $parsedDates.Count; $index++) {
        if ($parsedDates[$index] -ne $canonicalDates[$index]) {
            throw "Dates must be supplied in strictly increasing order."
        }
    }
    foreach ($marketDate in $canonicalDates) {
        $dateText = $marketDate.ToString("yyyy-MM-dd")
        $plannedBackup = Join-Path "backups/wp14" "replay-$dateText-post.sqlite"
        if (Test-Path -LiteralPath $plannedBackup) {
            throw "Refusing to start because a planned backup already exists: $plannedBackup"
        }
    }

    $batchId = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
    $batchRoot = Join-Path "state/replay/batches" $batchId
    New-Item -ItemType Directory -Path $batchRoot -ErrorAction Stop | Out-Null

    function Invoke-Pq {
        param(
            [Parameter(Mandatory = $true)][string[]]$Arguments,
            [Parameter(Mandatory = $true)][string]$Transcript
        )
        $previousErrorPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $output = @(& uv run pq @Arguments 2>&1)
            $exitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorPreference
        }
        $output | Tee-Object -FilePath $Transcript | Write-Host
        if ($exitCode -ne 0) {
            throw "pq $($Arguments[0]) failed with exit code $exitCode. See $Transcript"
        }
        return $output
    }

    foreach ($marketDate in $canonicalDates) {
        $dateText = $marketDate.ToString("yyyy-MM-dd")
        $dateRoot = Join-Path $batchRoot $dateText
        New-Item -ItemType Directory -Path $dateRoot -ErrorAction Stop | Out-Null
        Write-Host "=== Historical replay $dateText ==="

        $calendarOutput = Invoke-Pq -Arguments @(
            "calendar-check", "--date", $dateText, "--config", $Calendar
        ) -Transcript (Join-Path $dateRoot "01-calendar.txt")
        if (-not ($calendarOutput -match ": TRADING \(")) {
            throw "$dateText is not an approved trading date."
        }

        $downloadOutput = Invoke-Pq -Arguments @(
            "historical-download",
            "--production",
            "--instrument", $Instrument,
            "--start", $dateText,
            "--end", $dateText,
            "--interval", "minute",
            "--snapshot", $SnapshotDirectory,
            "--calendar", $Calendar,
            "--root", "data"
        ) -Transcript (Join-Path $dateRoot "02-download.txt")
        if (-not ($downloadOutput -match "Raw=375 Curated=375 Invalid=0 Gaps=0")) {
            throw "$dateText did not produce one complete 375-minute NSE session."
        }
        $manifestLine = @($downloadOutput | Where-Object { $_ -match "^Manifest:\s+(.+)$" })
        if ($manifestLine.Count -ne 1) {
            throw "$dateText did not report exactly one immutable manifest."
        }
        $manifestPath = ([regex]::Match($manifestLine[0], "^Manifest:\s+(.+)$")).Groups[1].Value.Trim()
        if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
            throw "Manifest does not exist: $manifestPath"
        }

        $configPath = Join-Path $dateRoot "historical_paper.yaml"
        @"
schema_version: 1
database_path: $($ReplayDatabase.Replace('\', '/'))
runtime_config: config/historical_paper_runtime.example.yaml
risk_config: config/risk/conservative_10k.yaml
strategy_config: config/strategies/baseline_momentum_v1.yaml
calendar_config: $($Calendar.Replace('\', '/'))
historical_manifest: $($manifestPath.Replace('\', '/'))
market_date: $dateText
instrument: $Instrument
"@ | Set-Content -LiteralPath $configPath -Encoding utf8

        Invoke-Pq -Arguments @(
            "historical-paper-session",
            "--config", $configPath,
            "--confirm", "START HISTORICAL PAPER REPLAY"
        ) -Transcript (Join-Path $dateRoot "03-replay.txt") | Out-Null

        Invoke-Pq -Arguments @(
            "db-check", "--path", $ReplayDatabase
        ) -Transcript (Join-Path $dateRoot "04-database-audit.txt") | Out-Null

        $backupPath = Join-Path "backups/wp14" "replay-$dateText-post.sqlite"
        if (Test-Path -LiteralPath $backupPath) {
            throw "Refusing to overwrite existing backup: $backupPath"
        }
        Invoke-Pq -Arguments @(
            "backup", "--path", $ReplayDatabase, "--destination", $backupPath
        ) -Transcript (Join-Path $dateRoot "05-backup.txt") | Out-Null

        Invoke-Pq -Arguments @(
            "hybrid-evidence-status",
            "--operational-path", $OperationalDatabase,
            "--replay-path", $ReplayDatabase
        ) -Transcript (Join-Path $dateRoot "06-hybrid-status.txt") | Out-Null

        Write-Host "Completed and backed up $dateText."
    }

    Write-Host "Controlled batch complete. Transcripts: $batchRoot"
}
finally {
    Pop-Location
}
