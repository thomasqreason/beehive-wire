# Beehive Wire — the Buchanan edition: new beats, 100 links (half new, half held over), and the standing orders to match.
#   powershell -ExecutionPolicy Bypass -File .\beats.ps1
#   powershell -ExecutionPolicy Bypass -File .\beats.ps1 -PrintNow     # also print a fresh edition right away
#
# Ships seven files (all already in this folder; page.html and sources.html are moved into templates\):
#   editorial.md   the rewritten lens: sports, Ukraine, crime-say-who, Trump watch, midterms, economy,
#                  housing, cynical Hollywood, and the rule that the page is never all bad news
#   config.yaml    100 links in ONE column, 23-topic mix, bigger candidate pool
#   feeds.yaml     ~55 new sources for the new beats; Musk's fan feeds dropped
#   build.py       the 23 topics, a compact editor payload so the whole wire fits in one read, and the
#                  new-vs-held-over marking for the page
#   page.html      one column; held-over stories in gray; the new-story count on the masthead; footer links the roster
#   sources.html   new page at /sources/ — every outlet we read, beat by beat, rebuilt each edition from feeds.yaml
# Nothing under .github changes. The evening edition picks this up on its own.

param([switch]$PrintNow)

Set-Location -Path $PSScriptRoot

Write-Host ""
Write-Host "0/4  Putting the page template in place..." -ForegroundColor Cyan
foreach ($f in 'page.html', 'sources.html') {
    if (Test-Path $f) { Move-Item -Force $f "templates\$f"; Write-Host "     templates\$f" -ForegroundColor Green } else { Write-Host "     $f already in place" -ForegroundColor DarkGray }
}

Write-Host ""
Write-Host "1/4  Checking the new files parse and the mix adds up..." -ForegroundColor Cyan
python -c "import yaml; c=yaml.safe_load(open('config.yaml')); f=yaml.safe_load(open('feeds.yaml')); s=sum(c['mix'].values()); assert s==c['page']['total_links'], s; print('     ', len(f['feeds']), 'feeds,', c['page']['total_links'], 'links, mix sums to', s)"
if ($LASTEXITCODE -ne 0) { Write-Host "     config or feeds failed to parse - stopping." -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "2/4  Testing the feeds (about 30 seconds; only problems are listed)..." -ForegroundColor Cyan
python check_feeds.py 2>$null | Select-String -Pattern '^(DEAD|STALE)' | ForEach-Object { "     " + $_.Line }
Write-Host "     (a dead feed never breaks a build; it is skipped)" -ForegroundColor DarkGray

Write-Host ""
Write-Host "3/4  Pulling anything the bot published..." -ForegroundColor Cyan
git pull --rebase --autostash
if ($LASTEXITCODE -ne 0) { Write-Host "     pull failed - stopping so nothing is lost." -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "4/4  Committing and pushing..." -ForegroundColor Cyan
git add -A
git commit -m "The Buchanan edition: 100 links in one column, held-over stories in gray, 23 beats, video links"
git push
if ($LASTEXITCODE -ne 0) { Write-Host "     push failed." -ForegroundColor Red; exit 1 }

if ($PrintNow) {
    Write-Host ""
    Write-Host "Printing a fresh edition now (re-prints the current slot, 100 links)..." -ForegroundColor Cyan
    Start-Sleep -Seconds 10
    gh workflow run build.yml
    Write-Host "     about 8 minutes - then check https://beehivewire.com" -ForegroundColor DarkGray
} else {
    Write-Host ""
    Write-Host "Done. The next scheduled edition will be the first one under the new orders." -ForegroundColor Green
    Write-Host "To see it sooner:  powershell -ExecutionPolicy Bypass -File .\beats.ps1 -PrintNow" -ForegroundColor DarkGray
}
