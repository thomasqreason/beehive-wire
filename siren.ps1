# Beehive Wire — install the siren: an hourly watch that almost always does nothing.
#   powershell -ExecutionPolicy Bypass -File .\siren.ps1

Set-Location -Path $PSScriptRoot

Write-Host ""
Write-Host "1/4  Putting the hourly watch in place..." -ForegroundColor Cyan
if (Test-Path 'breaking-workflow.yml') {
    New-Item -ItemType Directory -Force -Path '.github\workflows' | Out-Null
    Move-Item -Force 'breaking-workflow.yml' '.github\workflows\breaking.yml'
    Write-Host "     .github\workflows\breaking.yml" -ForegroundColor Green
} else {
    Write-Host "     already in place" -ForegroundColor DarkGray
}

Write-Host "2/4  Pulling anything the bot published..." -ForegroundColor Cyan
git pull --rebase --autostash
if ($LASTEXITCODE -ne 0) { Write-Host "     pull failed - stopping." -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "3/4  Relabelling the archive under the corrected edition rule..." -ForegroundColor Cyan
python backfill_archive.py
if ($LASTEXITCODE -ne 0) { Write-Host "     backfill failed - nothing pushed." -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "4/4  Committing and pushing..." -ForegroundColor Cyan
git add -A
git commit -m "The siren, and name an edition for the slot that passed"
git push
if ($LASTEXITCODE -ne 0) { Write-Host "     push failed." -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "Done. The watch runs at :23 past every hour." -ForegroundColor Green
Write-Host "To test it by hand:  gh workflow run breaking.yml" -ForegroundColor DarkGray
