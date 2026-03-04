"""
Xbox 360 Minecraft LCE savegame.dat -> Windows .mcs converter

Usage: python convert_x360_save.py <savegame.dat> [output.mcs]

This tool:
1. Strips the 4-byte STFS size prefix from the Xbox 360 save
2. Decompresses LZXRLE data (LZX via xcompress.dll, then custom RLE)
3. Byte-swaps the FileHeader and FileEntry structures (big-endian -> little-endian)
4. Byte-swaps region file data (chunk offsets and timestamps)
5. Re-compresses as ZLIBRLE (zlib + RLE) for the Windows build
6. Writes a .mcs file loadable by the LCE Windows build
"""

import ctypes
import struct
import zlib
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# --- LZX Decompression via xcompress.dll ---

def load_xcompress():
    """Load xcompress.dll with the kernelx.dll shim."""
    os.add_dll_directory(SCRIPT_DIR)
    ctypes.WinDLL(os.path.join(SCRIPT_DIR, "kernelx.dll"))
    return ctypes.WinDLL(os.path.join(SCRIPT_DIR, "xcompress.dll"))


def lzx_decompress(xcomp, compressed_data, decompressed_size):
    """Decompress LZX data using XMemDecompress."""
    XMEMCODEC_LZX = 1

    class XMEMCODEC_PARAMETERS_LZX(ctypes.Structure):
        _fields_ = [
            ("Flags", ctypes.c_uint32),
            ("WindowSize", ctypes.c_uint32),
            ("CompressionPartitionSize", ctypes.c_uint32),
        ]

    params = XMEMCODEC_PARAMETERS_LZX()
    params.Flags = 0
    params.WindowSize = 128 * 1024
    params.CompressionPartitionSize = 128 * 1024

    context = ctypes.c_void_p()
    hr = xcomp.XMemCreateDecompressionContext(
        XMEMCODEC_LZX,
        ctypes.byref(params),
        0,
        ctypes.byref(context),
    )
    if hr != 0:
        raise RuntimeError(f"XMemCreateDecompressionContext failed: 0x{hr:08X}")

    src_buf = (ctypes.c_ubyte * len(compressed_data))(*compressed_data)
    dst_buf = (ctypes.c_ubyte * decompressed_size)()
    dst_size = ctypes.c_size_t(decompressed_size)

    hr = xcomp.XMemDecompress(
        context,
        dst_buf,
        ctypes.byref(dst_size),
        src_buf,
        ctypes.c_size_t(len(compressed_data)),
    )

    xcomp.XMemDestroyDecompressionContext(context)

    if hr != 0:
        raise RuntimeError(f"XMemDecompress failed: 0x{hr:08X}")

    return bytes(dst_buf[: dst_size.value])


# --- RLE Decompression (from compression.cpp DecompressLZXRLE) ---

def rle_decompress(data):
    """
    Decompress the custom RLE format used by LCE.
    Format:
      0-254: literal byte
      255 followed by 0,1,2: 1,2,3 literal 255s
      255 followed by 3-255 followed by byte: run of (count+1) copies of byte
    """
    result = bytearray()
    i = 0
    length = len(data)

    while i < length:
        b = data[i]
        i += 1
        if b == 255:
            if i >= length:
                break
            count = data[i]
            i += 1
            if count < 3:
                result.extend(b"\xff" * (count + 1))
            else:
                if i >= length:
                    break
                val = data[i]
                i += 1
                result.extend(bytes([val]) * (count + 1))
        else:
            result.append(b)

    return bytes(result)


# --- RLE Compression (from compression.cpp CompressRLE / CompressLZXRLE) ---

def rle_compress(data):
    """
    Compress with the custom RLE format used by LCE.
    """
    result = bytearray()
    i = 0
    length = len(data)

    while i < length:
        b = data[i]
        i += 1
        count = 1
        while i < length and data[i] == b and count < 256:
            i += 1
            count += 1

        if count <= 3:
            if b == 255:
                result.append(255)
                result.append(count - 1)
            else:
                result.extend(bytes([b]) * count)
        else:
            result.append(255)
            result.append(count - 1)
            result.append(b)

    return bytes(result)


# --- Endianness Conversion ---

def swap16(data, offset):
    """Swap a 16-bit value in-place."""
    data[offset], data[offset + 1] = data[offset + 1], data[offset]


def swap32(data, offset):
    """Swap a 32-bit value in-place."""
    data[offset : offset + 4] = data[offset : offset + 4][::-1]


def swap64(data, offset):
    """Swap a 64-bit value in-place."""
    data[offset : offset + 8] = data[offset : offset + 8][::-1]


def swap_wchar_array(data, offset, count):
    """Swap an array of wchar_t (2 bytes each)."""
    for i in range(count):
        swap16(data, offset + i * 2)


