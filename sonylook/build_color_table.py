#!/usr/bin/env python3
"""Statically recovered Sony 1024 x 18 float table construction.

The default skips FUN_1000d4288's external camera correction lookup explicitly.
No Sony application execution takes place in this module.
"""
import base64
import ctypes
import hashlib
import importlib.util
import io
import json
import math
from pathlib import Path
import struct
import zipfile

CONSTANT_ADDRESSES = {
    'mode2_param8_nonzero':0x100237bd8,
    'mode2_param8_zero':0x100237bfc,
    'mode1_param8_zero':0x100237c44,
    'mode0_param8_nonzero':0x100237c68,
}

def round32(v):
    try:
        return struct.unpack('<f',struct.pack('<f',v))[0]
    except OverflowError:
        return math.copysign(math.inf,v)

def add(a,b): return round32(a+b)
def sub(a,b): return round32(a-b)
def mul(a,b): return round32(a*b)
def neg(a): return round32(-a)
def div(a,b):
    if b==0:
        if a==0:
            return struct.unpack('<f',bytes.fromhex('0000c07f'))[0]
        return math.copysign(math.inf, math.copysign(1,a)*math.copysign(1,b))
    return round32(a/b)

# Native C99 fmaf computes a single rounding of the exact product plus addend.
# It is used only where the ARM64 instruction stream actually contains FMA.
_libsystem = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
_fmaf = _libsystem.fmaf
_fmaf.argtypes = (ctypes.c_float,ctypes.c_float,ctypes.c_float)
_fmaf.restype = ctypes.c_float
def fma(a,b,c): return _fmaf(a,b,c)

def read_constants(target=None):
    import edit_binary
    data = Path(target).read_bytes() if target else edit_binary.arm64_slice()
    magic,cpu,subtype,filetype,ncmds,sizeofcmds,flags,reserved = struct.unpack_from('<8I',data)
    if magic != 0xfeedfacf or cpu != 0x100000c:
        raise ValueError('Expected the selected thin ARM64 Mach-O')
    segments = []
    offset = 32
    for _ in range(ncmds):
        command,size = struct.unpack_from('<II',data,offset)
        if command==0x19:
            name,vmaddr,vmsize,fileoff,filesize = struct.unpack_from('<16sQQQQ',data,offset+8)
            segments.append({'name':name.rstrip(b'\0').decode(),'vmaddr':vmaddr,
                             'vmsize':vmsize,'fileoff':fileoff,'filesize':filesize})
        offset += size
    result = {}
    for name,addr in CONSTANT_ADDRESSES.items():
        matches = [s for s in segments if s['vmaddr']<=addr and addr+36<=s['vmaddr']+s['filesize']]
        if len(matches)!=1:
            raise ValueError('Matrix source does not have a unique file-backed mapping')
        segment = matches[0]
        file_offset = segment['fileoff']+(addr-segment['vmaddr'])
        raw = data[file_offset:file_offset+36]
        result[name] = {'vmaddr':f'0x{addr:x}','segment':segment['name'],
                        'file_offset':file_offset,'raw_hex':raw.hex(),
                        'values':list(struct.unpack('<9f',raw))}
    return result,{'path':str(target),'sha256':hashlib.sha256(data).hexdigest(),
                   'segments':segments,'matrices':result}

_CONSTANTS = None
def constants():
    global _CONSTANTS
    if _CONSTANTS is None:
        _CONSTANTS = read_constants()[0]
    return _CONSTANTS

def select_matrix(output_mode,param8):
    key = None
    if output_mode==0 and param8!=0: key='mode0_param8_nonzero'
    elif output_mode==1 and param8==0: key='mode1_param8_zero'
    elif output_mode==2: key='mode2_param8_zero' if param8==0 else 'mode2_param8_nonzero'
    return constants()[key]['values'] if key else None

def _decoded_value(decoded,offset):
    return decoded['writes_by_destination'][f'0x{offset:x}']['value']

