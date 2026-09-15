# Beehive Wire — the catch-up. No edition waits on GitHub's scheduler any more.
#   powershell -ExecutionPolicy Bypass -File .\catchup.ps1
#
# Ships three things:
#   catchup.py                       knows whether the slot that just came round has printed
#   .github\workflows\build.yml      a cron run that arrives after the edition is already out stands down
#   .github\workflows\breaking.yml   the hourly siren sends the edition itself when it is missing
# then pushes, and asks GitHub to print this morning's edition right now if it still hasn't.

Set-Location -Path $PSScriptRoot

Write-Host ""
Write-Host "1/4  Moving the workflows into place..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path '.github\workflows' | Out-Null
foreach ($f in 'build.yml', 'breaking.yml') {
    if (Test-Path $f) {
        Move-Item -Force $f ".github\workflows\$f"
        Write-Host "     .github\workflows\$f" -ForegroundColor Green
    } else {
        Write-Host "     $f already in place" -ForegroundColor DarkGray
    }
}
if (-not (Test-Path 'catchup.py')) { Write-Host "     catchup.py is missing - stopping." -ForegroundColor Red; exit 1 }
Select-String -Path '.github\workflows\build.yml' -Pattern 'cron:|needs: check|catchup.py' | ForEach-Object { "     " + $_.Line.Trim() }

Write-Host ""
Write-Host "2/4  Pulling anything the bot published..." -ForegroundColor Cyan
git pull --rebase --autostash
if ($LASTEXITCODE -ne 0) { Write-Host "     pull failed - stopping so nothing is lost." -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "3/4  Committing and pushing..." -ForegroundColor Cyan
git add -A
git commit -m "The catch-up: a late cron run stands down if the edition is out, the siren sends it if not"
git push
if ($LASTEXITCODE -ne 0) { Write-Host "     push failed." -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "4/4  Asking GitHub to print the edition now (it stands down by itself if it is already out)..." -ForegroundColor Cyan
Start-Sleep -Seconds 15     # GitHub needs a moment to notice the workflow's new catchup input
gh workflow run build.yml -f catchup=true
if ($LASTEXITCODE -ne 0) {
    Write-Host "     not registered yet - trying once more in 20 seconds..." -ForegroundColor DarkGray
    Start-Sleep -Seconds 20
    gh workflow run build.yml -f catchup=true
}

Write-Host ""
Write-Host "Done. In about four minutes https://beehivewire.com should read MORNING EDITION - NO. 3." -ForegroundColor Green
Write-Host "The push itself starts a short re-render first; the edition run queues right behind it." -ForegroundColor DarkGray
Write-Host ""
gh run list --limit 5
