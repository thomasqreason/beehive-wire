# Beehive Wire - one-shot repair: fresh subscription token -> repo secret -> build an edition.
# Run from the beehive-wire folder:  powershell -ExecutionPolicy Bypass -File .\fix.ps1

Set-Location -Path $PSScriptRoot
$tmp = Join-Path $env:TEMP "bw-token.txt"

try {
    Write-Host ""
    Write-Host "1/4  Minting a fresh token. Approve in the browser when it opens." -ForegroundColor Cyan
    Write-Host "     You do NOT need to copy anything this time." -ForegroundColor DarkGray
    Write-Host ""

    claude setup-token | Tee-Object -FilePath $tmp

    $raw = ""
    if (Test-Path $tmp) { $raw = Get-Content $tmp -Raw }
    $m = [regex]::Match($raw, 'sk-ant-oat01-[A-Za-z0-9_\-]+')

    if (-not $m.Success) {
        Write-Host ""
        Write-Host "No token found in that output. Nothing was changed." -ForegroundColor Red
        Write-Host "Tell Claude: 'the script could not find a token'." -ForegroundColor Red
        exit 1
    }

    $tok = $m.Value
    Write-Host ""
    Write-Host ("2/4  Got a complete token: {0} characters, starts {1}" -f $tok.Length, $tok.Substring(0,12)) -ForegroundColor Green

    Write-Host "3/4  Storing it as the repository secret..." -ForegroundColor Cyan
    $tok | gh secret set CLAUDE_CODE_OAUTH_TOKEN
    if ($LASTEXITCODE -ne 0) { Write-Host "Storing the secret failed." -ForegroundColor Red; exit 1 }

    Write-Host "4/4  Building an edition. This takes 2-4 minutes." -ForegroundColor Cyan
    gh workflow run build.yml
    Start-Sleep -Seconds 25

    $id = gh run list --workflow=build.yml --limit 1 --json databaseId --jq '.[0].databaseId'
    Write-Host ("     watching run {0}" -f $id) -ForegroundColor DarkGray
    gh run watch $id

    Write-Host ""
    Write-Host "--- what the editor step said (last lines) ---" -ForegroundColor Cyan
    gh run view $id --log | Select-String "Edit the edition" | Select-Object -Last 15 | Out-String -Width 300

    Write-Host ""
    Write-Host "Done. Screenshot everything above this line." -ForegroundColor Green
}
finally {
    if (Test-Path $tmp) { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
}
