@echo off
setlocal
set "VSINSTALL="
for /f "usebackq tokens=*" %%i in (`"%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VSINSTALL=%%i"
if not defined VSINSTALL (
    echo Install Visual Studio C++ Build Tools first.
    exit /b 1
)
call "%VSINSTALL%\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 exit /b 1
set "PGROOT=%~dp0..\runtime\tools\postgresql"
pushd "%~dp0..\runtime\tools\pgvector" || exit /b 1
nmake /nologo /F Makefile.win
if errorlevel 1 (
    popd
    exit /b 1
)
if exist "%PGROOT%\lib\vector.dll" if exist "%PGROOT%\share\extension\vector.control" if exist "%PGROOT%\share\extension\vector--0.8.7.sql" (
    fc /b vector.dll "%PGROOT%\lib\vector.dll" >nul
    if not errorlevel 1 (
        echo pgvector is already installed with the same binary.
        popd
        exit /b 0
    )
)
nmake /nologo /F Makefile.win install
set "BUILD_RESULT=%errorlevel%"
popd
exit /b %BUILD_RESULT%
