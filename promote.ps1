# Beehive Wire — social cards, a sitemap, and robots pointing at it.
#   powershell -ExecutionPolicy Bypass -File .\promote.ps1

Set-Location -Path $PSScriptRoot

Write-Host ""
Write-Host "1/2  Pulling anything the bot published..." -ForegroundColor Cyan
git pull --rebase --autostash
if ($LASTEXITCODE -ne 0) { Write-Host "     pull failed - stopping." -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "2/2  Committing and pushing..." -ForegroundColor Cyan
git add -A
git commit -m "Social sharing cards, sitemap.xml, robots pointing at it"
git push
if ($LASTEXITCODE -ne 0) { Write-Host "     push failed." -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "Done. Sitemap will be live at https://beehivewire.com/sitemap.xml" -ForegroundColor Green
