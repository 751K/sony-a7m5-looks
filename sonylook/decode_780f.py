#!/usr/bin/env python3
"""Translate the pre-table-building part of FUN_1000d4288.

Static reconstruction only: no original Sony function is called. Numeric field
names remain destination offsets because their semantics are not established.
"""
import hashlib
import struct

def f32(value):
    return struct.unpack('<f',struct.pack('<f',value))[0]

def decode_special_signed_pair(word):
    """Return high-half result then low-half result, as Sony's stp does.

    Preserve the unusual mask/XOR negative branch literally. Do not replace
    with signed16/4096. ARM64 instructions 1000d43d0..1000d4424 corroborate it.
    """
    low_negative = bool(word & 0x8000)
    low = (word >> 2) & 0x3fff
    if low_negative:
        low = ((word >> 2) & 0x3ffe) ^ 0x3fff
    high_negative = bool(word & 0x80000000)  # signed int32 comparison < 0
    high = word >> 18
    if high_negative:
        high = ((word >> 18) & 0x3ffe) ^ 0x3fff
    high_result = f32((-1 if high_negative else 1) * high / 1024)
    low_result = f32((-1 if low_negative else 1) * low / 1024)
    return high_result, low_result

def decode_blob(blob):
    if len(blob) != 276:
        raise ValueError('FUN_1000d4288 copies exactly 0x114 bytes')
    words = struct.unpack('<69I',blob)
    writes = {}
    def store(offset,value,kind,source):
        key = f'0x{offset:x}'
        if key in writes:
            raise AssertionError('duplicate destination '+key)
        writes[key] = {'type':kind,'value':value,'source':source}
    def u16(offset,value,source):
        store(offset,value & 0xffff,'uint16',source)
    def flt(offset,value,source):
        store(offset,f32(value),'float32',source)

    w0 = words[0]
    for dest,shift,mask in [(0x91998,24,1),(0x9199a,25,1),(0x9199c,23,1),
                            (0x919be,26,3),(0x91bbc,28,3),(0x91bba,30,1),
                            (0x91bb8,31,1)]:
        u16(dest,(w0>>shift)&mask,f'word[0] >> {shift} & 0x{mask:x}')
    u16(0x919a0,w0&0x3ff,'word[0] & 0x3ff')
    u16(0x9199e,(w0>>11)&0x3ff,'word[0] >> 11 & 0x3ff')
    for i in range(1,5):
        base = 0x919a2+(i-1)*6
        u16(base,words[i]>>22,f'word[{i}] >> 22')
        u16(base+2,(words[i]>>11)&0x3ff,f'word[{i}] >> 11 & 0x3ff')
        u16(base+4,words[i]&0x3ff,f'word[{i}] & 0x3ff')
    u16(0x919ba,words[5]>>22,'word[5] >> 22')
    u16(0x919bc,(words[5]>>11)&0x3ff,'word[5] >> 11 & 0x3ff')

    groups = []
    for group in range(6):
        src = 0x18+group*0x20
        dest = 0x919c0+group*0x40
        values = []
        for offset in range(src,src+0x20,4):
            high,low = decode_special_signed_pair(words[offset//4])
            for value,label in [(high,'high'),(low,'low')]:
                flt(dest+len(values)*4,value,f'special_signed_pair(blob+0x{offset:x}).{label}')
                values.append(value)
        groups.append({'group_index':group,'blob_start':f'0x{src:x}',
                       'destination_start':f'0x{dest:x}','values':values})

    unsigned12 = []
    for i in range(0x36,0x3e):
        base = 0x91b80+(i-0x36)*4
        for delta,value,label in [(0,(words[i]>>18)&0xfff,'high'),
                                  (2,(words[i]>>2)&0xfff,'low')]:
            u16(base+delta,value,f'word[{i}] {label} bits 2..13')
            unsigned12.append(value)

    high,low = decode_special_signed_pair(words[0x3e])
    flt(0x91ba0,high,'special_signed_pair(word[62]).high')
    flt(0x91ba4,low,'special_signed_pair(word[62]).low')
    high,_unused_low = decode_special_signed_pair(words[0x3f])
    flt(0x91ba8,high,'special_signed_pair(word[63]).high')

    # Exact destination order of the tail byte fields in the decompilation.
    byte_mapping = [(0x3f,8),(0x3f,0),
                    (0x40,24),(0x40,16),(0x40,8),(0x40,0),
                    (0x41,24),(0x41,16),(0x41,8),(0x41,0),
                    (0x42,24),(0x42,16),(0x42,8),(0x42,0),
                    (0x43,24),(0x43,16)]
    byte_floats = []
    for i,(word_index,shift) in enumerate(byte_mapping):
        value = ((words[word_index]>>shift)&0xff)/128
        flt(0x91b40+i*4,value,f'word[{word_index}] >> {shift} & 0xff, /128')
        byte_floats.append(value)
    flt(0x91bac,(words[0x43]&0xffff)/32,'word[67] & 0xffff, /32')
    flt(0x91bb0,(words[0x44]>>16)/32,'word[68] >> 16, /32')
    flt(0x91bb4,(words[0x44]&0xffff)/32,'word[68] & 0xffff, /32')

    # Reconstruct the byte-addressed destination region; the decode writes every
    # byte in this 550-byte range, though source bit fields intentionally overlap.
    region = bytearray(0x91bbe-0x91998)
    coverage = set()
    for key,item in writes.items():
        offset = int(key,16)-0x91998
        packed = struct.pack('<H' if item['type']=='uint16' else '<f',item['value'])
        assert not set(range(offset,offset+len(packed))) & coverage
        coverage.update(range(offset,offset+len(packed)))
        region[offset:offset+len(packed)] = packed
    assert len(coverage)==len(region)
    knots = [writes[f'0x{offset:x}']['value'] for offset in range(0x9199e,0x919be,2)]
    return {
        'blob_sha256':hashlib.sha256(blob).hexdigest(),
        'word_order':'little-endian uint32[69]',
        'words_hex':[f'0x{w:08x}' for w in words],
        'header_fields_by_destination':{k:v for k,v in writes.items() if v['type']=='uint16' and int(k,16)<0x919c0},
        'knots_16_by_destination_order':knots,
        'float_groups_6_by_16':groups,
        'unsigned12_16_by_destination_order':unsigned12,
        'tail_byte_floats_16_by_destination_order':byte_floats,
        'tail_scalar_floats_by_destination':{f'0x{o:x}':writes[f'0x{o:x}']['value'] for o in range(0x91ba0,0x91bb8,4)},
        'writes_by_destination':dict(sorted(writes.items(),key=lambda x:int(x[0],16))),
        'reconstructed_destination_range':'[param_1+0x91998, param_1+0x91bbe)',
        'reconstructed_destination_bytes_hex':region.hex(),
    }