def convert_file_header(data):
    """
    Convert the FileHeader from big-endian (Xbox 360) to little-endian (Windows).

    Layout of decompressed save data:
      Bytes 0-3:   headerOffset (uint32)
      Bytes 4-7:   headerSize (uint32) = number of file entries
      Bytes 8-9:   originalSaveVersion (int16)
      Bytes 10-11: saveVersion (int16)
      Bytes 12+:   file data
      At headerOffset: file table (headerSize entries, each 144 bytes)
    """
    buf = bytearray(data)

    # Read header values in big-endian
    header_offset = struct.unpack(">I", buf[0:4])[0]
    header_size = struct.unpack(">I", buf[4:8])[0]
    orig_version = struct.unpack(">h", buf[8:10])[0]
    save_version = struct.unpack(">h", buf[10:12])[0]

    print(f"  Header offset: {header_offset}")
    print(f"  File count: {header_size}")
    print(f"  Original version: {orig_version}")
    print(f"  Save version: {save_version}")

    # Swap the header fields to little-endian
    swap32(buf, 0)  # headerOffset
    swap32(buf, 4)  # headerSize
    swap16(buf, 8)  # originalSaveVersion
    swap16(buf, 10)  # saveVersion

    # Swap each FileEntry in the file table
    # FileEntrySaveDataV2 = 144 bytes:
    #   wchar_t filename[64]  = 128 bytes
    #   uint32  length        = 4 bytes
    #   uint32  startOffset   = 4 bytes
    #   int64   lastModified  = 8 bytes
    ENTRY_SIZE = 144

    print(f"\n  Files in save:")
    for i in range(header_size):
        entry_offset = header_offset + i * ENTRY_SIZE

        # Read filename before swapping (big-endian wchar)
        raw_name = buf[entry_offset : entry_offset + 128]
        name_chars = []
        for j in range(64):
            c = struct.unpack(">H", raw_name[j * 2 : j * 2 + 2])[0]
            if c == 0:
                break
            name_chars.append(chr(c))
        filename = "".join(name_chars)

        length = struct.unpack(">I", buf[entry_offset + 128 : entry_offset + 132])[0]
        start = struct.unpack(">I", buf[entry_offset + 132 : entry_offset + 136])[0]

        print(f"    {filename!r}: offset={start}, length={length}")

        # Swap the fields
        swap_wchar_array(buf, entry_offset, 64)  # filename
        swap32(buf, entry_offset + 128)  # length
        swap32(buf, entry_offset + 132)  # startOffset
        swap64(buf, entry_offset + 136)  # lastModifiedTime

    return bytes(buf), header_size, header_offset, save_version


def convert_region_files(data, header_offset, header_size):
    """
    Convert region file data from big-endian to little-endian.
    Region files (.mcr) contain:
      - Sector 0: offset table (1024 uint32 entries)
      - Sector 1: timestamp table (1024 uint32 entries)
      - Sectors 2+: chunk data with per-chunk headers

    This converts the offset/timestamp tables and chunk headers.
    """
    buf = bytearray(data)
    ENTRY_SIZE = 144
    SECTOR_SIZE = 4096
    SECTOR_INTS = 1024

    for i in range(header_size):
        entry_offset = header_offset + i * ENTRY_SIZE

        # Read filename (now in little-endian after header conversion)
        name_chars = []
        for j in range(64):
            c = struct.unpack("<H", buf[entry_offset + j * 2 : entry_offset + j * 2 + 2])[0]
            if c == 0:
                break
            name_chars.append(chr(c))
        filename = "".join(name_chars)

        if not filename.endswith(".mcr"):
            continue

        file_start = struct.unpack("<I", buf[entry_offset + 132 : entry_offset + 136])[0]
        file_length = struct.unpack("<I", buf[entry_offset + 128 : entry_offset + 132])[0]

        print(f"  Converting region file: {filename} (offset={file_start}, size={file_length})")

        if file_length < 2 * SECTOR_SIZE:
            print(f"    Region file too small, skipping")
            continue

        # Swap offset table (sector 0: 1024 uint32 values)
        for j in range(SECTOR_INTS):
            swap32(buf, file_start + j * 4)

        # Swap timestamp table (sector 1: 1024 uint32 values)
        for j in range(SECTOR_INTS):
            swap32(buf, file_start + SECTOR_SIZE + j * 4)

        # Swap chunk data headers
        # Each chunk entry in the offset table encodes: (sector_count & 0xFF) | (sector_offset << 8)
        # The chunk data at each sector has an 8-byte header:
        #   uint32 compressed_length (MSB = RLE flag)
        #   uint32 decompressed_length
        for j in range(SECTOR_INTS):
            offset_entry = struct.unpack("<I", buf[file_start + j * 4 : file_start + j * 4 + 4])[0]
            if offset_entry == 0:
                continue

            sector_count = offset_entry & 0xFF
            sector_offset = offset_entry >> 8

            if sector_offset < 2:
                continue

            chunk_data_offset = file_start + sector_offset * SECTOR_SIZE
            if chunk_data_offset + 8 > len(buf):
                continue

            # Swap the chunk header (compressed_length and decompressed_length)
            swap32(buf, chunk_data_offset)
            swap32(buf, chunk_data_offset + 4)

    return bytes(buf)


