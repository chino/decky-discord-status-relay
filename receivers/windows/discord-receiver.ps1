# Receives Discord rich presence from another machine (the Decky plugin or a
# relay) and passes it to the Discord app on this PC.
#
# Games talk to Discord through a local named pipe (\\.\pipe\discord-ipc-0).
# The other machine runs a small relay that exposes a fake Discord socket there and
# forwards every connection over the network to this listener. Each accepted
# TCP connection gets its own pipe connection, and bytes are copied both ways
# until either side closes.
#
# With -Allow, only those addresses are accepted; without it, anyone the
# firewall lets through.
#
# Only one copy runs at a time (a named mutex); a second start exits at once,
# so a scheduler can safely start it every few minutes. Errors on one
# connection are logged and skipped; if the listener itself fails, it is
# rebuilt after a short pause.
#
# Usage:  powershell -NoProfile -ExecutionPolicy Bypass -File discord-receiver.ps1 [-Allow 192.0.2.20]
param(
    [int]$Port = 8920,
    [string[]]$Allow = @(),                  # optional: address(es) allowed to connect
    [string]$Token = '',                     # optional; the sender must open with "AUTH <token>"
    [string]$PipeName = 'discord-ipc-0'
)
$ErrorActionPreference = 'Stop'
$Allow = $Allow -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }  # -File passes 'a,b' as one string
$log = Join-Path $env:LOCALAPPDATA 'discord-relay.log'
function Log($m) {
    try { "$(Get-Date -Format s) $m" | Add-Content -Path $log } catch {}
}

# Single instance.
$created = $false
$mutex = [Threading.Mutex]::new($true, 'Local\DiscordPresenceReceiver', [ref]$created)
if (-not $created) { exit 0 }

# Keep the log from growing forever.
try { if ((Get-Item $log -ErrorAction SilentlyContinue).Length -gt 1MB) { Move-Item $log "$log.old" -Force } } catch {}

$sessions = [Collections.Generic.List[object]]::new()

# Reads the "AUTH <token>" line byte by byte so nothing after it is consumed.
function Test-Token($stream) {
    if (-not $Token) { return $true }
    $stream.ReadTimeout = 3000
    $bytes = [Collections.Generic.List[byte]]::new()
    while ($bytes.Count -lt 512) {
        $b = $stream.ReadByte()
        if ($b -lt 0) { return $false }
        if ($b -eq 10) { break }
        $bytes.Add([byte]$b)
    }
    $stream.ReadTimeout = [Threading.Timeout]::Infinite
    $line = [Text.Encoding]::UTF8.GetString($bytes.ToArray()).TrimEnd("`r")
    return $line -ceq "AUTH $Token"
}

function Close-Session($s) {
    try { $s.Pipe.Dispose() } catch {}
    try { $s.Tcp.Dispose() } catch {}
}

while ($true) {
    $listener = $null
    try {
        $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Any, $Port)
        $listener.Start()
        Log "listening on $Port, allowed: $(if ($Allow) { $Allow -join ', ' } else { 'any' }) (pid $PID)"
        while ($true) {
            if ($listener.Pending()) {
                $tcp = $listener.AcceptTcpClient()
                try {
                    $ip = $tcp.Client.RemoteEndPoint.Address.ToString()
                    if ($Allow -and $Allow -notcontains $ip) {
                        Log "rejected $ip"; $tcp.Close(); continue
                    }
                    $net = $tcp.GetStream()
                    if (-not (Test-Token $net)) {
                        Log "rejected $ip (bad or missing token)"; $tcp.Close(); continue
                    }
                    $pipeStream = [IO.Pipes.NamedPipeClientStream]::new('.', $PipeName,
                        [IO.Pipes.PipeDirection]::InOut, [IO.Pipes.PipeOptions]::Asynchronous)
                    try { $pipeStream.Connect(3000) } catch {
                        Log "Discord pipe not available ($($_.Exception.Message)); is Discord running?"
                        $pipeStream.Dispose(); $tcp.Close(); continue
                    }
                    $sessions.Add([pscustomobject]@{
                        Ip = $ip; Tcp = $tcp; Pipe = $pipeStream
                        Up = $net.CopyToAsync($pipeStream); Down = $pipeStream.CopyToAsync($net)
                    })
                    Log "relaying $ip ($($sessions.Count) open)"
                } catch {
                    Log "connection error: $($_.Exception.Message)"
                    try { $tcp.Close() } catch {}
                }
            }
            # Close sessions where either direction has finished.
            for ($i = $sessions.Count - 1; $i -ge 0; $i--) {
                $s = $sessions[$i]
                if ($s.Up.IsCompleted -or $s.Down.IsCompleted) {
                    Close-Session $s; $sessions.RemoveAt($i)
                    Log "closed $($s.Ip) ($($sessions.Count) open)"
                }
            }
            Start-Sleep -Milliseconds 100
        }
    } catch {
        Log "listener error: $($_.Exception.Message); restarting in 5s"
        foreach ($s in $sessions) { Close-Session $s }
        $sessions.Clear()
        try { if ($listener) { $listener.Stop() } } catch {}
        Start-Sleep -Seconds 5
    }
}
