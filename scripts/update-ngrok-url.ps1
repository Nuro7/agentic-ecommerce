# update-ngrok-url.ps1
# Run this every time Docker restarts to sync the new ngrok URL into WordPress.
# Usage: powershell -ExecutionPolicy Bypass -File update-ngrok-url.ps1

$PHP      = "C:\Users\hp\AppData\Roaming\Local\lightning-services\php-8.2.27+1\bin\win64\php.exe"
$WPCLI    = "C:\Users\hp\AppData\Local\Programs\Local\resources\extraResources\bin\wp-cli\wp-cli.phar"
$WP_PATH  = "C:\Users\hp\Local Sites\ecomify\app\public"
$OPTION   = "wooagent_settings"

Write-Host "Fetching current ngrok URL..." -ForegroundColor Cyan

try {
    $response = Invoke-RestMethod -Uri "http://localhost:4040/api/tunnels" -TimeoutSec 5
    $ngrokUrl = ($response.tunnels | Where-Object { $_.proto -eq "https" } | Select-Object -First 1).public_url
    if (-not $ngrokUrl) {
        $ngrokUrl = $response.tunnels[0].public_url
    }
} catch {
    Write-Host "ERROR: Could not reach ngrok at localhost:4040. Is Docker running?" -ForegroundColor Red
    exit 1
}

Write-Host "ngrok URL: $ngrokUrl" -ForegroundColor Green

# Read current WordPress option, update backend_url, write back
$phpScript = @"
<?php
define('ABSPATH', '$($WP_PATH.Replace('\','\\'))\\');
require '$($WP_PATH.Replace('\','\\'))\\wp-load.php';
\$opts = get_option('$OPTION', array());
\$opts['backend_url'] = '$ngrokUrl';
update_option('$OPTION', \$opts);
echo 'Updated backend_url to: ' . \$opts['backend_url'];
"@

$tmpFile = [System.IO.Path]::GetTempFileName() + ".php"
$phpScript | Out-File -FilePath $tmpFile -Encoding utf8

Write-Host "Updating WordPress option '$OPTION'..." -ForegroundColor Cyan

$result = & $PHP $tmpFile 2>&1
Remove-Item $tmpFile -Force

if ($result -match "Updated backend_url") {
    Write-Host $result -ForegroundColor Green
    Write-Host ""
    Write-Host "Done. Hard-refresh your browser (Ctrl+Shift+R) to reconnect." -ForegroundColor Yellow
} else {
    Write-Host "PHP output: $result" -ForegroundColor Yellow
    Write-Host "If that failed, set manually: WP Admin -> WooCommerce -> WooAgent -> Backend URL = $ngrokUrl" -ForegroundColor Yellow
}
