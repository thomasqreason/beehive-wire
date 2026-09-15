# Beehive Wire — ship the icons, the app manifest, the archive and edition numbers,
# then rebuild the archive from the repo's own history so edition No. 1 is in the book.
#   powershell -ExecutionPolicy Bypass -File .\ship.ps1

Set-Location -Path $PSScriptRoot
$ErrorActionPreference = 'Continue'

Write-Host ""
Write-Host "1/6  Letting the workflow commit the archive folder..." -ForegroundColor Cyan
$f = '.github\workflows\build.yml'
$t = [IO.File]::ReadAllText($f)
$t = $t.Replace('git add data/state.json archive', 'git add data/state.json')   # normalise first
$t = $t.Replace('git add data/state.json', 'git add data/state.json archive')
$t = $t.Replace('paths-ignore: ["data/**", "archive/**"]', 'paths-ignore: ["data/**"]')
$t = $t.Replace('paths-ignore: ["data/**"]', 'paths-ignore: ["data/**", "archive/**"]')
[IO.File]::WriteAllText($f, $t)
Write-Host "     done" -ForegroundColor Green

Write-Host "2/6  Making sure the Python bits are installed..." -ForegroundColor Cyan
python -m pip install -q -r requirements.txt 2>&1 | Out-Null
Write-Host "     done" -ForegroundColor Green

Write-Host "3/6  Pulling the editions the bot has published..." -ForegroundColor Cyan
git pull --rebase --autostash
if ($LASTEXITCODE -ne 0) { Write-Host "     pull failed - stopping so nothing is lost." -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "4/6  Rebuilding the archive from the repository's history..." -ForegroundColor Cyan
python backfill_archive.py
if ($LASTEXITCODE -ne 0) { Write-Host "     backfill failed - nothing pushed." -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "5/6  Committing and pushing..." -ForegroundColor Cyan
git add -A
git commit -m "Brand icon set, installable web app, edition archive with a calendar and numbering"
git push
if ($LASTEXITCODE -ne 0) { Write-Host "     push failed." -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "6/6  Recent workflow runs - this tells us whether the 6 p.m. edition fired:" -ForegroundColor Cyan
gh run list --limit 8

Write-Host ""
Write-Host "Done. Screenshot from step 4 down." -ForegroundColor Green
