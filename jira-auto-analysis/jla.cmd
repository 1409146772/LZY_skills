@echo off
REM ===========================================================================
REM jla.cmd -- jira-log-analysis launcher
REM
REM Resolves its own directory, so the tool can be unzipped anywhere and any
REM drive letter works without editing config:
REM   jla.cmd validate
REM   jla.cmd run --keys LH2512024-5408
REM   jla.cmd mail-only --date 2026-09-22 --dry-run
REM
REM Input paths (code / dbc / docs) are NOT command-line arguments -- they come
REM from <TOOL>\inputs.json, which the Claude session writes before calling.
REM See inputs.json.example.
REM
REM Equivalent to:
REM   <TOOL>\.venv\Scripts\python.exe <TOOL>\scripts\jla.py ...
REM
REM NOTE: kept pure ASCII on purpose -- cmd.exe parses .cmd files using the
REM OEM codepage (cp936 here), so non-ASCII comments would corrupt and break
REM the script. Chinese docs live in README.md / SKILL.md instead.
REM ===========================================================================
setlocal

REM Use UTF-8 console code page so Chinese output and the check marks render
REM correctly instead of turning into mojibake.
chcp 65001 >nul 2>&1

set "TOOL=%~dp0"
set "VPY=%TOOL%.venv\Scripts\python.exe"
set "JLA=%TOOL%scripts\jla.py"

REM Use a goto rather than a parenthesised block: quoting inside a block is a
REM reliable way to make cmd.exe choke, and this message needs quotes.
if not exist "%VPY%" goto novenv

REM Run inside the tool dir -- there is no jira\ subdir here, so nothing
REM shadows the python-jira package on import.
pushd "%TOOL%"
"%VPY%" "%JLA%" %*
set "RC=%ERRORLEVEL%"
popd

REM exit /b terminates the script here; setlocal is released automatically.
REM (Do NOT write "endlocal ^& exit /b %RC%" -- the escaped ampersand lets cmd
REM fall through into :novenv with %TOOL% already cleared by endlocal.)
exit /b %RC%


:novenv
echo [jla] venv interpreter not found: %VPY%
echo.
echo The distribution does not ship .venv. Build it once after unzipping:
echo     uv venv --python 3.12 "%TOOL%.venv"
echo     uv pip install --python "%VPY%" -r "%TOOL%requirements.txt"
echo.
echo If imports then fail with missing packaging.version or urllib3.exceptions,
echo the corporate DLP agent has renamed .py files to .py.IPGSD inside the venv.
echo Repair from wheel, bypassing the poisoned uv cache:
echo     uv pip install --python "%VPY%" --no-cache --reinstall urllib3 packaging charset-normalizer
echo.
echo See https://docs.astral.sh/uv/getting-started/installation/ if uv is missing.
exit /b 1
