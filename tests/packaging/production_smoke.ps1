# tests/packaging/production_smoke.ps1
# Usage: .\production_smoke.ps1 -Origin https://wiki.example.com [-CaCertPath .\dev-ca.pem]
# Exit 0 = all 5 checks OK. Exit 1 = any failure.

param(
    [Parameter(Mandatory=$true)]
    [string]$Origin,
    [string]$CaCertPath = ""
)

if (-not $Origin.StartsWith("https://")) {
    Write-Error "ERROR: argument must be https:// public origin URL"
    exit 1
}
$Origin = $Origin.TrimEnd("/")

$ErrorActionPreference = "Stop"

function Check-Url {
    param([string]$Url, [string]$Label)
    Write-Host "[$Label] $Url"
    try {
        $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 30
        return $resp
    } catch {
        Write-Host "FAIL: $Label - $_"
        exit 1
    }
}

# Check (a): /health/live
Write-Host "[a] /health/live"
$resp = Check-Url "$Origin/health/live" "a"
if ($resp.Content -notmatch '"status":"alive"') { Write-Host "FAIL: live status"; exit 1 }
Write-Host "  OK"

# Check (b): /health/ready
Write-Host "[b] /health/ready"
$resp = Check-Url "$Origin/health/ready" "b"
if ($resp.Content -notmatch '"status":"ready"') { Write-Host "FAIL: ready status"; exit 1 }
if ($resp.Content -notmatch '"db":"ok"') { Write-Host "FAIL: db component"; exit 1 }
Write-Host "  OK"

# Check (c): OAuth metadata
Write-Host "[c] /.well-known/oauth-protected-resource"
$resp = Check-Url "$Origin/.well-known/oauth-protected-resource" "c"
if ($resp.Content -notmatch [regex]::Escape('"resource":"' + $Origin + '/mcp"')) {
    Write-Host "FAIL: resource mismatch"; exit 1
}
Write-Host "  OK"

# Check (d): TLS cert
Write-Host "[d] TLS cert"
try {
    $host_part = ($Origin -replace "^https://", "").Split("/")[0]
    $tls_host = $host_part.Split(":")[0]
    $tls_port = 443
    if ($host_part.Contains(":")) { $tls_port = [int]$host_part.Split(":")[1] }
    if ($CaCertPath) {
        if (-not (Test-Path $CaCertPath)) { Write-Host "FAIL: CA file missing"; exit 1 }
        $opensslArgs = @(
            "s_client",
            "-servername", $tls_host,
            "-connect", "$($tls_host):$($tls_port)",
            "-verify_return_error",
            "-CAfile", $CaCertPath
        )
        $certText = "" | & openssl @opensslArgs 2>$null
        if ($LASTEXITCODE -ne 0) { Write-Host "FAIL: TLS verify"; exit 1 }
        $certText | & openssl x509 -noout -dates -checkend 86400 | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Host "FAIL: cert expires soon"; exit 1 }
    } else {
        $tcpClient = New-Object System.Net.Sockets.TcpClient
        $tcpClient.Connect($tls_host, $tls_port)
        $sslStream = New-Object System.Net.Security.SslStream($tcpClient.GetStream(), $false)
        $sslStream.AuthenticateAsClient($tls_host)
        $cert = $sslStream.RemoteCertificate
        $expiry = [datetime]::Parse($cert.GetExpirationDateString())
        if ($expiry -lt (Get-Date).AddDays(1)) { Write-Host "FAIL: cert expires soon"; exit 1 }
        $sslStream.Close()
        $tcpClient.Close()
    }
} catch {
    Write-Host "FAIL: TLS check - $_"; exit 1
}
Write-Host "  OK"

# Check (e): /mcp 401
Write-Host "[e] /mcp 401 + WWW-Authenticate"
try {
    $resp = Invoke-WebRequest -Uri "$Origin/mcp" -UseBasicParsing -TimeoutSec 30
    Write-Host "FAIL: /mcp should return 401, got $($resp.StatusCode)"; exit 1
} catch {
    $statusCode = $_.Exception.Response.StatusCode.value__
    if ($statusCode -ne 401) { Write-Host "FAIL: /mcp not 401, got $statusCode"; exit 1 }
    $wwwAuth = $_.Exception.Response.Headers["WWW-Authenticate"]
    if (-not $wwwAuth -or $wwwAuth -notmatch "resource_metadata") {
        Write-Host "FAIL: WWW-Authenticate missing metadata URL"; exit 1
    }
}
Write-Host "  OK"

Write-Host ""
Write-Host "ALL CHECKS PASSED ($Origin)"
exit 0
