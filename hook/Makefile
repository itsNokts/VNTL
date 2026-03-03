# VNTL hook DLL — MinGW build
#
# Requirements:
#   sudo apt install gcc-mingw-w64    (Debian/Ubuntu)
#   pacman -S mingw-w64-gcc           (Arch/CachyOS)
#
# Usage:
#   make          # builds both x86 and x64 DLLs
#   make x86      # 32-bit only
#   make x64      # 64-bit only
#   make clean

CC_X86 = i686-w64-mingw32-gcc
CC_X64 = x86_64-w64-mingw32-gcc

CFLAGS  = -O2 -Wall -shared -fno-exceptions
LIBS    = -lgdi32 -lpsapi

OUT_X86    = vntl_hook_x86.dll
OUT_X64    = vntl_hook_x64.dll
SRC        = vntl_hook.c

OUT_HELPER = vntl_inject32.exe
SRC_HELPER = vntl_inject32.c

.PHONY: all x86 x64 helper clean

all: x86 x64 helper

x86: $(OUT_X86)
x64: $(OUT_X64)
helper: $(OUT_HELPER)

$(OUT_X86): $(SRC)
	$(CC_X86) -m32 $(CFLAGS) -o $@ $< $(LIBS)

$(OUT_X64): $(SRC)
	$(CC_X64) -m64 $(CFLAGS) -o $@ $< $(LIBS)

$(OUT_HELPER): $(SRC_HELPER)
	$(CC_X86) -m32 -O2 -Wall -o $@ $<

clean:
	rm -f $(OUT_X86) $(OUT_X64) $(OUT_HELPER)
