# Run from HexaAgent/backend
#
# Host and port come from the repo-root .env (API_HOST / API_PORT) so the backend, the
# Angular dev server and the CLI all agree — see .env.example. Uvicorn needs them on the
# command line, before the app is imported, hence the parsing here.
$env:PYTHONPATH = (Get-Location).Path

$apiHost = "0.0.0.0"
$apiPort = "8555"

# .env.example first so a fresh clone without a .env still starts on the documented port;
# a real .env then overrides it key by key.
foreach ($envFile in @("..\.env.example", "..\.env")) {
    if (Test-Path $envFile) {
        Get-Content $envFile | ForEach-Object {
            $line = $_.Trim()
            if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
                $key, $value = $line.Split("=", 2)
                $key = $key.Trim()
                $value = $value.Trim().Trim('"').Trim("'")
                if ($value) {
                    if ($key -eq "API_HOST") { $apiHost = $value }
                    if ($key -eq "API_PORT") { $apiPort = $value }
                }
            }
        }
    }
}

Write-Host "Starting API on http://${apiHost}:${apiPort}" -ForegroundColor Cyan
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host $apiHost --port $apiPort
