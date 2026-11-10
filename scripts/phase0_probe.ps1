<#
.SYNOPSIS
    Phase 0 probe for the DMX Inventory app: checks the Ingest Processor (DMX)
    management endpoints on a Splunk Cloud stack and saves sample responses.

.DESCRIPTION
    Read-only. Makes only GET requests. Checks, in order:
      1. Connectivity and token     GET /services/server/info
      2. Who the token is, roles    GET /services/authentication/current-context   (assumption A3)
      3. Each DMX endpoint          GET <BasePath>/<endpoint>                      (A1)
         - response shape: would the app's parser recognise it?
         - pagination: does the endpoint honour offset/count?                       (A4)
         - stable ID and modified-time fields present?                              (A5)
         - largest object size (event size risk R6)
    A2 (reachable from inside the search head) and A7 (same answer on every
    cluster member) can't be tested from outside; run "| dmxcollect dry_run=t"
    in Splunk for those.

    Writes to tests/fixtures/live/ (git-ignored):
      <endpoint>.json            raw first page; MAY CONTAIN SECRETS, don't share
      <endpoint>.scrubbed.json   same with credential-like values redacted
      summary.json               the results below, machine-readable

    Works in Windows PowerShell 5.1 and PowerShell 7+.

.PARAMETER Stack
    Stack name (mystack -> mystack.splunkcloud.com) or a full host name.
    Defaults to $env:SPLUNK_STACK.

.PARAMETER BaseUrl
    Full management URL, e.g. https://mystack.splunkcloud.com:8089.
    Overrides -Stack.

.PARAMETER BasePath
    Path the DMX endpoints sit under. Default /services/v2/dmx (from the
    handover, unconfirmed).

.PARAMETER Token
    Token. Defaults to $env:SPLUNK_TOKEN; if neither is set you're prompted
    (input hidden). Prefer the prompt or the environment variable so the token
    doesn't end up in your shell history.

.PARAMETER AuthScheme
    Bearer (authentication token, default) or Splunk (session key).

.EXAMPLE
    $env:SPLUNK_STACK = 'mystack'
    .\scripts\phase0_probe.ps1

.EXAMPLE
    .\scripts\phase0_probe.ps1 -Stack mystack -BasePath /services/v2/dmx -PageSize 5

.NOTES
    Your public IP must be on the stack's ACS allow list for search-api
    (management port 8089). TLS certificates are verified; there is no option
    to skip that.
    Exit code: 0 if every endpoint returned 200 with a recognised shape, 1 otherwise.
#>
[CmdletBinding()]
param(
    [string]$Stack = $env:SPLUNK_STACK,
    [string]$BaseUrl,
    [string]$BasePath = '/services/v2/dmx',
    [string[]]$Endpoints = @('pipelines', 'destinations', 'datasets'),
    [string]$Token = $env:SPLUNK_TOKEN,
    [ValidateSet('Bearer', 'Splunk')]
    [string]$AuthScheme = 'Bearer',
    [ValidateRange(1, 1000)]
    [int]$PageSize = 2,
    [int]$TimeoutSec = 60,
    [string]$OutDir = (Join-Path (Join-Path (Join-Path $PSScriptRoot '..') 'tests') (Join-Path 'fixtures' 'live'))
)

$ErrorActionPreference = 'Stop'

# Windows PowerShell 5.1 may default to TLS 1.0/1.1, which Splunk Cloud refuses.
if ($PSVersionTable.PSVersion.Major -lt 6) {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
}

