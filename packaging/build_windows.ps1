<#
.SYNOPSIS
    Compila o PhotoDedupe para Windows: executável (PyInstaller) e instalador (Inno Setup).

.DESCRIPTION
    Faz tudo do zero em uma máquina Windows com Python 3.10+ instalado:
    cria o ambiente virtual, instala as dependências, roda os testes,
    gera dist\PhotoDedupe\PhotoDedupe.exe e, se o Inno Setup estiver
    disponível, também dist\instalador\PhotoDedupe-<versão>-instalador.exe

.PARAMETER PularTestes
    Não executa a suíte de testes antes de empacotar.

.PARAMETER SomenteExe
    Gera apenas o executável, sem tentar criar o instalador.

.PARAMETER SemOpenCV
    Não embute o OpenCV: o pacote fica cerca de 150 MB menor e a análise,
    cerca de 17% mais lenta (o aplicativo usa o caminho em NumPy).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#>

[CmdletBinding()]
param(
    [switch]$PularTestes,
    [switch]$SomenteExe,
    [switch]$SemOpenCV
)

$ErrorActionPreference = "Stop"
$Raiz = Split-Path -Parent $PSScriptRoot
Set-Location $Raiz

function Escrever($texto) { Write-Host "==> $texto" -ForegroundColor Cyan }

# ---------------------------------------------------------------- ambiente
$Python = "python"
try { & $Python --version | Out-Null } catch { throw "Python não encontrado no PATH. Instale o Python 3.10 ou superior." }

$Venv = Join-Path $Raiz ".venv-build"
if (-not (Test-Path $Venv)) {
    Escrever "Criando ambiente virtual em $Venv"
    & $Python -m venv $Venv
}
$VenvPy = Join-Path $Venv "Scripts\python.exe"

Escrever "Instalando dependências"
& $VenvPy -m pip install --upgrade pip wheel | Out-Null
& $VenvPy -m pip install -r requirements.txt
& $VenvPy -m pip install pyinstaller pytest

# ------------------------------------------------------------------ testes
if (-not $PularTestes) {
    Escrever "Executando a suíte de testes"
    & $VenvPy -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "Os testes falharam. Empacotamento interrompido." }
}

# -------------------------------------------------------------- executável
if ($SemOpenCV) {
    Escrever "OpenCV será deixado de fora do pacote"
    $env:PHOTODEDUPE_SKIP_CV2 = "1"
} else {
    Remove-Item Env:\PHOTODEDUPE_SKIP_CV2 -ErrorAction SilentlyContinue
}

Escrever "Gerando o executável com o PyInstaller"
Remove-Item -Recurse -Force (Join-Path $Raiz "build"), (Join-Path $Raiz "dist\PhotoDedupe") -ErrorAction SilentlyContinue
& $VenvPy -m PyInstaller packaging\photodedupe.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw "Falha ao gerar o executável." }

$Exe = Join-Path $Raiz "dist\PhotoDedupe\PhotoDedupe.exe"
if (-not (Test-Path $Exe)) { throw "Executável não encontrado em $Exe" }
$TamanhoMB = [math]::Round(((Get-ChildItem (Join-Path $Raiz "dist\PhotoDedupe") -Recurse | Measure-Object Length -Sum).Sum / 1MB), 1)
Escrever "Executável pronto: $Exe ($TamanhoMB MB na pasta)"

# -------------------------------------------------------------- instalador
if ($SomenteExe) { Escrever "Concluído (sem instalador, conforme solicitado)."; exit 0 }

$Iscc = $null
foreach ($caminho in @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
)) { if (Test-Path $caminho) { $Iscc = $caminho; break } }
if (-not $Iscc) { $Iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source }

if (-not $Iscc) {
    Write-Warning "Inno Setup 6 não encontrado. Baixe em https://jrsoftware.org/isdl.php e rode novamente,"
    Write-Warning "ou use somente a pasta dist\PhotoDedupe (o aplicativo já funciona a partir dela)."
    exit 0
}

Escrever "Gerando o instalador com o Inno Setup"
& $Iscc "packaging\installer.iss"
if ($LASTEXITCODE -ne 0) { throw "Falha ao gerar o instalador." }

$Instalador = Get-ChildItem (Join-Path $Raiz "dist\instalador") -Filter *.exe | Select-Object -First 1
Escrever "Instalador pronto: $($Instalador.FullName)"