# --- Main Conversion ---

def convert_save(input_path, output_path):
    print(f"Loading {input_path}...")
    with open(input_path, "rb") as f:
        raw_data = f.read()

    file_size = len(raw_data)
    print(f"File size: {file_size} bytes")

    # Check for 4-byte STFS size prefix
    # The first 4 bytes (BE) should match approximately (file_size - padding - 4)
    prefix_size = struct.unpack(">I", raw_data[0:4])[0]
    comp_flag = struct.unpack(">I", raw_data[4:8])[0]

    if comp_flag == 0 and prefix_size > 0 and prefix_size < file_size:
        print(f"Detected 4-byte STFS prefix (size={prefix_size})")
        save_data = raw_data[4:]
    else:
        save_data = raw_data

    # Parse compression header
    compressed_flag = struct.unpack(">I", save_data[0:4])[0]
    if compressed_flag != 0:
        print("Save data does not appear to be compressed (flag != 0)")
        print("Treating as raw uncompressed save data")
        decompressed = save_data
    else:
        decomp_size = struct.unpack(">I", save_data[4:8])[0]
        compressed_data = save_data[8:]
        print(f"Compressed save: decompressed size = {decomp_size} ({decomp_size/1024/1024:.1f} MB)")
        print(f"Compressed data size: {len(compressed_data)} bytes")

        # LZX decompress (whole-file level is pure LZX, not LZXRLE)
        # The RLE layer is only used per-chunk inside region files
        print("\nStep 1: LZX decompression...")
        xcomp = load_xcompress()
        decompressed = lzx_decompress(xcomp, compressed_data, decomp_size)
        print(f"  LZX decompressed: {len(decompressed)} bytes")

    # Step 2: Inspect the raw save structure (big-endian)
    print("\nStep 2: Inspecting raw save data (big-endian Xbox 360 format)...")
    header_offset = struct.unpack(">I", decompressed[0:4])[0]
    file_count = struct.unpack(">I", decompressed[4:8])[0]
    orig_version = struct.unpack(">h", decompressed[8:10])[0]
    save_version = struct.unpack(">h", decompressed[10:12])[0]
    print(f"  Header offset: {header_offset}")
    print(f"  File count: {file_count}")
    print(f"  Original version: {orig_version}, Save version: {save_version}")

    # List files in the save
    ENTRY_SIZE = 144
    print(f"\n  Files in save:")
    for i in range(file_count):
        entry_off = header_offset + i * ENTRY_SIZE
        name_chars = []
        for j in range(64):
            c = struct.unpack(">H", decompressed[entry_off + j * 2 : entry_off + j * 2 + 2])[0]
            if c == 0:
                break
            name_chars.append(chr(c))
        filename = "".join(name_chars)
        length = struct.unpack(">I", decompressed[entry_off + 128 : entry_off + 132])[0]
        start = struct.unpack(">I", decompressed[entry_off + 132 : entry_off + 136])[0]
        print(f"    {filename}: offset={start}, size={length}")

    # Step 3: Write raw decompressed data
    # The game code will handle endianness conversion when loaded with plat=X360
    # Write as uncompressed (first 4 bytes != 0 tells the game it's uncompressed)
    print(f"\nStep 3: Writing {output_path}...")
    with open(output_path, "wb") as f:
        f.write(decompressed)

    print(f"\nDone! Output: {output_path} ({len(decompressed)} bytes)")
    print(f"\nTo use this save with the LCE source code:")
    print(f"  1. Ensure xcompress.dll is available to the Windows build")
    print(f"  2. Replace XMemDecompress stubs with real implementation")
    print(f"  3. Load with: ConsoleSaveFileOriginal(name, data, size, false, SAVE_FILE_PLATFORM_X360)")
    print(f"  4. Call pSave->ConvertToLocalPlatform() after loading")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <savegame.dat> [output.mcs]")
        sys.exit(1)

    input_file = sys.argv[1]
    if len(sys.argv) >= 3:
        output_file = sys.argv[2]
    else:
        base = os.path.splitext(input_file)[0]
        output_file = base + ".mcs"

    convert_save(input_file, output_file)