def interpolate_entry(distance_from_left,distance_to_right,right_node,left_node,
                      decoded,output_mode=0,param8=0,correction_matrix=None):
    """Function45 scalar transcription; numerical schedule reviewed separately."""
    a,b = round32(distance_from_left),round32(distance_to_right)
    right,left=right_node,left_node
    g=[r['values'] for r in decoded['float_groups_6_by_16']]
    tr=decoded['tail_byte_floats_16_by_destination_order'][right]
    tl=decoded['tail_byte_floats_16_by_destination_order'][left]
    weight=round32(decoded['unsigned12_16_by_destination_order'][left])
    control=_decoded_value(decoded,0x919be)
    factor=div(1.,round32(2 << (control&31))) if control<3 else None
    def weighted(xr,xl): return mul(fma(xl,b,mul(xr,a)),weight)
    def scaled(value):
        return mul(mul(value,1/4096),factor) if control<3 else mul(value,1/65536)

    f18=scaled(weighted(g[0][right],g[0][left]))
    f9=scaled(weighted(g[1][right],g[1][left]))
    f12=scaled(weighted(mul(tr,g[0][right]),mul(tl,g[0][left])))
    f14=scaled(weighted(mul(tr,g[1][right]),mul(tl,g[1][left])))
    afirst=fma(tr,g[0][right],mul(tr,g[1][right]))
    asecond=fma(tl,g[0][left],mul(tl,g[1][left]))
    scalefactor=mul(factor,1/4096) if control<3 else 1/65536
    f25=mul(neg(weighted(afirst,asecond)),scalefactor)

    f10=scaled(weighted(g[2][right],g[2][left]))
    f13=scaled(weighted(g[3][right],g[3][left]))
    br=mul(g[2][right],tr);bl=mul(g[2][left],tl)
    f11=scaled(weighted(br,bl))
    f15=scaled(mul(fma(mul(g[3][left],tl),b,mul(g[3][right],mul(tr,a))),weight))
    f30=mul(mul(fma(fma(g[3][left],tl,bl),neg(b),
                       neg(mul(fma(g[3][right],tr,br),a))),weight),scalefactor)

    f32=weighted(g[4][right],g[4][left])
    f33=weighted(g[5][right],g[5][left])
    cr=mul(g[4][right],tr);cl=mul(g[4][left],tl)
    f17=weighted(cr,cl)
    f29=mul(fma(mul(g[5][left],tl),b,mul(g[5][right],mul(tr,a))),weight)
    f19=mul(fma(fma(g[5][left],tl,cl),neg(b),
                   neg(mul(fma(g[5][right],tr,cr),a))),weight)
    # This path has a different multiply association in the recovered code.
    f28=mul(f19,mul(1/4096,factor)) if control<3 else mul(f19,1/65536)
    f21=scaled(f29);f22=scaled(f17);f16=scaled(f33);f26=scaled(f32)
    f24=add(add(f9,f18),_decoded_value(decoded,0x91ba0))
    f23=add(add(f13,f10),_decoded_value(decoded,0x91ba4))
    f27=neg(f13)
    f20=add(f16,add(f26,_decoded_value(decoded,0x91ba0)))
    f19=neg(f16);f29=f27;f17=neg(f9);f31=neg(f26);f32=neg(f18);f33=neg(f10)
    matrix=select_matrix(output_mode,param8)
    if matrix is not None:
        f19,f25,f29,f28=matrix[:4]
        f24=fma(f19,f24,neg(fma(f25,f10,mul(f29,f26))))
        f32=fma(f25,f23,neg(fma(f19,f18,mul(f29,f16))))
        f17=fma(f29,f20,fma(f25,f27,neg(mul(f19,f9))))
        f19,f25=matrix[4:6]
        f33=fma(f24,f28,neg(fma(f19,f10,mul(f25,f26))))
        f23=fma(neg(f25),f16,fma(f32,f28,mul(f19,f23)))
        f29=fma(f25,f20,fma(f17,f28,neg(mul(f19,f13))))
        f13,f25,f28=matrix[6:9]
        f31=fma(neg(f28),f26,fma(f25,f33,mul(f13,f24)))
        f12=div(mul(f32,f12),neg(f18))
        f19=fma(neg(f28),f16,fma(f25,f23,mul(f13,f32)))
        f20=fma(f28,f20,fma(f25,f29,mul(f13,f17)))
        f14=div(mul(f17,f14),neg(f9));f25=neg(add(f12,f14))
        f11=div(mul(f33,f11),neg(f10));f15=div(mul(f29,f15),f27);f30=neg(add(f11,f15))
        f22=div(mul(f31,f22),neg(f26));f21=div(mul(f19,f21),neg(f16));f28=neg(add(f22,f21))
    c=[1.,0.,0.,0.,1.,0.,0.,0.,1.] if correction_matrix is None else correction_matrix
    rows=[]
    for i in range(0,9,3):
        rows += [fma(c[i+2],f31,fma(c[i+1],f33,mul(c[i],f24))),
                 fma(c[i+2],f19,fma(c[i+1],f23,mul(c[i],f32))),
                 fma(c[i+2],f20,fma(c[i+1],f29,mul(c[i],f17)))]
    return rows+[f25,f12,f14,f11,f30,f15,f22,f21,f28]

def build_table(decoded,output_mode=0,param8=0,correction_matrix=None):
    """Build the table with function38's exact interval/wrap traversal.

    correction_matrix is an explicit row-major sequence of nine float32 values.
    None means identity, corresponding to bypassing the external lookup.
    """
    correction = [1.,0.,0.,0.,1.,0.,0.,0.,1.] if correction_matrix is None else [round32(x) for x in correction_matrix]
    if len(correction)!=9:
        raise ValueError('correction_matrix must contain nine float32 values')
    knots = decoded['knots_16_by_destination_order']
    table = [None]*1024
    def assign(index,right,left,a,b):
        table[index] = interpolate_entry(round32(a),round32(b),right,left,decoded,
                                          output_mode,param8,correction)
    for left in range(15):
        right=left+1
        if knots[left]<knots[right]:
            for i in range(knots[left],knots[right]):
                assign(i,right,left,(i-knots[left])&0xffff,(knots[right]-i)&0xffff)
    if knots[0]!=0:
        for i in range(knots[0]):
            assign(i,0,15,(i-knots[15]+1024)&0xffff,(knots[0]-i)&0xffff)
    if knots[15]<1023:
        for i in range(knots[15],1024):
            assign(i,0,15,(i-knots[15])&0xffff,(knots[0]+1024-i)&0xffff)
    if any(r is None for r in table):
        raise ValueError('Uncovered table indices: knots do not match the recovered traversal assumptions')
    return table

def table_bytes(table):
    return struct.pack('<'+str(len(table)*18)+'f',*(v for row in table for v in row))

def npy_bytes(table):
    header = repr({'descr':'<f4','fortran_order':False,'shape':(len(table),18)})
    padding = (64-(10+len(header)+1)%64)%64
    encoded = (header+' '*padding+'\n').encode('ascii')
    return b'\x93NUMPY\x01\x00'+struct.pack('<H',len(encoded))+encoded+table_bytes(table)
