// kernelx.dll shim - forwards standard Win32 functions to kernel32.dll
// and provides stub implementations for Xbox-specific XMemAlloc/XMemFree
#include <windows.h>
#include <stdlib.h>

// Forward declarations for exports
__declspec(dllexport) void* __stdcall XMemAlloc(SIZE_T dwSize, DWORD dwAllocAttributes);
__declspec(dllexport) void __stdcall XMemFree(void* pAddress, DWORD dwAllocAttributes);

// Xbox-specific memory functions - map to standard heap
__declspec(dllexport) void* __stdcall XMemAlloc(SIZE_T dwSize, DWORD dwAllocAttributes) {
    (void)dwAllocAttributes;
    return malloc(dwSize);
}

__declspec(dllexport) void __stdcall XMemFree(void* pAddress, DWORD dwAllocAttributes) {
    (void)dwAllocAttributes;
    free(pAddress);
}

// All other functions are forwarded to kernel32.dll via the .def file
BOOL WINAPI DllMain(HINSTANCE hinstDLL, DWORD fdwReason, LPVOID lpReserved) {
    (void)hinstDLL; (void)fdwReason; (void)lpReserved;
    return TRUE;
}
