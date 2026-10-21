"""ktx2png.py - convert KTX1 textures (DXT1/DXT3/DXT5 or raw RGBA8) to PNG using Pillow.

Usage: python ktx2png.py <in.ktx> <out.png>
Can also be imported:
    ktx_to_png(in_path, out_path) -> True/False
    ktx_bytes_to_png_bytes(data) -> PNG bytes or None   (no temp files)
"""
import io
import struct
import sys

KTX_MAGIC = b'\xabKTX 11\xbb\r\n\x1a\n'
KTX2_MAGIC = b'\xabKTX 20\xbb\r\n\x1a\n'

# glInternalFormat -> DDS fourCC
DXT1_FORMATS = {0x83F0, 0x83F1}
DXT3_FORMATS = {0x83F2}
DXT5_FORMATS = {0x83F3}
RGBA8_FORMATS = {0x8058, 0x8056, 0x8051, 0x1908, 0x8229, 0x822B}
GL_RGBA = 0x1908
GL_UNSIGNED_BYTE = 0x1401


def _dds_header(width, height, mip_count, fourcc=None, linear_size=0, masks=None):
    flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000  # caps,height,width,pixelformat,linearsize
    hdr = bytearray()
    hdr += b'DDS '
    hdr += struct.pack('<I', 124)
    hdr += struct.pack('<I', flags)
    hdr += struct.pack('<I', height)
    hdr += struct.pack('<I', width)
    hdr += struct.pack('<I', linear_size)
    hdr += struct.pack('<I', 0)          # depth
    hdr += struct.pack('<I', max(1, mip_count))
    hdr += b'\x00' * 44                  # reserved1[11]
    hdr += struct.pack('<I', 32)         # pixelformat size
    if fourcc is not None:
        hdr += struct.pack('<I', 0x4)    # DDPF_FOURCC
        hdr += fourcc
        hdr += struct.pack('<I', 0) * 5  # rgb bit count / masks
    else:
        r, g, b, a = masks
        hdr += struct.pack('<I', 0x41)   # DDPF_RGB | DDPF_ALPHAPIXELS
        hdr += struct.pack('<I', 0)      # fourcc
        hdr += struct.pack('<I', 32)     # rgb bit count
        hdr += struct.pack('<I', r) + struct.pack('<I', g) + struct.pack('<I', b) + struct.pack('<I', a)
    hdr += struct.pack('<I', 0x1000)     # caps: texture
    hdr += struct.pack('<I', 0x8 if mip_count > 1 else 0)  # caps2: complex
    hdr += struct.pack('<I', 0)
    hdr += struct.pack('<I', 0)
    hdr += struct.pack('<I', 0)          # reserved2
    assert len(hdr) == 128, len(hdr)
    return bytes(hdr)


def ktx_bytes_to_png_bytes(data):
    """Convert KTX1 data (bytes) to PNG bytes. Returns None if unsupported."""
    try:
        from PIL import Image
    except ImportError:
        return None
    if len(data) < 64 or not data.startswith(KTX_MAGIC):
        return None
    if data.startswith(KTX2_MAGIC):
        return None
    (endian, gl_type, gl_type_size, gl_format, gl_internal,
     gl_base) = struct.unpack_from('<6I', data, 12)
    (width, height, depth, array_elems, faces,
     mip_levels) = struct.unpack_from('<6I', data, 36)
    if width == 0 or height == 0:
        return None
    kv_off = 64
    kv_bytes = struct.unpack_from('<I', data, 60)[0]
    off = kv_off + kv_bytes
    if gl_internal in DXT1_FORMATS:
        fourcc, masks = b'DXT1', None
    elif gl_internal in DXT3_FORMATS:
        fourcc, masks = b'DXT3', None
    elif gl_internal in DXT5_FORMATS:
        fourcc, masks = b'DXT5', None
    elif gl_internal in RGBA8_FORMATS and gl_format in (GL_RGBA, 0x1907) and gl_type == GL_UNSIGNED_BYTE:
        if gl_internal in (0x8051,):  # RGB8
            return None
        fourcc, masks = None, (0x000000FF, 0x0000FF00, 0x00FF0000, 0xFF000000)
    else:
        return None
    # gather mip levels: after header each mip: u32 imageSize + data
    images = []
    for _ in range(max(1, mip_levels)):
        if off + 4 > len(data):
            break
        sz = struct.unpack_from('<I', data, off)[0]
        off += 4
        if off + sz > len(data):
            break
        images.append(data[off:off + sz])
        off += sz
    if not images:
        return None
    linear = len(images[0])
    hdr = _dds_header(width, height, len(images), fourcc, linear, masks)
    dds = hdr + b''.join(images)
    try:
        img = Image.open(io.BytesIO(dds))
        img.load()
        out = io.BytesIO()
        img.save(out, 'PNG')
        return out.getvalue()
    except Exception:
        return None


def ktx_to_png(in_path, out_path):
    data = open(in_path, 'rb').read()
    png = ktx_bytes_to_png_bytes(data)
    if png is None:
        return False
    with open(out_path, 'wb') as f:
        f.write(png)
    return True


def main():
    if len(sys.argv) < 3:
        print('usage: python ktx2png.py <in.ktx> <out.png>')
        return 1
    ok = ktx_to_png(sys.argv[1], sys.argv[2])
    print('ok' if ok else 'failed')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