# ---------------------------------------------------------------- settings ---
# Keep these lists in step with dmx_inventory/bin/dmx_inventory_lib/client.py and normalize.py.
$ListKeys = @('items', 'results', 'entry', 'data', 'pipelines', 'destinations', 'datasets', 'content', 'values', 'records')
$TotalKeys = @('total', 'totalCount', 'total_count', 'totalResults')
$IdKeys = @('id', '_key', 'uuid', 'pipelineId', 'destinationId', 'datasetId', 'name')
$ModifiedKeys = @('modified', 'updated', 'updatedAt', 'updated_at', 'lastModified', 'last_modified', 'modifiedAt', 'modified_at', 'lastUpdated')
$SecretWords = @('password', 'passwd', 'passphrase', 'pwd', 'secret', 'secrets', 'token', 'tokens', 'credential', 'credentials',
    'apikey', 'privatekey', 'bearer', 'authorization', 'auth', 'sas', 'hmac', 'signature')
$SecretPairs = @('api key', 'access key', 'private key', 'secret key', 'client secret', 'account key', 'signing key', 'shared key', 'connection string')
$Redacted = '***REDACTED***'

# ----------------------------------------------------------------- helpers ---
function Write-Step([string]$Text) { Write-Host ''; Write-Host "== $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "   [OK]   $Text" -ForegroundColor Green }
function Write-Warn([string]$Text) { Write-Host "   [WARN] $Text" -ForegroundColor Yellow }
function Write-Fail([string]$Text) { Write-Host "   [FAIL] $Text" -ForegroundColor Red }
function Write-Info([string]$Text) { Write-Host "          $Text" }

function Get-PlainToken {
    if ($Token) { return $Token }
    $secure = Read-Host -Prompt 'Splunk token (input hidden)' -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

function Get-PropNames($Obj) {
    if ($null -eq $Obj -or $Obj -isnot [psobject] -or $Obj -is [string] -or $Obj -is [array]) { return @() }
    return @($Obj.PSObject.Properties | Where-Object { $_.MemberType -eq 'NoteProperty' } | ForEach-Object { $_.Name })
}

function Get-Prop($Obj, [string]$Name) {
    if ($null -eq $Obj) { return $null }
    $p = $Obj.PSObject.Properties[$Name]
    # The leading comma stops PowerShell unwrapping a one-element array into its element.
    if ($p) { return , $p.Value }
    return $null
}

function Invoke-SplunkGet([string]$Path, [hashtable]$Query) {
    $pairs = @()
    foreach ($k in $Query.Keys) { $pairs += ('{0}={1}' -f [uri]::EscapeDataString($k), [uri]::EscapeDataString([string]$Query[$k])) }
    $uri = $script:Root + $Path
    if ($pairs.Count) { $uri += '?' + ($pairs -join '&') }
    $headers = @{ Authorization = "$AuthScheme $script:PlainToken"; Accept = 'application/json' }
    $result = [ordered]@{ Uri = $uri; Status = $null; Body = $null; Error = $null; Ms = 0 }
    $sw = [Diagnostics.Stopwatch]::StartNew()
    try {
        $r = Invoke-WebRequest -Uri $uri -Headers $headers -Method Get -UseBasicParsing -TimeoutSec $TimeoutSec
        $result.Status = [int]$r.StatusCode
        $result.Body = if ($r.Content -is [byte[]]) { [Text.Encoding]::UTF8.GetString($r.Content) } else { [string]$r.Content }
    }
    catch {
        $resp = $null
        if ($_.Exception.PSObject.Properties['Response']) { $resp = $_.Exception.Response }
        if ($resp) {
            $result.Status = [int]$resp.StatusCode
            if ($_.ErrorDetails -and $_.ErrorDetails.Message) { $result.Body = $_.ErrorDetails.Message }
        }
        else {
            $result.Error = $_.Exception.Message
        }
    }
    $sw.Stop()
    $result.Ms = [int]$sw.ElapsedMilliseconds
    return $result
}

function ConvertFrom-JsonSafe([string]$Text) {
    try { return , (ConvertFrom-Json -InputObject $Text) } catch { return $null }
}

function Get-ErrorText($Response) {
    if ($Response.Error) { return $Response.Error }
    $j = ConvertFrom-JsonSafe $Response.Body
    if ($j -and @(Get-PropNames $j) -contains 'messages') { return (@($j.messages) | ForEach-Object { $_.text }) -join '; ' }
    if ($Response.Body) { return $Response.Body.Substring(0, [Math]::Min(200, $Response.Body.Length)) }
    return ''
}

# Mirrors client.extract_items(): returns @{ Items; Total; ListKey } or $null if unrecognised.
function Get-Items($Payload, [string]$Type) {
    if ($Payload -is [array]) { return @{ Items = @($Payload); Total = $null; ListKey = '(top-level array)' } }
    $names = @(Get-PropNames $Payload)
    if (-not $names.Count) { return $null }
    $total = $null
    foreach ($k in $TotalKeys) { if ($names -contains $k -and (Get-Prop $Payload $k) -is [ValueType]) { $total = [int](Get-Prop $Payload $k); break } }
    if ($null -eq $total -and $names -contains 'paging') {
        $paging = Get-Prop $Payload 'paging'
        if (@(Get-PropNames $paging) -contains 'total') { $total = [int]$paging.total }
    }
    $order = @($Type) + @($ListKeys | Where-Object { $_ -ne $Type })
    foreach ($k in $order) {
        if ($names -contains $k) {
            $v = Get-Prop $Payload $k
            if ($v -is [array] -or ($null -ne $v -and $v -is [Collections.IList])) {
                $items = @($v)
                if ($k -eq 'entry') {
                    # Splunk Atom-style entries: flatten {name, content:{...}}
                    $items = @($items | ForEach-Object {
                            $o = [ordered]@{}
                            $content = Get-Prop $_ 'content'
                            foreach ($n in (Get-PropNames $content)) { $o[$n] = Get-Prop $content $n }
                            $entryName = Get-Prop $_ 'name'
                            if (-not $o.Contains('name')) { $o['name'] = $entryName }
                            if (-not $o.Contains('id')) { $o['id'] = $entryName }
                            [pscustomobject]$o
                        })
                }
                return @{ Items = $items; Total = $total; ListKey = $k }
            }
        }
    }
    return $null
}

# Mirrors normalize.is_secret_key().
function Test-SecretKey([string]$Key) {
    $flat = ($Key -replace '[_-]', '').ToLowerInvariant()
    if ($SecretWords -contains $flat) { return $true }
    $words = @([regex]::Matches($Key, '[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+') | ForEach-Object { $_.Value.ToLowerInvariant() })
    foreach ($w in $words) { if ($SecretWords -contains $w) { return $true } }
    for ($i = 0; $i -lt $words.Count - 1; $i++) {
        if ($SecretPairs -contains ($words[$i] + ' ' + $words[$i + 1])) { return $true }
    }
    return $false
}

function Protect-Secrets($Node) {
    if ($null -eq $Node) { return $null }
    if ($Node -is [string] -or $Node -is [ValueType]) { return $Node }
    if ($Node -is [array] -or $Node -is [Collections.IList]) { return , @($Node | ForEach-Object { Protect-Secrets $_ }) }
    $names = @(Get-PropNames $Node)
    if (-not $names.Count) { return $Node }
    $out = [ordered]@{}
    foreach ($n in $names) {
        $v = Get-Prop $Node $n
        $isContainer = ($v -is [array]) -or ($v -is [Collections.IList]) -or (@(Get-PropNames $v).Count -gt 0)
        if ((Test-SecretKey $n) -and -not $isContainer -and $null -ne $v -and "$v" -ne '') { $out[$n] = $Redacted }
        else { $out[$n] = Protect-Secrets $v }
    }
    return [pscustomobject]$out
}

function Get-FirstPresent($Obj, [string[]]$Keys) {
    $names = @(Get-PropNames $Obj)
    foreach ($k in $Keys) {
        if ($names -contains $k) {
            $v = Get-Prop $Obj $k
            if ($null -ne $v -and "$v" -ne '') { return $k }
        }
    }
    return $null
}

function Save-Json([string]$Name, $Value, [switch]$Raw) {
    $path = Join-Path $OutDir $Name
    $text = if ($Raw) { [string]$Value } else { ConvertTo-Json -InputObject $Value -Depth 100 }
    [IO.File]::WriteAllText($path, $text, (New-Object Text.UTF8Encoding($false)))
    return $path
}

# -------------------------------------------------------------------- main ---
if (-not $BaseUrl) {
    if (-not $Stack) { throw 'Set -Stack (or $env:SPLUNK_STACK), or -BaseUrl.' }
    $hostName = if ($Stack.Contains('.')) { $Stack } else { "$Stack.splunkcloud.com" }
    $BaseUrl = "https://${hostName}:8089"
}
$script:Root = $BaseUrl.TrimEnd('/')
$BasePath = '/' + $BasePath.Trim('/')
$script:PlainToken = Get-PlainToken
if (-not $script:PlainToken) { throw 'No token given.' }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$OutDir = (Resolve-Path $OutDir).Path

$summary = [ordered]@{
    probed_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    target    = $script:Root
    base_path = $BasePath
    ps_version = $PSVersionTable.PSVersion.ToString()
    server    = $null
    identity  = $null
    endpoints = [ordered]@{}
}
$allGood = $true

Write-Host "DMX Inventory - Phase 0 probe  ->  $($script:Root)$BasePath"

# 1. Connectivity and token ---------------------------------------------------
Write-Step '1. Connectivity and token (/services/server/info)'
$r = Invoke-SplunkGet '/services/server/info' @{ output_mode = 'json' }
if ($r.Status -eq 200) {
    $c = (ConvertFrom-JsonSafe $r.Body).entry[0].content
    $version = [string](Get-Prop $c 'version')
    $summary.server = [ordered]@{ version = $version; server_name = Get-Prop $c 'serverName'; instance_type = Get-Prop $c 'instance_type' }
    Write-Ok "Reached $(Get-Prop $c 'serverName'), Splunk $version, instance_type=$(Get-Prop $c 'instance_type') ($($r.Ms) ms)"
    try {
        $v = [version](($version -split '[^0-9.]')[0])
        if ($v -lt [version]'10.2') { Write-Warn 'App needs Splunk 10.2+ (python.required = 3.13/3.14). Check with Splunk before installing.' }
        else { Write-Ok 'Version is 10.2 or later (needed for python.required = 3.13/3.14).' }
    } catch { Write-Warn "Couldn't parse version '$version'." }
}
elseif ($null -eq $r.Status) {
    Write-Fail "No HTTP response: $($r.Error)"
    Write-Info 'Check the host name, that your IP is on the ACS search-api allow list, and any proxy.'
    $summary.server = [ordered]@{ error = $r.Error }
    Save-Json 'summary.json' $summary | Out-Null
    exit 1
}
else {
    Write-Fail "HTTP $($r.Status): $(Get-ErrorText $r)"
    if ($r.Status -eq 401) { Write-Info 'Token rejected. Check it is valid, not expired, and that -AuthScheme matches (Bearer for tokens).' }
    $summary.server = [ordered]@{ http_status = $r.Status; error = Get-ErrorText $r }
    Save-Json 'summary.json' $summary | Out-Null
    exit 1
}

# 2. Identity, roles and capabilities (A3) -----------------------------------
Write-Step '2. Identity, roles and capabilities (A3)'
$r = Invoke-SplunkGet '/services/authentication/current-context' @{ output_mode = 'json' }
if ($r.Status -eq 200) {
    $c = (ConvertFrom-JsonSafe $r.Body).entry[0].content
    $roles = @(Get-Prop $c 'roles')
    $caps = @(Get-Prop $c 'capabilities')
    $relevant = @($caps | Where-Object { $_ -match 'dmx|ingest|pipeline|data_?manag|processor|edge|destination' } | Sort-Object)
    $summary.identity = [ordered]@{ username = Get-Prop $c 'username'; roles = $roles; capability_count = $caps.Count; dmx_related_capabilities = $relevant }
    Write-Ok "User '$(Get-Prop $c 'username')', roles: $($roles -join ', ')"
    if ($roles -contains 'sc_admin') { Write-Info 'Token has sc_admin. To find the minimum role (A3), repeat with a token for a test role.' }
    if ($relevant.Count) { Write-Info "DMX-related capabilities: $($relevant -join ', ')" }
    else { Write-Info 'No capability names mention DMX / ingest / pipelines.' }
}
else {
    Write-Warn "current-context returned HTTP $($r.Status): $(Get-ErrorText $r)"
}

# 3. DMX endpoints (A1, A4, A5) ----------------------------------------------
foreach ($ep in $Endpoints) {
    $path = "$BasePath/$ep"
    Write-Step "3. $path"
    $res = [ordered]@{ path = $path; http_status = $null; ok = $false }
    $summary.endpoints[$ep] = $res

    $r = Invoke-SplunkGet $path @{ output_mode = 'json'; offset = 0; count = $PageSize }
    $res.http_status = $r.Status
    $res.elapsed_ms = $r.Ms
    if ($r.Status -ne 200) {
        $allGood = $false
        $res.error = if ($null -eq $r.Status) { $r.Error } else { Get-ErrorText $r }
        Write-Fail "HTTP $($r.Status): $($res.error)"
        switch ($r.Status) {
            403 { Write-Info "Forbidden: this token's role can't read it (A3). Try an sc_admin token." }
            404 { Write-Info 'Not found: the path is wrong for this stack (A1). Find the real one: open Data Management > Ingest Processor in a browser, press F12 > Network, and look at the API calls it makes. Then re-run with -BasePath.' }
        }
        continue
    }
    $rawPath = Save-Json "$ep.json" $r.Body -Raw
    $json = ConvertFrom-JsonSafe $r.Body
    if ($null -eq $json -and $r.Body.Trim() -ne 'null') {
        $allGood = $false
        $res.error = 'response is not JSON'
        Write-Fail "Response is not JSON (saved to $rawPath). First 200 chars:"
        Write-Info $r.Body.Substring(0, [Math]::Min(200, $r.Body.Length))
        continue
    }
    $topKeys = if ($json -is [array]) { @('(array)') } else { @(Get-PropNames $json) }
    $res.top_level_keys = $topKeys
    $parsed = Get-Items $json $ep
    if ($null -eq $parsed) {
        $allGood = $false
        $res.error = 'unrecognised response shape'
        Write-Fail "HTTP 200, but the app wouldn't recognise this shape. Top-level keys: $($topKeys -join ', ')"
        Write-Info "Saved to $rawPath. Send a scrubbed copy so client.py can be extended."
        continue
    }
    $items = @($parsed.Items)
    $res.ok = $true
    $res.list_key = $parsed.ListKey
    $res.total = $parsed.Total
    $res.first_page_count = $items.Count
    Write-Ok ("HTTP 200 in $($r.Ms) ms; objects under '$($parsed.ListKey)'; first page: $($items.Count)" +
        $(if ($null -ne $parsed.Total) { "; total: $($parsed.Total)" } else { '; no total field' }))

    # Fields (A5) and sizes (R6)
    if ($items.Count) {
        $first = $items[0]
        $res.object_keys = @(Get-PropNames $first)
        $idKey = Get-FirstPresent $first $IdKeys
        $modKey = Get-FirstPresent $first $ModifiedKeys
        $res.id_field = $idKey
        $res.modified_field = $modKey
        Write-Info "Object fields: $((Get-PropNames $first) -join ', ')"
        if ($idKey) { Write-Ok "ID field: '$idKey'" } else { Write-Warn 'No recognised ID field; the app will fall back to a content hash (A5).' }
        if ($modKey) { Write-Ok "Modified-time field: '$modKey'" } else { Write-Info 'No modified-time field; change detection uses content hashes, which is fine.' }
        $sizes = @($items | ForEach-Object { (ConvertTo-Json -InputObject $_ -Depth 100 -Compress).Length })
        $res.max_object_chars = ($sizes | Measure-Object -Maximum).Maximum
        Write-Info "Largest object on this page: $($res.max_object_chars) characters"

        $scrubbed = Protect-Secrets $json
        $cleanText = ConvertTo-Json -InputObject $scrubbed -Depth 100 -Compress
        $res.redactions = ([regex]::Matches($cleanText, [regex]::Escape($Redacted))).Count
        Save-Json "$ep.scrubbed.json" $scrubbed | Out-Null
        if ($res.redactions) { Write-Info "Redacted $($res.redactions) credential-like value(s) in $ep.scrubbed.json" }
    }
    else {
        Write-Info 'First page is empty; nothing to inspect. Create a test object and re-run if you need samples.'
        Save-Json "$ep.scrubbed.json" $json | Out-Null
    }

    # Pagination (A4)
    if ($items.Count -ge $PageSize) {
        $r2 = Invoke-SplunkGet $path @{ output_mode = 'json'; offset = $PageSize; count = $PageSize }
        $p2 = if ($r2.Status -eq 200) { Get-Items (ConvertFrom-JsonSafe $r2.Body) $ep } else { $null }
        if ($null -eq $p2) {
            $res.pagination = "second page failed (HTTP $($r2.Status))"
            Write-Warn "Second page request failed: HTTP $($r2.Status)"
        }
        elseif (@($p2.Items).Count -eq 0) {
            $res.pagination = 'offset honoured (second page empty)'
            Write-Ok 'offset/count honoured (second page empty)'
        }
        elseif ((ConvertTo-Json -InputObject @($p2.Items)[0] -Depth 100 -Compress) -eq (ConvertTo-Json -InputObject $items[0] -Depth 100 -Compress)) {
            $res.pagination = 'offset ignored'
            Write-Warn 'offset seems to be ignored (same first object). The app copes, but set [api] offset_param/count_param if this API uses other names.'
        }
        else {
            $res.pagination = 'offset honoured'
            Write-Ok 'offset/count honoured (second page differs)'
        }
        if ($items.Count -gt $PageSize) {
            $res.pagination += '; count ignored (page larger than requested)'
            Write-Warn "count seems to be ignored: asked for $PageSize, got $($items.Count)."
        }
    }
    else {
        $res.pagination = 'not tested (fewer objects than page size)'
        Write-Info "Pagination not tested: fewer than $PageSize objects."
    }
}

# Summary ---------------------------------------------------------------------
$summaryPath = Save-Json 'summary.json' $summary
Write-Step 'Summary'
$rows = foreach ($ep in $summary.endpoints.Keys) {
    $e = $summary.endpoints[$ep]
    $get = { param($k) if ($e.Contains($k)) { $e[$k] } else { $null } }
    [pscustomobject]@{
        Endpoint   = $ep
        HTTP       = $e.http_status
        Recognised = $e.ok
        Objects    = & $get 'first_page_count'
        Total      = & $get 'total'
        IdField    = & $get 'id_field'
        Pagination = & $get 'pagination'
    }
}
$rows | Format-Table -AutoSize | Out-String | Write-Host
Write-Host "Results: $summaryPath"
Write-Host "Samples: $OutDir"
Write-Host '  *.json           raw responses - may contain secrets, do not share or commit' -ForegroundColor Yellow
Write-Host '  *.scrubbed.json  credential-like values redacted - review before sharing'
Write-Host ''
Write-Host 'Still to check inside Splunk: run "| dmxcollect dry_run=t" on the search head (A2),'
Write-Host 'and compare it on two cluster members (A7).'

$script:PlainToken = $null
if ($allGood) { exit 0 } else { exit 1 }
