@echo off
call "C:\Program Files\Microsoft Visual Studio\2022\Professional\VC\Auxiliary\Build\vcvarsall.bat" x64
cd /d "C:\Users\cole\Documents\GitHub\client\tools\save_converter"
cl /LD /O2 kernelx_shim.c /link /DEF:kernelx.def /OUT:kernelx.dll
